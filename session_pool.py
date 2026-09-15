"""按 user_id 索引的 Telethon 客户端池。

即使现在只有一个用户，也按池子来写：
  - 惰性连接：有任务才 connect
  - 空闲超时自动 disconnect，不白占内存和 socket
  - LRU 淘汰，上限 SESSION_POOL_SIZE
  - session 失效（用户在手机上踢掉设备）自动标记，不做无意义重试
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    SessionRevokedError,
    UserDeactivatedError,
)
from telethon.sessions import StringSession

import crypto
import db
from config import CFG

log = logging.getLogger("pool")

SESSION_DEAD = (
    AuthKeyUnregisteredError,
    AuthKeyDuplicatedError,
    SessionRevokedError,
    UserDeactivatedError,
)


class NoSession(RuntimeError):
    """该用户还没登录，或 session 已失效。"""


@dataclass
class _Entry:
    client: TelegramClient
    last_used: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionPool:
    def __init__(self) -> None:
        self._entries: OrderedDict[int, _Entry] = OrderedDict()
        self._guard = asyncio.Lock()
        self._reaper: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._reaper = asyncio.create_task(self._reap_loop())

    async def stop(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        async with self._guard:
            for uid, e in list(self._entries.items()):
                await self._drop(uid, e)

    # -------------------------------------------------- 获取客户端

    async def acquire(self, user_id: int) -> TelegramClient:
        """拿到一个已连接、已授权的客户端。用完不需要显式释放。"""
        async with self._guard:
            entry = self._entries.get(user_id)
            if entry is not None:
                entry.last_used = time.monotonic()
                self._entries.move_to_end(user_id)
                if entry.client.is_connected():
                    return entry.client
                self._entries.pop(user_id, None)

            enc = await db.load_session(user_id)
            if not enc:
                raise NoSession("尚未登录")

            client = TelegramClient(
                StringSession(crypto.decrypt(enc)),
                CFG.api_id,
                CFG.api_hash,
                connection_retries=5,
                retry_delay=2,
                auto_reconnect=True,
                request_retries=3,
            )
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise NoSession("session 已失效")
            except SESSION_DEAD as e:
                await db.mark_session_invalid(user_id)
                await _safe_disconnect(client)
                raise NoSession("session 已被撤销，请重新 /login") from e
            except NoSession:
                await db.mark_session_invalid(user_id)
                await _safe_disconnect(client)
                raise

            entry = _Entry(client)
            self._entries[user_id] = entry
            self._entries.move_to_end(user_id)
            await self._evict_if_needed()
            log.info("session 已连接 user=%s", user_id)
            return client

    def lock_for(self, user_id: int) -> asyncio.Lock:
        """同一用户的 session 串行使用，避免并发请求互相踩 FloodWait。"""
        entry = self._entries.get(user_id)
        if entry is None:
            # acquire 之后一定存在；这里只是兜底
            return asyncio.Lock()
        return entry.lock

    async def invalidate(self, user_id: int) -> None:
        async with self._guard:
            e = self._entries.pop(user_id, None)
            if e:
                await self._drop(user_id, e)
        await db.mark_session_invalid(user_id)

    async def logout(self, user_id: int) -> None:
        """真正从 Telegram 服务端撤销这个 session，而不只是本地删除。"""
        try:
            client = await self.acquire(user_id)
            await client.log_out()
        except Exception as e:  # noqa: BLE001
            log.warning("服务端登出失败 user=%s: %s", user_id, e)
        async with self._guard:
            e2 = self._entries.pop(user_id, None)
            if e2:
                await _safe_disconnect(e2.client)
        await db.clear_session(user_id)

    # -------------------------------------------------- 内部

    async def _evict_if_needed(self) -> None:
        while len(self._entries) > CFG.session_pool_size:
            uid, e = self._entries.popitem(last=False)
            log.info("LRU 淘汰 session user=%s", uid)
            await self._drop(uid, e)

    async def _drop(self, user_id: int, e: _Entry) -> None:
        await _safe_disconnect(e.client)

    async def _reap_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                cutoff = time.monotonic() - CFG.session_idle_timeout
                async with self._guard:
                    for uid in [u for u, e in self._entries.items()
                                if e.last_used < cutoff and not e.lock.locked()]:
                        e = self._entries.pop(uid)
                        log.info("空闲断开 session user=%s", uid)
                        await self._drop(uid, e)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                log.exception("session 回收循环异常")


async def _safe_disconnect(client: TelegramClient) -> None:
    try:
        r = client.disconnect()
        if r is not None:
            await r
    except Exception:  # noqa: BLE001
        pass


POOL = SessionPool()
