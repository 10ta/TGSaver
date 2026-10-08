"""会话使用：不能让一个长传输堵住其他操作，也不能让回收断开正在用的会话。"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "h")
os.environ.setdefault("BOT_TOKEN", "t")
os.environ.setdefault("OWNER_ID", "42")
os.environ.setdefault("RELAY_CHANNEL_ID", "-1001111111111")
os.environ.setdefault("SECRET_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402

import session_pool  # noqa: E402


def _pool_with_entry():
    pool = session_pool.SessionPool()
    client = type("C", (), {"is_connected": lambda self: True,
                            "disconnect": lambda self: None})()
    pool._entries[42] = session_pool._Entry(client)
    return pool


@pytest.mark.asyncio
async def test_long_transfer_does_not_block_others():
    """线上现象：慢通道在搬 1GB 文件，抓取卡在「正在查找」、快通道也不动。"""
    pool = _pool_with_entry()
    transfer_started = asyncio.Event()
    release = asyncio.Event()

    async def long_transfer():
        async with pool.using(42):
            transfer_started.set()
            await release.wait()        # 模拟两小时的下载上传

    async def quick_lookup():
        async with pool.using(42):
            return "ok"

    t = asyncio.create_task(long_transfer())
    await transfer_started.wait()
    assert await asyncio.wait_for(quick_lookup(), timeout=1) == "ok", \
        "长传输进行中，其他操作必须能立刻进行"
    release.set()
    await t


@pytest.mark.asyncio
async def test_busy_counts_concurrent_users():
    pool = _pool_with_entry()
    async with pool.using(42):
        async with pool.using(42):
            assert pool._entries[42].busy == 2
        assert pool._entries[42].busy == 1
    assert pool._entries[42].busy == 0


@pytest.mark.asyncio
async def test_reaper_skips_busy_session(monkeypatch):
    """两小时的传输期间，空闲回收不能把会话断开。"""
    from config import CFG
    object.__setattr__(CFG, "session_idle_timeout", 0)
    pool = _pool_with_entry()
    dropped = []

    async def fake_drop(uid, e):
        dropped.append(uid)
    pool._drop = fake_drop

    real_sleep = asyncio.sleep

    async def fast_sleep(d):
        await real_sleep(0)
    monkeypatch.setattr(session_pool.asyncio, "sleep", fast_sleep)

    async with pool.using(42):
        pool._entries[42].last_used = time.monotonic() - 9999
        task = asyncio.create_task(pool._reap_loop())
        await real_sleep(0.05)
        assert dropped == [], "使用中的会话被回收了"
        task.cancel()

    pool._entries[42].last_used = time.monotonic() - 9999
    task = asyncio.create_task(pool._reap_loop())
    await real_sleep(0.05)
    task.cancel()
    assert dropped == [42], "空闲的会话应该被回收"


@pytest.mark.asyncio
async def test_using_unknown_user_is_noop():
    pool = session_pool.SessionPool()
    async with pool.using(999):
        pass
