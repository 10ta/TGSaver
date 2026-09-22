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

from telethon import TelegramClient, helpers, utils
from telethon.tl.functions.messages import (
    SendMultiMediaRequest,
    UploadMediaRequest,
)
from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    InputMediaUploadedDocument,
    InputMediaUploadedPhoto,
    InputSingleMedia,
    Message,
    UpdateNewChannelMessage,
    UpdateNewMessage,
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

    def _chunks(self):
        """数据源。子类覆盖它就能换成别的来源（比如 HTTP）。"""
        return self._client.iter_download(self._media, request_size=CHUNK)

    async def _produce(self) -> None:
        try:
            async for chunk in self._chunks():
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
    # 剧透遮罩不保留：重建媒体时一律不设 spoiler，
    # 原消息带遮罩的，转存后是直接可见的。
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


# --------------------------------------------------------------- 相册整组

async def _upload_one(client: TelegramClient, msg: Message, spec: MediaSpec,
                      relay: int, prog, task_id, register_tmp) -> Any:
    """把一条消息的媒体传上去，返回可用于发送的 InputFile 句柄。"""
    use_disk = CFG.stream_max_size > 0 and spec.size > CFG.stream_max_size
    if use_disk:
        return await _upload_via_disk(client, msg, spec, prog,
                                      task_id, register_tmp)
    media = msg.photo if spec.is_photo else msg.document
    async with _DownloadStream(
        client, media, spec.size, spec.file_name, prog("下载")
    ) as stream:
        return await client.upload_file(
            stream, file_size=spec.size, file_name=spec.file_name,
            progress_callback=prog("上传"),
        )


async def _upload_via_disk(client, msg, spec: MediaSpec, prog,
                           task_id, register_tmp) -> Any:
    CFG.tmp_dir.mkdir(parents=True, exist_ok=True)
    need = int(spec.size * CFG.disk_headroom)
    free = shutil.disk_usage(CFG.tmp_dir).free
    if free < need:
        raise TransferError(
            f"磁盘空间不足：需要 {_hs(need)}，{CFG.tmp_dir} 只剩 {_hs(free)}。")

    path = CFG.tmp_dir / f"t{task_id or os.getpid()}_{msg.id}_{spec.file_name}"
    if register_tmp:
        await _maybe_await(register_tmp(str(path)))
    log.info("落盘转存 %s (%s)", spec.file_name, _hs(spec.size))
    try:
        await client.download_media(msg, file=str(path),
                                    progress_callback=prog("下载"))
        return await client.upload_file(
            str(path), file_name=spec.file_name, progress_callback=prog("上传"))
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("临时文件删除失败: %s", path)
        if register_tmp:
            await _maybe_await(register_tmp(None))


async def _to_input_media(client: TelegramClient, handle: Any,
                          spec: MediaSpec, relay: int) -> Any:
    """把上传句柄变成可放进相册的 InputMedia。

    相册要求的是已经在服务端落地的媒体引用，所以必须先走一次
    UploadMedia 把"刚传上去的文件"转成"服务端的媒体对象"，
    再取其中的 photo / document 换成 InputMedia。
    """
    if spec.is_photo:
        fm = InputMediaUploadedPhoto(file=handle)
        r = await client(UploadMediaRequest(relay, media=fm))
        return utils.get_input_media(r.photo)

    thumb = None
    if spec.thumb is not None:
        try:
            thumb = await client.upload_file(spec.thumb)
        except Exception as e:  # noqa: BLE001
            log.debug("缩略图上传失败，忽略: %s", e)

    fm = InputMediaUploadedDocument(
        file=handle,
        mime_type=spec.mime_type or "application/octet-stream",
        attributes=spec.attributes or [],
        thumb=thumb,
        force_file=spec.force_document,
    )
    r = await client(UploadMediaRequest(relay, media=fm))
    return utils.get_input_media(
        r.document, supports_streaming=not spec.force_document)


def _ids_from_updates(result: Any) -> list[int]:
    ids = []
    for u in getattr(result, "updates", []) or []:
        if isinstance(u, (UpdateNewChannelMessage, UpdateNewMessage)):
            m = getattr(u, "message", None)
            if m is not None and getattr(m, "id", None):
                ids.append(m.id)
    return sorted(ids)


