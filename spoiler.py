"""剧透的统一处理。

剧透有两种：媒体上的遮罩（photo / document 的 spoiler 标记），和文字里的
剧透片段（MessageEntitySpoiler）。由 /setting 里的总开关决定保留还是去掉，
默认去掉；链接后加 nosp 是这一次强制去掉。

各条路径原本的表现不一样：
  直转（A）     服务端转发，遮罩和文字剧透原样带过来
  重传（B / C） 重建媒体时不设遮罩，遮罩丢了；文字剧透原样保留
所以统一在搬进中转频道**之后**补一步：比对想要的状态和实际状态，
不一致就把这批消息用服务端引用重发一遍，再删掉旧的。

重发引用的是中转频道里已有的媒体，不下载、不上传，零流量。
整批重发而不是只改有问题的那几条，是为了保住相册分组和 id 递增
（Bot API 的 copyMessages 要求 id 严格递增）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from telethon import helpers, utils
from telethon.tl.functions.messages import SendMediaRequest, SendMultiMediaRequest
from telethon.tl.types import (
    InputSingleMedia,
    MessageEntitySpoiler,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaWebPage,
)

from streamer import _ids_from_updates

log = logging.getLogger("spoiler")


def can_cover(m: Any) -> bool:
    """这条消息的媒体能不能带遮罩（只有照片和文件类可以）。"""
    media = getattr(m, "media", None)
    return (isinstance(media, MessageMediaPhoto) and media.photo is not None) or \
        (isinstance(media, MessageMediaDocument) and media.document is not None)


def covered(m: Any) -> bool:
    return can_cover(m) and bool(getattr(m.media, "spoiler", False))


def has_text_spoiler(m: Any) -> bool:
    return any(isinstance(e, MessageEntitySpoiler) for e in (getattr(m, "entities", None) or []))


def involved(msgs: list) -> bool:
    """来源里有没有任何剧透。没有的话后面整步都可以跳过，一个请求都不多发。"""
    return any(covered(m) or has_text_spoiler(m) for m in msgs)


def wanted(sources: list, n: int, keep: bool) -> list[bool]:
    """每条中转消息想要的遮罩状态。保留时照抄来源，去掉时全部 False。

    来源和中转结果条数对不上（极少见）时，有一张带遮罩就全部带上。
    """
    if not keep:
        return [False] * n
    flags = [covered(m) for m in sources]
    if len(flags) == n:
        return flags
    return [any(flags)] * n


def _entities(m: Any, keep_text: bool) -> Optional[list]:
    ents = list(getattr(m, "entities", None) or [])
    if not keep_text:
        ents = [e for e in ents if not isinstance(e, MessageEntitySpoiler)]
    return ents or None


def _input(m: Any, want: bool) -> Any:
    im = utils.get_input_media(m.media)
    im.spoiler = want
    return im


def _needs_change(m: Any, want: bool, keep_text: bool) -> bool:
    if can_cover(m) and covered(m) != want:
        return True
    return not keep_text and has_text_spoiler(m)


def _blocks(pairs: list) -> list[list]:
    """相邻且 grouped_id 相同的归成一个相册，其余各自一条。"""
    out: list[list] = []
    for m, w in pairs:
        gid = getattr(m, "grouped_id", None)
        if out and gid is not None and getattr(out[-1][0][0], "grouped_id", None) == gid:
            out[-1].append((m, w))
        else:
            out.append([(m, w)])
    return out


async def apply(client: Any, peer: Any, ids: list[int], want: list[bool],
                keep_text: bool) -> list[int]:
    """让中转频道里这批消息的剧透状态符合要求，返回最终的消息 id。

    已经符合就原样返回。中途失败时撤掉已经重发的部分、返回原来的 id ——
    内容照样送到，只是剧透没处理成，不能因为这一步让整个任务失败。
    """
    got = await client.get_messages(peer, ids=ids)
    pairs = [(m, w) for m, w in zip(got, want) if m is not None]
    if not any(_needs_change(m, w, keep_text) for m, w in pairs):
        return ids

    out: list[int] = []
    try:
        for block in _blocks(pairs):
            if len(block) > 1 and all(can_cover(m) for m, _ in block):
                multi = [InputSingleMedia(
                    media=_input(m, w), random_id=helpers.generate_random_long(),
                    message=m.message or "", entities=_entities(m, keep_text))
                    for m, w in block]
                res = await client(SendMultiMediaRequest(peer=peer, multi_media=multi))
                out.extend(_ids_from_updates(res))
                continue
            for m, w in block:
                out.extend(await _resend_one(client, peer, m, w, keep_text))
    except Exception as e:  # noqa: BLE001
        log.warning("剧透处理失败，按原样投递: %s", e)
        if out:
            try:
                await client.delete_messages(peer, out)
            except Exception:  # noqa: BLE001
                pass
        return ids

    try:
        await client.delete_messages(peer, [m.id for m, _ in pairs])
    except Exception as e:  # noqa: BLE001
        log.debug("清理旧副本失败（不影响结果）: %s", e)
    log.info("剧透已处理：%d 条重发（%s）", len(out), "保留" if any(want) else "去掉")
    return out


async def _resend_one(client: Any, peer: Any, m: Any, want: bool,
                      keep_text: bool) -> list[int]:
    if can_cover(m):
        res = await client(SendMediaRequest(
            peer=peer, media=_input(m, want), message=m.message or "",
            entities=_entities(m, keep_text),
            random_id=helpers.generate_random_long()))
        return _ids_from_updates(res)
    if m.media is None or isinstance(m.media, MessageMediaWebPage):
        sent = await client.send_message(
            peer, m.message or "", formatting_entities=_entities(m, keep_text),
            link_preview=isinstance(m.media, MessageMediaWebPage))
        return [sent.id]
    # 投票、位置这类：没有剧透可言，原样复制一份占住顺序
    sent = await client.forward_messages(peer, [m.id], peer, drop_author=True)
    return [x.id for x in (sent if isinstance(sent, list) else [sent])]
