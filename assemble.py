"""把中转频道里已有的多条消息，按类型重新组成相册，并汇总说明文字。

全部是服务端操作：用 user 账号把这些消息的媒体引用重新发成相册，
不下载、不上传。

Telegram 的相册规则：照片和视频可以混在一起；文件只能和文件组队；
音频只能和音频组队；动图、贴纸、语音、圆形视频根本进不了相册。
所以按类别分组，每组最多 10 个。类别按第一次出现的顺序排，
同类内部保持原顺序——尽量少拆成几条。

还有一个容易踩的坑：Bot API 的 copyMessages 要求消息 id 严格递增。
如果相册是新发的（id 大）、单条是原样保留的（id 小），顺序一乱整批复制
就会失败、退化成逐条，相册分组全丢。所以只要有任何东西需要组装，
就按最终顺序把**所有**内容都重新发一遍，保证 id 递增，再删掉中间副本。

说明文字：相册里各项各带说明时，聊天里只显示图，文字要点开单张才看得到。
所以和推文合并一样，每个相册把所有文字汇总到第一项的说明里，其余项清空：

    ┃ YM 闪闪 :
    ┃ 1 : 第一条的文字
    ┃ 2-4 : 第二条（一个 3 张图的相册）的文字
    1-4 · #ym_ss_bot

    via @署名

同一来源合并成一个引用块，逐条列出；没有文字的那条不单独占一行。
序号是在这个相册里的位置。超长时公平截断，固定部分本身就装不下时，
相册不带说明、汇总另发一条文字消息 —— 都沿用推文的逻辑。
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from telethon import helpers, utils
from telethon.tl.functions.messages import SendMultiMediaRequest
from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    InputSingleMedia,
    MessageMediaDocument,
    MessageMediaPhoto,
)

import tweet
from streamer import _ids_from_updates

log = logging.getLogger("assemble")

ALBUM_MAX = 10


@dataclass
class Unit:
    """一条抓取结果：来源里的一条消息或一个相册，搬进中转频道后的样子。"""
    key: str                 # 来源标识，用来把同一来源的条目归到一个引用块
    name: str                # 来源显示名，如「YM 闪闪」
    tag: str                 # 来源用户名做成的 hashtag，没有用户名就是空串
    ids: list[int] = field(default_factory=list)


def _label(start: int, count: int) -> str:
    return str(start) if count == 1 else f"{start}-{start + count - 1}"


def build_caption(sources: list[tuple], limit: int) -> tuple[str, bool]:
    """汇总说明文字。返回 (html, 是否装得下)。

    sources: [(name, tag, first_pos, last_pos, [(label, text), ...])]，按出现顺序。
    装不下指固定部分本身就超了 limit，调用方该改用更宽松的上限另发一条。
    """
    u16 = tweet.utf16_len
    texts, fixed, n_lines = [], 0, 0
    for name, tag, a, b, items in sources:
        fixed += u16(f"{name} :")
        n_lines += 1
        for label, text in items:
            if text:
                fixed += u16(f"{label} : ")
                texts.append(text)
                n_lines += 1
        if tag:
            fixed += u16(f"{_label(a, b - a + 1)} · {tag}")
            n_lines += 1
    sign_plain = f"\n\nvia {tweet.SIGN_TEXT}" if tweet.SIGN_TEXT else ""
    fixed += u16(sign_plain) + max(n_lines - 1, 0)
    caps = tweet._fair_caps([u16(t) for t in texts], limit - fixed)

    esc = lambda x: html.escape(x, quote=False)     # noqa: E731
    parts, k = [], 0
    for name, tag, a, b, items in sources:
        quote = f"<b>{esc(name)}</b> :"
        for label, text in items:
            if not text:
                continue
            cap = caps[k]
            k += 1
            if cap <= 0:
                body = ""
            elif u16(text) <= cap:
                body = text
            else:
                body = tweet._cut_utf16(text, max(cap - 1, 0)).rstrip() + "…"
            quote += f"\n{label} : {esc(body)}"
        parts.append(f"<blockquote>{quote}</blockquote>")
    for name, tag, a, b, items in sources:
        if tag:
            parts.append(f"{_label(a, b - a + 1)} · {esc(tag)}")
    if tweet.SIGN_TEXT:
        sign = esc(tweet.SIGN_TEXT)
        if tweet.SIGN_URL:
            sign = f'<a href="{html.escape(tweet.SIGN_URL, quote=True)}">{sign}</a>'
        parts += ["", f"via {sign}"]
    return "\n".join(parts), fixed <= limit


def caption_for(block: list, units: list[Unit], unit_text: list[str]) -> list[tuple]:
    """为一个相册算出 build_caption 需要的结构。block 里是 (msg, 单元下标)。"""
    spans: dict[int, list[int]] = {}
    for pos, (_, ui) in enumerate(block, 1):
        spans.setdefault(ui, []).append(pos)

    order: list[str] = []
    by_key: dict[str, list] = {}
    for ui, poss in spans.items():                 # dict 保持插入顺序
        u = units[ui]
        if u.key not in by_key:
            order.append(u.key)
            by_key[u.key] = [u.name, u.tag, poss[0], poss[-1], []]
        g = by_key[u.key]
        g[3] = poss[-1]
        g[4].append((_label(poss[0], len(poss)), unit_text[ui]))
    return [tuple(by_key[k]) for k in order]


def category(msg: Any) -> Optional[str]:
    """能进哪种相册。None 表示只能单独发。"""
    media = getattr(msg, "media", None)
    if isinstance(media, MessageMediaPhoto) and media.photo is not None:
        return "visual"
    if isinstance(media, MessageMediaDocument) and media.document is not None:
        attrs = media.document.attributes or []
        if any(isinstance(a, (DocumentAttributeSticker, DocumentAttributeAnimated))
               for a in attrs):
            return None
        for a in attrs:
            if isinstance(a, DocumentAttributeVideo):
                return None if a.round_message else "visual"
            if isinstance(a, DocumentAttributeAudio):
                return None if a.voice else "audio"
        return "file"
    return None


def plan_blocks(msgs: list, msg_of=lambda x: x) -> list[list]:
    """分组。返回若干块，每块是一个相册（≥2 项）或一条单独的消息。

    msg_of 用来从条目里取出消息本身（条目可以是 (msg, 单元下标) 这样的元组）。
    """
    blocks: list[list] = []
    where: dict[str, int] = {}
    for m in msgs:
        c = category(msg_of(m))
        if c is None:
            blocks.append([m])
            continue
        if c not in where:
            where[c] = len(blocks)
            blocks.append([m])
        else:
            blocks[where[c]].append(m)

    out: list[list] = []
    for b in blocks:
        for i in range(0, len(b), ALBUM_MAX):
            out.append(b[i:i + ALBUM_MAX])
    return out


async def compose(client: Any, peer: Any,
                  units: list[Unit]) -> tuple[list[int], str]:
    """把各单元在中转频道里的消息组装好，返回 (最终的消息 id, 说明)。"""
    from telethon.extensions import html as tl_html

    all_ids = [i for u in units for i in u.ids]
    got = {m.id: m for m in await client.get_messages(peer, ids=all_ids) if m}
    items = [(got[i], ui) for ui, u in enumerate(units) for i in u.ids if i in got]
    if not items:
        return [], ""

    # 每个单元的文字：来源相册里可能有几项各带说明，按顺序拼起来
    unit_text = ["\n".join(t for t in (
        (got[i].message or "").strip() for i in u.ids if i in got) if t)
        for u in units]

    blocks = plan_blocks(items, msg_of=lambda x: x[0])
    if all(len(b) == 1 for b in blocks):
        # 没有可以合并的（比如全是贴纸），原样用，省得多发一遍
        return [b[0][0].id for b in blocks], ""

    final: list[int] = []
    note = ""
    for b in blocks:
        msgs = [m for m, _ in b]
        if len(b) == 1:
            # 进不了相册的单条：原样重发（带它自己的说明），保证最终 id 递增
            sent = await client.forward_messages(peer, [msgs[0].id], peer,
                                                 drop_author=True)
            final.extend(x.id for x in (sent if isinstance(sent, list) else [sent]))
            continue

        sources = caption_for(b, units, unit_text)
        cap_html, fits = build_caption(sources, tweet.CAPTION_LIMIT)
        if fits:
            cap_text, cap_ents = tl_html.parse(cap_html)
            long_html = None
        else:
            cap_text, cap_ents = "", None
            long_html, _ = build_caption(sources, tweet.TEXT_LIMIT)

        try:
            multi = [InputSingleMedia(
                media=utils.get_input_media(m.media),
                random_id=helpers.generate_random_long(),
                message=cap_text if i == 0 else "",
                entities=(cap_ents or None) if i == 0 else None,
            ) for i, m in enumerate(msgs)]
            res = await client(SendMultiMediaRequest(peer=peer, multi_media=multi))
            ids = _ids_from_updates(res)
            if not ids:
                raise RuntimeError("相册已发出但未能读回消息 id")
            final.extend(ids)
        except Exception as e:  # noqa: BLE001
            log.warning("组装相册失败，改为逐条: %s", e)
            sent = await client.forward_messages(peer, [m.id for m in msgs], peer,
                                                 drop_author=True)
            final.extend(x.id for x in (sent if isinstance(sent, list) else [sent]))
            note = "部分内容未能组成相册，已逐条发送。"
            continue

        if long_html:
            # 固定部分本身就超 1024：相册不带说明，汇总另发一条
            t, ents = tl_html.parse(long_html)
            msg = await client.send_message(peer, t, formatting_entities=ents or None,
                                            link_preview=False)
            final.append(msg.id)

    try:
        await client.delete_messages(peer, list(got))
    except Exception as e:  # noqa: BLE001
        log.debug("清理中间副本失败（不影响结果）: %s", e)
    return final, note