async def relay_protected_album(
    client: TelegramClient,
    msgs: list[Message],
    relay_channel: int,
    on_progress: Optional[Callable[..., Any]] = None,
    task_id: Optional[int] = None,
    register_tmp: Optional[Callable[[Optional[str]], Any]] = None,
) -> tuple[list[int], int, str]:
    """把一整组受保护的相册搬进中转频道，保持分组。

    Telethon 的 send_file(列表) 会走 _send_album，但那条路径不传
    attributes —— 文件名、视频时长分辨率、缩略图全会丢。所以这里
    手工组装 SendMultiMedia，逐项保留自己的属性。

    返回 (消息 id 列表, 字节数, 需要告知用户的说明)。
    """
    specs = [inspect_media(m) for m in msgs]
    total = sum(s.size for s in specs)

    for s in specs:
        if s.size > CFG.max_upload_size:
            raise TransferError(
                f"相册中的 {s.file_name}（{_hs(s.size)}）超过账号上传上限 "
                f"{_hs(CFG.max_upload_size)}，整组无法转存。")

    for m, s in zip(msgs, specs):
        s.thumb = await fetch_thumb(client, m)

    started = time.monotonic()
    handles = []
    for i, (m, s) in enumerate(zip(msgs, specs)):
        def _prog(phase: str, _i=i):
            def cb(done: int, tot: int) -> None:
                if on_progress:
                    on_progress(phase, done, tot or s.size,
                                time.monotonic() - started, _i + 1, len(msgs))
            return cb
        log.info("相册 %d/%d 转存 %s (%s)", i + 1, len(msgs),
                 s.file_name, _hs(s.size))
        handles.append(await _upload_one(client, m, s, relay_channel,
                                         _prog, task_id, register_tmp))

    ids, note = await send_handles_as_album(
        client, relay_channel, handles, specs)
    return ids, total, note


# --------------------------------------------------------------- 网络媒体

@dataclass
class WebMedia:
    """来自网页的媒体（目前是推文）。与 Telegram 消息无关，所以单独建模。"""

    kind: str                     # photo | video | gif
    url: str
    width: int = 0
    height: int = 0
    duration: float = 0.0
    thumb_url: Optional[str] = None
    size: int = 0                 # 已知时填，用来预判能否走 URL 直发


class _HttpStream(_DownloadStream):
    """同一个有界管道，数据源换成 HTTP 响应体。"""

    def __init__(self, resp: Any, size: int, name: str,
                 on_progress: Optional[ProgressCb] = None) -> None:
        super().__init__(None, None, size, name, on_progress)
        self._resp = resp

    def _chunks(self):
        return self._resp.content.iter_chunked(CHUNK)


HTTP_UA = "Mozilla/5.0 (compatible; TgSaver/1.0)"


def http_session():
    import aiohttp
    return aiohttp.ClientSession(
        headers={"User-Agent": HTTP_UA},
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=90),
    )


def _web_spec(item: WebMedia, idx: int, size: int) -> MediaSpec:
    if item.kind == "photo":
        return MediaSpec(size=size, file_name=f"photo_{idx + 1}.jpg",
                         mime_type="image/jpeg", attributes=[], is_photo=True,
                         force_document=False, caption="", entities=[])
    name = f"{'gif' if item.kind == 'gif' else 'video'}_{idx + 1}.mp4"
    attrs = [
        DocumentAttributeVideo(
            duration=float(item.duration or 0), w=int(item.width or 0),
            h=int(item.height or 0), supports_streaming=True,
            nosound=item.kind == "gif"),
        DocumentAttributeFilename(file_name=name),
    ]
    if item.kind == "gif":
        attrs.append(DocumentAttributeAnimated())
    return MediaSpec(size=size, file_name=name, mime_type="video/mp4",
                     attributes=attrs, is_photo=False, force_document=False,
                     caption="", entities=[])


async def _fetch_small(http, url: Optional[str], name: str) -> Optional[io.BytesIO]:
    """缩略图这类小文件直接进内存。失败不影响主流程。"""
    if not url:
        return None
    try:
        async with http.get(url) as r:
            if r.status != 200:
                return None
            data = await r.read()
        if not data or len(data) > 1024 * 1024:
            return None
        bio = io.BytesIO(data)
        bio.name = name
        return bio
    except Exception as e:  # noqa: BLE001
        log.debug("小文件获取失败 %s: %s", url, e)
        return None


