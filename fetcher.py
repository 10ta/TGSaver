"""user session 侧：定位消息 -> 判定路径 -> 送进中转频道。

三条路径：
  A 直转   普通内容，forward_messages，全服务端完成，零流量零磁盘
  B 流式   受保护且 < STREAM_MAX_SIZE
  C 落盘   受保护且 >= STREAM_MAX_SIZE
B/C 的实现都在 streamer.py。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChatForwardsRestrictedError,
    FloodWaitError,
    MessageIdInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.types import Message, MessageService, PeerChannel

import streamer
from parser import MsgRef

log = logging.getLogger("fetch")

ALBUM_SCAN = 10  # 相册成员在 id 上连续，前后各扫这么多条


class FetchError(RuntimeError):
    """可以直接展示给用户的失败原因。"""


@dataclass
class Relayed:
    """已经躺在中转频道里、等待 bot copy 的结果。"""

    message_ids: list[int]
    is_album: bool
    path: str          # A / B / C
    bytes_moved: int
    note: str = ""     # 需要额外告知用户的说明


async def resolve_entity(client: TelegramClient, ref: MsgRef) -> Any:
    try:
        if ref.is_private:
            return await client.get_entity(PeerChannel(ref.channel_id))
        return await client.get_entity(ref.username)
    except (ValueError, UsernameNotOccupiedError) as e:
        raise FetchError(
            "找不到这个对话。如果是私有频道，你的账号必须先加入它。"
        ) from e
    except ChannelPrivateError as e:
        raise FetchError("你的账号没有加入这个私有频道，或已被移出。") from e


async def load_message(client: TelegramClient, entity: Any, ref: MsgRef) -> Message:
    try:
        msg = await client.get_messages(entity, ids=ref.msg_id)
    except MessageIdInvalidError as e:
        raise FetchError("消息 id 无效，可能已被删除。") from e
    if msg is None:
        raise FetchError("这条消息不存在或已被删除。")
    if isinstance(msg, MessageService):
        raise FetchError("这是一条系统消息（入群/置顶提示等），无法转存。")
    return msg


async def load_album(client: TelegramClient, entity: Any, msg: Message) -> list[Message]:
    """相册成员的 id 是连续的，在附近扫一圈把同组的凑齐。"""
    if not msg.grouped_id:
        return [msg]
    ids = list(range(msg.id - ALBUM_SCAN, msg.id + ALBUM_SCAN + 1))
    try:
        near = await client.get_messages(entity, ids=ids)
    except Exception:  # noqa: BLE001
        return [msg]
    group = [m for m in near
             if m and not isinstance(m, MessageService)
             and getattr(m, "grouped_id", None) == msg.grouped_id]
    group.sort(key=lambda m: m.id)
    return group or [msg]


def is_protected(entity: Any, msg: Message) -> bool:
    if getattr(entity, "noforwards", False):
        return True
    return bool(getattr(msg, "noforwards", False))


async def probe(client: TelegramClient, ref: MsgRef) -> tuple[Any, Message, bool]:
    """轻量探测：定位消息并判断是否受保护，用于决定走哪条通道。"""
    entity = await resolve_entity(client, ref)
    msg = await load_message(client, entity, ref)
    return entity, msg, is_protected(entity, msg)


def media_size(msg: Message) -> int:
    f = getattr(msg, "file", None)
    return int(getattr(f, "size", 0) or 0) if f else 0


async def relay(
    client: TelegramClient,
    ref: MsgRef,
    relay_channel: int,
    on_progress: Optional[Callable[..., Any]] = None,
    task_id: Optional[int] = None,
    register_tmp: Optional[Callable[[Optional[str]], Any]] = None,
    entity: Any = None,
    msg: Optional[Message] = None,
) -> Relayed:
    """把链接指向的消息搬进中转频道。

    entity/msg 可由 probe() 预先传入，避免重复拉取。
    """
    if entity is None:
        entity = await resolve_entity(client, ref)
    if msg is None:
        msg = await load_message(client, entity, ref)

    group = [msg] if ref.single else await load_album(client, entity, msg)
    is_album = len(group) > 1
    protected = is_protected(entity, msg)

    note = ""
    if msg.poll is not None:
        note = "投票会被复制成一个全新的投票，原始票数无法保留（API 限制）。"
    elif getattr(msg, "reply_markup", None) is not None:
        note = "原消息带有 inline 按钮，复制后按钮会丢失（API 限制）。"

    # ---------- 路径 A ----------
    if not protected:
        try:
            sent = await client.forward_messages(
                relay_channel, [m.id for m in group], entity
            )
            ids = [m.id for m in (sent if isinstance(sent, list) else [sent])]
            log.info("路径A 直转 %s -> %s", ref, ids)
            return Relayed(ids, is_album, "A", 0, note)
        except ChatForwardsRestrictedError:
            protected = True  # 实体标记没显示，但服务端拒绝了，降级
        except FloodWaitError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("直转失败，降级到转存: %s", e)
            protected = True

    # ---------- 路径 B / C ----------
    if msg.media is None:
        # 纯文字的受保护消息：不需要搬运字节，直接重发文本
        sent = await client.send_message(
            relay_channel,
            msg.message or "(空消息)",
            formatting_entities=msg.entities or None,
            link_preview=False,
        )
        return Relayed([sent.id], False, "B", 0, note)

    all_ids: list[int] = []
    total = 0
    biggest = 0
    for i, m in enumerate(group):
        if m.media is None:
            sent = await client.send_message(
                relay_channel, m.message or "",
                formatting_entities=m.entities or None, link_preview=False)
            all_ids.append(sent.id)
            continue

        def wrapped(phase, done, tot, elapsed, _i=i):
            if on_progress:
                on_progress(phase, done, tot, elapsed, _i + 1, len(group))

        ids, nbytes = await streamer.relay_protected(
            client, m, relay_channel,
            on_progress=wrapped, task_id=task_id, register_tmp=register_tmp,
        )
        all_ids.extend(ids)
        total += nbytes
        biggest = max(biggest, nbytes)

    path = "C" if _used_disk(biggest) else "B"
    if is_album:
        note = (note + " " if note else "") + \
               "受保护相册逐条转存，无法保持相册分组。"
    log.info("路径%s 转存 %s -> %s (%s)", path, ref, all_ids,
             streamer.human_size(total))
    return Relayed(all_ids, False, path, total, note.strip())


def _used_disk(size: int) -> bool:
    from config import CFG
    return CFG.stream_max_size > 0 and size > CFG.stream_max_size
