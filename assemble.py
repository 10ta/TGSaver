"""把中转频道里已有的多条消息，按类型重新组成相册。

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
"""
from __future__ import annotations

import logging
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

from streamer import _ids_from_updates

log = logging.getLogger("assemble")

ALBUM_MAX = 10


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


def plan_blocks(msgs: list) -> list[list]:
    """分组。返回若干块，每块是一个相册（≥2 项）或一条单独的消息。"""
    blocks: list[list] = []
    where: dict[str, int] = {}
    for m in msgs:
        c = category(m)
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


async def compose(client: Any, peer: Any, ids: list[int]) -> tuple[list[int], str]:
    """把中转频道里的 ids 组装好，返回 (最终的消息 id, 说明)。"""
    msgs = [m for m in await client.get_messages(peer, ids=ids) if m is not None]
    if not msgs:
        return [], ""
    blocks = plan_blocks(msgs)

    if all(len(b) == 1 for b in blocks):
        # 没有可以合并的（比如全是贴纸），原样用，省得多发一遍
        return [b[0].id for b in blocks], ""

    final: list[int] = []
    note = ""
    for b in blocks:
        if len(b) == 1:
            # 单条也重发一遍，保证最终 id 递增（copyMessages 的要求）
            sent = await client.forward_messages(peer, [b[0].id], peer,
                                                 drop_author=True)
            final.extend(x.id for x in (sent if isinstance(sent, list) else [sent]))
            continue
        try:
            multi = [InputSingleMedia(
                media=utils.get_input_media(x.media),
                random_id=helpers.generate_random_long(),
                message=x.message or "",
                entities=x.entities or None,     # 每项保留自己的说明文字
            ) for x in b]
            res = await client(SendMultiMediaRequest(peer=peer, multi_media=multi))
            got = _ids_from_updates(res)
            if not got:
                raise RuntimeError("相册已发出但未能读回消息 id")
            final.extend(got)
        except Exception as e:  # noqa: BLE001
            log.warning("组装相册失败，改为逐条: %s", e)
            sent = await client.forward_messages(peer, [x.id for x in b], peer,
                                                 drop_author=True)
            final.extend(x.id for x in (sent if isinstance(sent, list) else [sent]))
            note = "部分内容未能组成相册，已逐条发送。"

    try:
        await client.delete_messages(peer, [m.id for m in msgs])
    except Exception as e:  # noqa: BLE001
        log.debug("清理中间副本失败（不影响结果）: %s", e)
    return final, note