async def _upload_web(client: TelegramClient, http, item: WebMedia, idx: int,
                      prog, task_id, register_tmp) -> tuple[Any, MediaSpec]:
    """下载一个网络媒体并传到 Telegram，返回 (句柄, 规格)。

    有 Content-Length 就走有界管道边下边传，零磁盘；
    没有的话大小未知，upload_file 无法流式，只能先落到临时目录。
    """
    async with http.get(item.url) as resp:
        if resp.status != 200:
            raise TransferError(f"媒体下载失败（HTTP {resp.status}）")
        size = resp.content_length

        if size:
            if size > CFG.max_upload_size:
                raise TransferError(
                    f"第 {idx + 1} 个媒体 {_hs(size)} 超过上传上限 "
                    f"{_hs(CFG.max_upload_size)}。")
            spec = _web_spec(item, idx, size)
            async with _HttpStream(resp, size, spec.file_name,
                                   prog("下载")) as stream:
                handle = await client.upload_file(
                    stream, file_size=size, file_name=spec.file_name,
                    progress_callback=prog("上传"))
        else:
            CFG.tmp_dir.mkdir(parents=True, exist_ok=True)
            path = CFG.tmp_dir / f"w{task_id or os.getpid()}_{idx}"
            if register_tmp:
                await _maybe_await(register_tmp(str(path)))
            try:
                got = 0
                with open(path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(CHUNK):
                        got += len(chunk)
                        if got > CFG.max_upload_size:
                            raise TransferError(
                                f"第 {idx + 1} 个媒体超过上传上限 "
                                f"{_hs(CFG.max_upload_size)}。")
                        f.write(chunk)
                spec = _web_spec(item, idx, got)
                handle = await client.upload_file(
                    str(path), file_name=spec.file_name,
                    progress_callback=prog("上传"))
            finally:
                path.unlink(missing_ok=True)
                if register_tmp:
                    await _maybe_await(register_tmp(None))

    if not spec.is_photo:
        spec.thumb = await _fetch_small(http, item.thumb_url, "thumb.jpg")
    return handle, spec


async def relay_web_media(
    client: TelegramClient,
    items: list[WebMedia],
    relay_channel: int,
    caption_html: Optional[str] = None,
    on_progress: Optional[Callable[..., Any]] = None,
    task_id: Optional[int] = None,
    register_tmp: Optional[Callable[[Optional[str]], Any]] = None,
) -> tuple[list[int], int, str]:
    """把一组网络媒体搬进中转频道。

    caption_html 挂在第一项上（单个媒体就是它本身，相册就是封面那张），
    和 Telegram 客户端发相册时的习惯一致。

    返回 (消息 id 列表, 字节数, 说明)。
    """
    if not items:
        raise TransferError("没有可搬运的媒体")

    started = time.monotonic()
    handles, specs, total = [], [], 0
    async with http_session() as http:
        for i, item in enumerate(items):
            def _prog(phase: str, _i=i):
                def cb(done: int, tot: int) -> None:
                    if on_progress:
                        on_progress(phase, done, tot or 0,
                                    time.monotonic() - started, _i + 1, len(items))
                return cb
            log.info("网络媒体 %d/%d %s", i + 1, len(items), item.kind)
            h, sp = await _upload_web(client, http, item, i, _prog,
                                      task_id, register_tmp)
            handles.append(h)
            specs.append(sp)
            total += sp.size

    if caption_html:
        from telethon.extensions import html as tl_html
        specs[0].caption, specs[0].entities = tl_html.parse(caption_html)

    if len(handles) == 1:
        sent = await _send(client, relay_channel, handles[0], specs[0])
        return [sent.id], total, ""

    ids, note = await send_handles_as_album(client, relay_channel, handles, specs)
    return ids, total, note


async def send_handles_as_album(
    client: TelegramClient, relay_channel: int,
    handles: list, specs: list[MediaSpec],
) -> tuple[list[int], str]:
    """把已上传的句柄组装成相册发出去。受保护内容和推文两条路径共用。

    返回 (消息 id 列表, 说明)。整组失败时用已有句柄逐条补发，
    不重传字节，并在说明里告知分组没保住。
    """
    # Telegram 相册上限 10 项，超出按 10 分块。
    sent_ids: list[int] = []
    note = ""
    for chunk_start in range(0, len(handles), 10):
        hs = handles[chunk_start:chunk_start + 10]
        ss = specs[chunk_start:chunk_start + 10]
        try:
            multi = []
            for h, s in zip(hs, ss):
                im = await _to_input_media(client, h, s, relay_channel)
                multi.append(InputSingleMedia(
                    media=im,
                    random_id=helpers.generate_random_long(),
                    message=s.caption or "",
                    entities=s.entities or None,
                ))
            res = await client(SendMultiMediaRequest(
                peer=relay_channel, multi_media=multi))
            got = _ids_from_updates(res)
            if not got:
                raise TransferError("相册已发出但未能读回消息 id")
            sent_ids.extend(got)
        except TransferError:
            raise
        except Exception as e:  # noqa: BLE001
            # 字节已经传上去了，别浪费。退化成逐条发送，
            # 至少把内容给到用户，只是分组保不住。
            log.warning("相册整组发送失败，退化为逐条: %s", e)
            for h, s in zip(hs, ss):
                sent = await _send(client, relay_channel, h, s)
                sent_ids.append(sent.id)
            note = "相册整组发送失败，已逐条转存，分组未能保持。"

    return sorted(sent_ids), note


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
