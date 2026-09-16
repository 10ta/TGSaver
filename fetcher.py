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
    MsgIdInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.messages import GetDiscussionMessageRequest
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


def _coerce_peer(v: str) -> Any:
    """内部伪链接里的 peer：纯数字当 id，否则当用户名。"""
    s = str(v).strip()
    try:
        return int(s)
    except ValueError:
        return s


async def resolve_entity(client: TelegramClient, ref: MsgRef) -> Any:
    try:
        if ref.direct_peer is not None:
            return await client.get_entity(_coerce_peer(ref.direct_peer))
        if ref.is_private:
            return await client.get_entity(PeerChannel(ref.channel_id))
        return await client.get_entity(ref.username)
    except (ValueError, UsernameNotOccupiedError) as e:
        raise FetchError(
            "找不到这个对话。如果是私有频道，你的账号必须先加入它。"
        ) from e
    except ChannelPrivateError as e:
        raise FetchError("你的账号没有加入这个私有频道，或已被移出。") from e


async def resolve_comment(client: TelegramClient,
                          ref: MsgRef) -> tuple[Any, Message]:
    """?comment=N 指向的是频道关联讨论群里的消息。

    链接里的两个数字属于两套独立编号：
        t.me/chan/181?comment=4832
                 ^^^ 频道里的帖子      ^^^^ 讨论群里的消息
    直接按 181 取会拿回原帖 —— 这正是之前的 bug。
    用 GetDiscussionMessage 从频道帖子反查出讨论群，再按 4832 取。
    """
    channel = await resolve_entity(client, ref)
    try:
        res = await client(GetDiscussionMessageRequest(
            peer=channel, msg_id=ref.msg_id))
    except (MsgIdInvalidError, MessageIdInvalidError) as e:
        raise FetchError("这条帖子没有评论区，或帖子已被删除。") from e
    except ChannelPrivateError as e:
        raise FetchError("你的账号无法访问该频道的讨论群。") from e
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"无法定位评论区：{e}") from e

    if not res.messages:
        raise FetchError("这条帖子没有关联的讨论区。")

    # 讨论群实体在 res.chats 里，按线程根消息的 peer 挑出来
    root = res.messages[0]
    want = getattr(root.peer_id, "channel_id", None)
    group = next((c for c in res.chats if c.id == want), None)
    if group is None:
        group = res.chats[0] if res.chats else None
    if group is None:
        raise FetchError("找不到该频道的讨论群。")

    try:
        msg = await client.get_messages(group, ids=ref.comment_id)
    except ChannelPrivateError as e:
        raise FetchError(
            "你的账号没有加入该频道的讨论群，无法读取评论。\n"
            "请先在 Telegram 里进入这条帖子的评论区（会自动入群）再试。"
        ) from e

    if msg is None:
        raise FetchError("这条评论不存在或已被删除。")
    if isinstance(msg, MessageService):
        raise FetchError("这是一条系统消息，无法转存。")
    return group, msg


async def locate(client: TelegramClient, ref: MsgRef) -> tuple[Any, Message]:
    """统一定位入口：普通链接、私聊伪链接、评论链接都走这里。"""
    if ref.is_comment:
        return await resolve_comment(client, ref)
    entity = await resolve_entity(client, ref)
    msg = await load_message(client, entity, ref)
    return entity, msg


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
    """轻量探测：定位消息并判断是否需要搬运字节，用于决定走哪条通道。

    两种情况需要搬：内容受保护（服务端拒绝转发），
    或链接带了 nosp（用户主动要求重传以剥离剧透遮罩）。
    """
    entity, msg = await locate(client, ref)
    heavy = is_protected(entity, msg) or ref.force_reupload
    return entity, msg, heavy


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
    if entity is None or msg is None:
        entity, msg = await locate(client, ref)

    group = [msg] if ref.single else await load_album(client, entity, msg)
    is_album = len(group) > 1
    protected = is_protected(entity, msg)

    note = ""
    if ref.force_reupload and not protected:
        log.info("nosp：%s 主动走重传路径以剥离剧透遮罩", ref)
    if msg.poll is not None:
        note = "投票会被复制成一个全新的投票，原始票数无法保留（API 限制）。"
    elif getattr(msg, "reply_markup", None) is not None:
        note = "原消息带有 inline 按钮，复制后按钮会丢失（API 限制）。"

    # ---------- 路径 A ----------
    # nosp 要求重传，直转会把原样的剧透遮罩一起带过来，所以跳过这条路
    if not protected and not ref.force_reupload:
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

    # 整组都是媒体 -> 手工组装相册，保持分组
    if is_album and all(m.media is not None for m in group):
        ids, total, album_note = await streamer.relay_protected_album(
            client, group, relay_channel,
            on_progress=on_progress, task_id=task_id, register_tmp=register_tmp,
        )
        path = "C" if _used_disk(max((media_size(m) for m in group), default=0)) else "B"
        keep_album = not album_note
        note = " ".join(x for x in (note, album_note) if x).strip()
        log.info("路径%s 相册转存 %s x%d -> %s (%s)", path, ref, len(group),
                 ids, streamer.human_size(total))
        return Relayed(ids, keep_album, path, total, note)

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
               "这组消息含非媒体项，已逐条转存，未能保持相册分组。"
    log.info("路径%s 转存 %s -> %s (%s)", path, ref, all_ids,
             streamer.human_size(total))
    return Relayed(all_ids, False, path, total, note.strip())


def _used_disk(size: int) -> bool:
    from config import CFG
    return CFG.stream_max_size > 0 and size > CFG.stream_max_size
