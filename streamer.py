"""受保护内容的转存实现。

Telegram 服务端拒绝 forward 的消息（频道开了 noforwards），字节必须
经过本机。但"经过"不等于"落盘"：

  路径 B  流式   iter_download -> 有界队列(16MB) -> upload_file
                 磁盘 0，内存峰值 ~30MB
  路径 C  落盘   下载到 TMP_DIR -> upload_file -> 立即删除
                 用于超大文件，上传失败可低成本重试

分界线是 STREAM_MAX_SIZE（默认 1GB）。

原理：Telethon 的 upload_file 接受任何带 read() 的对象，且会 await
异步的 read。我们提供一个 read() 从下载队列取数据的伪文件对象，
下载和上传就并行跑起来了，中间只有一个有界缓冲。
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from telethon import TelegramClient
from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    Message,
)

from config import CFG

log = logging.getLogger("stream")

CHUNK = 512 * 1024          # 512KB，Telethon 单次请求上限
QUEUE_DEPTH = 32            # 32 * 512KB = 16MB 有界缓冲

ProgressCb = Callable[[int, int], Any]


class TransferError(RuntimeError):
    pass


# --------------------------------------------------------------- 有界管道

class _DownloadStream:
    """把 iter_download 包装成 upload_file 能读的伪文件。

    read(n) 永远返回恰好 n 字节，除非到了文件末尾。
    生产者受 Queue.maxsize 反压，不会把整个文件读进内存。
    """

    def __init__(self, client: TelegramClient, media: Any, size: int,
                 name: str, on_progress: Optional[ProgressCb] = None) -> None:
        self._client = client
        self._media = media
        self._size = size
        self.name = name
        self._on_progress = on_progress
        self._q: asyncio.Queue[Optional[bytes]] = asyncio.Queue(QUEUE_DEPTH)
        self._buf = bytearray()
        self._eof = False
        self._done = 0
        self._err: Optional[BaseException] = None
        self._task: Optional[asyncio.Task] = None

    async def __aenter__(self) -> "_DownloadStream":
        self._task = asyncio.create_task(self._produce())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _produce(self) -> None:
        try:
            async for chunk in self._client.iter_download(
                self._media, request_size=CHUNK
            ):
                await self._q.put(bytes(chunk))
                self._done += len(chunk)
                if self._on_progress:
                    self._on_progress(self._done, self._size)
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001
            self._err = e
        finally:
            await self._q.put(None)

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            n = self._size
        while len(self._buf) < n and not self._eof:
            item = await self._q.get()
            if item is None:
                self._eof = True
                if self._err is not None:
                    raise TransferError(f"下载中断: {self._err}") from self._err
                break
            self._buf.extend(item)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


# --------------------------------------------------------------- 属性重建

@dataclass
class MediaSpec:
    """从原消息还原出来的、重新上传所需的一整套参数。"""

    size: int
    file_name: str
    mime_type: Optional[str]
    attributes: list
    is_photo: bool
    force_document: bool
    caption: str
    entities: list
    thumb: Optional[io.BytesIO] = None


def inspect_media(msg: Message) -> MediaSpec:
    """读出媒体的全部属性，保证重传后形态与原件一致。"""
    caption = msg.message or ""
    entities = list(msg.entities or [])

    if msg.photo is not None:
        return MediaSpec(
            size=getattr(msg.file, "size", 0) or 0,
            file_name=getattr(msg.file, "name", None) or "photo.jpg",
            mime_type="image/jpeg",
            attributes=[],
            is_photo=True,
            force_document=False,
            caption=caption,
            entities=entities,
        )

    doc = msg.document
    if doc is None:
        raise TransferError("这条消息没有可转存的媒体")

    attrs = list(doc.attributes or [])
    name = None
    is_sticker = False
    for a in attrs:
        if isinstance(a, DocumentAttributeFilename):
            name = a.file_name
        elif isinstance(a, DocumentAttributeSticker):
            is_sticker = True

    if not name:
        ext = _guess_ext(doc.mime_type, attrs)
        name = f"file_{msg.id}{ext}"

    # 视频/动图/语音 如果丢了对应属性，Telegram 会当成普通文件显示，
    # 播放按钮和缩略图都会没有。这里显式补齐。
    has_video = any(isinstance(a, DocumentAttributeVideo) for a in attrs)
    has_audio = any(isinstance(a, DocumentAttributeAudio) for a in attrs)
    is_gif = any(isinstance(a, DocumentAttributeAnimated) for a in attrs)

    if has_video:
        # 原地改，不重建对象。新版 DocumentAttributeVideo 有 video_codec、
        # nosound、preload_prefix_size 等字段，重建会把它们丢掉。
        for a in attrs:
            if isinstance(a, DocumentAttributeVideo):
                a.supports_streaming = True

    force_doc = not (has_video or has_audio or is_gif or is_sticker)

    return MediaSpec(
        size=doc.size,
        file_name=name,
        mime_type=doc.mime_type,
        attributes=attrs,
        is_photo=False,
        force_document=force_doc,
        caption=caption,
        entities=entities,
    )


def _guess_ext(mime: Optional[str], attrs: list) -> str:
    if any(isinstance(a, DocumentAttributeVideo) for a in attrs):
        return ".mp4"
    if any(isinstance(a, DocumentAttributeAudio) for a in attrs):
        return ".ogg"
    table = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
             "video/mp4": ".mp4", "audio/ogg": ".ogg", "audio/mpeg": ".mp3",
             "application/pdf": ".pdf", "application/zip": ".zip"}
    return table.get(mime or "", ".bin")


async def fetch_thumb(client: TelegramClient, msg: Message) -> Optional[io.BytesIO]:
    """单独取一份缩略图。只有几十 KB，直接进内存。"""
    doc = msg.document
    if doc is None or not getattr(doc, "thumbs", None):
        return None
    try:
        data = await client.download_media(msg, thumb=-1, file=bytes)
        if not data:
            return None
        bio = io.BytesIO(data)
        bio.name = "thumb.jpg"
        return bio
    except Exception as e:  # noqa: BLE001
        log.debug("缩略图获取失败，忽略: %s", e)
        return None


# --------------------------------------------------------------- 两条路径

async def relay_protected(
    client: TelegramClient,
    msg: Message,
    relay_channel: int,
    on_progress: Optional[Callable[[str, int, int, float], Any]] = None,
    task_id: Optional[int] = None,
    register_tmp: Optional[Callable[[Optional[str]], Any]] = None,
) -> tuple[list[int], int]:
    """把一条受保护消息搬进中转频道，返回 (中转侧 message_id 列表, 字节数)。"""
    spec = inspect_media(msg)

    if spec.size > CFG.max_upload_size:
        raise TransferError(
            f"文件 {_hs(spec.size)} 超过账号上传上限 {_hs(CFG.max_upload_size)}，"
            f"无法转存。"
        )

    spec.thumb = await fetch_thumb(client, msg)
    use_disk = CFG.stream_max_size > 0 and spec.size > CFG.stream_max_size

    started = time.monotonic()

    def _prog(phase: str):
        def cb(done: int, total: int) -> None:
            if on_progress:
                on_progress(phase, done, total or spec.size,
                            time.monotonic() - started)
        return cb

    if use_disk:
        sent = await _via_disk(client, msg, spec, relay_channel,
                               _prog, task_id, register_tmp)
    else:
        sent = await _via_stream(client, msg, spec, relay_channel, _prog)

    ids = [m.id for m in (sent if isinstance(sent, list) else [sent])]
    return ids, spec.size


async def _via_stream(client, msg, spec: MediaSpec, relay: int, prog) -> Any:
    log.info("流式转存 %s (%s)", spec.file_name, _hs(spec.size))
    media = msg.photo if spec.is_photo else msg.document
    async with _DownloadStream(
        client, media, spec.size, spec.file_name, prog("下载")
    ) as stream:
        handle = await client.upload_file(
            stream,
            file_size=spec.size,
            file_name=spec.file_name,
            progress_callback=prog("上传"),
        )
    return await _send(client, relay, handle, spec)


async def _via_disk(client, msg, spec: MediaSpec, relay: int, prog,
                    task_id, register_tmp) -> Any:
    CFG.tmp_dir.mkdir(parents=True, exist_ok=True)
    need = int(spec.size * CFG.disk_headroom)
    free = shutil.disk_usage(CFG.tmp_dir).free
    if free < need:
        raise TransferError(
            f"磁盘空间不足：需要 {_hs(need)}，{CFG.tmp_dir} 只剩 {_hs(free)}。"
        )

    path = CFG.tmp_dir / f"t{task_id or os.getpid()}_{msg.id}_{spec.file_name}"
    if register_tmp:
        await _maybe_await(register_tmp(str(path)))
    log.info("落盘转存 %s (%s) -> %s", spec.file_name, _hs(spec.size), path)
    try:
        await client.download_media(msg, file=str(path),
                                    progress_callback=prog("下载"))
        handle = await client.upload_file(
            str(path), file_name=spec.file_name, progress_callback=prog("上传")
        )
        return await _send(client, relay, handle, spec)
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("临时文件删除失败: %s", path)
        if register_tmp:
            await _maybe_await(register_tmp(None))


async def _send(client, relay: int, handle, spec: MediaSpec):
    return await client.send_file(
        relay,
        file=handle,
        caption=spec.caption or None,
        formatting_entities=spec.entities or None,
        attributes=spec.attributes or None,
        mime_type=spec.mime_type,
        thumb=spec.thumb,
        force_document=spec.force_document,
        supports_streaming=not spec.force_document and not spec.is_photo,
    )


async def _maybe_await(v: Any) -> None:
    if hasattr(v, "__await__"):
        await v


def _hs(n: int) -> str:
    x = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or u == "TB":
            return f"{x:.1f}{u}" if u != "B" else f"{int(x)}B"
        x /= 1024
    return f"{x:.1f}TB"


human_size = _hs


def cleanup_orphans(paths: list[str]) -> int:
    """启动时清理崩溃遗留的临时文件。"""
    n = 0
    for p in paths:
        try:
            f = Path(p)
            if f.is_file():
                f.unlink()
                n += 1
        except OSError:
            pass
    return n
