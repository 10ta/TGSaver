"""收集箱：随手转发给 bot 的媒体先攒着，/pack 一次组装成相册发回，/clear 清空。

全部由 bot 完成，不经过 user 账号：bot 收到的每个媒体都带 file_id，用 file_id
重新发送是服务端引用，不下载不上传，没有 50MB 限制，2GB 的视频也是瞬间完成。

流程和别的功能一致：先在中转频道里组好，再 copyMessages 给你。这样 fw 可以
直接复用（user 账号从中转频道转出去，隐藏来源）。

收集箱只在内存里。这是「随转随组装」的低频即时操作，重启丢了重新转发就是。

分组规则和抓取组装一样（assemble.plan_blocks）：图片视频混排成相册，文件一组，
音频一组，每组最多 10 个；动图、贴纸、语音、圆形视频进不了相册，单独发。
每个相册的说明汇总所有文字，序号是在这个相册里的位置。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
)

import assemble
import tweet

log = logging.getLogger("pack")

INBOX_MAX = 500         # 收集箱上限，防止误操作攒爆内存
SETTLE = 0.8            # /pack 之前等一下，让同一批转发里还在路上的媒体落进来
COPY_MAX = 100          # copyMessages 一次最多复制这么多条

# 检查顺序有讲究：动图消息同时带 animation 和 document，必须先认 animation
KINDS = ("photo", "video", "animation", "video_note", "voice", "audio",
         "sticker", "document")


@dataclass
class Item:
    msg_id: int                     # 你发给 bot 的那条消息的 id，排序和删除都用它
    kind: str
    file_id: str
    caption: str = ""
    entities: list = field(default_factory=list)
    spoiler: bool = False
    group: Optional[str] = None     # media_group_id：原本是同一个相册
    src_key: str = "self"
    src_name: str = ""
    src_tag: str = ""


def origin_of(m: Any) -> tuple[str, str, str]:
    """转发来源 (标识, 显示名, #用户名)。不是转发来的返回 ("self", "", "")。"""
    o = getattr(m, "forward_origin", None)
    if o is None:
        return "self", "", ""
    t = getattr(o, "type", "")
    if t == "hidden_user":
        name = o.sender_user_name or ""
        return f"h:{name}", name, ""
    if t == "user":
        u = o.sender_user
        name = " ".join(x for x in (u.first_name, u.last_name) if x)
        return f"u{u.id}", name, tweet.to_hashtag(u.username) if u.username else ""
    chat = getattr(o, "chat", None) or getattr(o, "sender_chat", None)
    if chat is None:
        return "self", "", ""
    uname = getattr(chat, "username", None)
    return (f"c{chat.id}", chat.title or "",
            tweet.to_hashtag(uname) if uname else "")


def from_message(m: Any) -> Optional[Item]:
    for kind in KINDS:
        obj = getattr(m, kind, None)
        if not obj:
            continue
        file_id = obj[-1].file_id if kind == "photo" else obj.file_id
        key, name, tag = origin_of(m)
        return Item(
            msg_id=m.message_id, kind=kind, file_id=file_id,
            caption=m.caption or "", entities=list(m.caption_entities or []),
            spoiler=bool(getattr(m, "has_media_spoiler", False)),
            group=m.media_group_id, src_key=key, src_name=name, src_tag=tag)
    return None


# ------------------------------------------------------------------ 收集箱

_inbox: dict[int, list[Item]] = {}
_warned: set[int] = set()


def add(user_id: int, item: Item) -> bool:
    """放进收集箱。满了返回 False。"""
    box = _inbox.setdefault(user_id, [])
    if len(box) >= INBOX_MAX:
        return False
    box.append(item)
    return True


def first_overflow(user_id: int) -> bool:
    """满了之后只提醒一次，免得一批转发刷出几十条提示。"""
    if user_id in _warned:
        return False
    _warned.add(user_id)
    return True


def take(user_id: int, before: Optional[int] = None) -> list[Item]:
    """取出（并移出）收集箱里的东西，按消息 id 从旧到新。

    before 给了就只取 id 比它小的 —— /pack 之后才到的留给下一次。
    """
    box = _inbox.get(user_id, [])
    got = [x for x in box if before is None or x.msg_id < before]
    rest = [x for x in box if not (before is None or x.msg_id < before)]
    if rest:
        _inbox[user_id] = rest
    else:
        _inbox.pop(user_id, None)
    _warned.discard(user_id)
    return sorted(got, key=lambda x: x.msg_id)


def restore(user_id: int, items: list[Item]) -> None:
    """组装失败时放回去，用户可以直接再发一次 /pack。"""
    box = _inbox.setdefault(user_id, [])
    box.extend(items)
    box.sort(key=lambda x: x.msg_id)


def clear(user_id: int) -> int:
    _warned.discard(user_id)
    return len(_inbox.pop(user_id, []))


def count(user_id: int) -> int:
    return len(_inbox.get(user_id, []))


# ------------------------------------------------------------------ 组装

def category(item: Item) -> Optional[str]:
    if item.kind in ("photo", "video"):
        return "visual"
    if item.kind == "document":
        return "file"
    if item.kind == "audio":
        return "audio"
    return None          # 动图、贴纸、语音、圆形视频：只能单独发


def units_of(items: list[Item]) -> tuple[list[assemble.Unit], list[int], list[str]]:
    """把条目归成「单元」：原本同一个相册的算一个单元，其余一条一个。

    返回 (单元, 每个条目所属单元的下标, 每个单元的文字)。
    """
    units: list[assemble.Unit] = []
    owner: list[int] = []
    texts: list[list[str]] = []
    for i, it in enumerate(items):
        prev = items[i - 1] if i else None
        if prev is not None and it.group and it.group == prev.group:
            ui = owner[-1]
        else:
            units.append(assemble.Unit(key=it.src_key, name=it.src_name,
                                       tag=it.src_tag))
            texts.append([])
            ui = len(units) - 1
        owner.append(ui)
        if it.caption.strip():
            texts[ui].append(it.caption.strip())
    return units, owner, ["\n".join(t) for t in texts]


def _ents(item: Item, keep_spoiler: bool) -> list:
    if keep_spoiler:
        return item.entities
    return [e for e in item.entities if getattr(e, "type", "") != "spoiler"]


async def _retry(fn, *a, **kw):
    """Bot 往频道里连发会被限流，按 Telegram 给的秒数等完再发。"""
    for _ in range(6):
        try:
            return await fn(*a, **kw)
        except TelegramRetryAfter as e:
            log.info("限流 %ss", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
    return await fn(*a, **kw)


def _album_item(it: Item, cap_html: Optional[str], keep_spoiler: bool):
    kw = {"media": it.file_id}
    if cap_html:
        kw.update(caption=cap_html, parse_mode="HTML")
    sp = keep_spoiler and it.spoiler
    if it.kind == "photo":
        return InputMediaPhoto(**kw, has_spoiler=sp)
    if it.kind == "video":
        return InputMediaVideo(**kw, has_spoiler=sp, supports_streaming=True)
    if it.kind == "audio":
        return InputMediaAudio(**kw)
    return InputMediaDocument(**kw)


async def _send_single(bot: Any, chat: int, it: Item, keep_spoiler: bool) -> int:
    """单独一条：原样发，带它自己的说明和格式。"""
    sp = keep_spoiler and it.spoiler
    ents = _ents(it, keep_spoiler)
    cap = {"caption": it.caption or None, "caption_entities": ents or None,
           "parse_mode": None}
    k = it.kind
    if k == "photo":
        m = await _retry(bot.send_photo, chat, it.file_id, has_spoiler=sp, **cap)
    elif k == "video":
        m = await _retry(bot.send_video, chat, it.file_id, has_spoiler=sp,
                         supports_streaming=True, **cap)
    elif k == "animation":
        m = await _retry(bot.send_animation, chat, it.file_id, has_spoiler=sp, **cap)
    elif k == "audio":
        m = await _retry(bot.send_audio, chat, it.file_id, **cap)
    elif k == "voice":
        m = await _retry(bot.send_voice, chat, it.file_id, **cap)
    elif k == "video_note":
        m = await _retry(bot.send_video_note, chat, it.file_id)
    elif k == "sticker":
        m = await _retry(bot.send_sticker, chat, it.file_id)
    else:
        m = await _retry(bot.send_document, chat, it.file_id, **cap)
    return m.message_id


@dataclass
class Packed:
    blocks: list[list[int]]          # 中转频道里每一块（相册或单条）的消息 id
    note: str = ""

    @property
    def ids(self) -> list[int]:
        return [i for b in self.blocks for i in b]


async def build(bot: Any, relay: int, items: list[Item], *,
                keep_spoiler: bool = False,
                show_source: bool = False) -> Packed:
    """在中转频道里按块发好。"""
    units, owner, unit_text = units_of(items)
    entries = list(zip(items, owner))
    blocks = assemble.plan_blocks(entries, msg_of=lambda e: e[0], cat=category)

    out: list[list[int]] = []
    note = ""
    for block in blocks:
        its = [it for it, _ in block]
        if len(block) == 1:
            out.append([await _send_single(bot, relay, its[0], keep_spoiler)])
            continue

        sources = assemble.caption_for(block, units, unit_text)
        cap, fits = assemble.build_caption(sources, tweet.CAPTION_LIMIT, show_source)
        long_html = None
        if not fits:
            long_html, _ = assemble.build_caption(sources, tweet.TEXT_LIMIT, show_source)
            cap = None
        try:
            media = [_album_item(it, cap if i == 0 else None, keep_spoiler)
                     for i, it in enumerate(its)]
            sent = await _retry(bot.send_media_group, relay, media)
            ids = [m.message_id for m in sent]
        except TelegramRetryAfter:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("组装相册失败，改为逐条: %s", e)
            ids = [await _send_single(bot, relay, it, keep_spoiler) for it in its]
            note = "部分内容未能组成相册，已逐条发送。"
            long_html = None
        if long_html:
            # 说明的固定部分本身就超了 1024：相册不带说明，汇总另发一条
            m = await _retry(bot.send_message, relay, long_html, parse_mode="HTML",
                             link_preview_options=LinkPreviewOptions(is_disabled=True))
            ids.append(m.message_id)
        out.append(ids)
    return Packed(out, note)


async def deliver(bot: Any, relay: int, chat: int, packed: Packed) -> int:
    """copyMessages 给用户。相册分组照样保住；一次最多 100 条，块不拆开。"""
    n = 0
    chunk: list[int] = []

    async def flush():
        nonlocal n, chunk
        if chunk:
            res = await _retry(bot.copy_messages, chat_id=chat,
                               from_chat_id=relay, message_ids=chunk)
            n += len(res)
            chunk = []

    for ids in packed.blocks:
        if chunk and len(chunk) + len(ids) > COPY_MAX:
            await flush()
        chunk.extend(ids)
    await flush()
    return n


async def delete_originals(bot: Any, chat: int, msg_ids: list[int]) -> None:
    """删掉你转发来的原消息。超过 48 小时的 Telegram 不让删，忽略即可。"""
    for i in range(0, len(msg_ids), 100):
        try:
            await bot.delete_messages(chat, msg_ids[i:i + 100])
        except Exception as e:  # noqa: BLE001
            log.info("删除原消息失败（不影响结果）: %s", e)
