"""受保护照片的大小对不上。

线上报错：「重试 3 次仍失败：read less than 131072 before reaching the end;
either `file_size` or `read` are wrong」，私聊里一组图片 + 一个短视频。

原因：流式上传要预先声明字节数。照片的大小取自 msg.file.size —— Telethon
算的是所有尺寸里**最重**的那个；而 iter_download(photo) 下载的是尺寸列表里
**最后一个**。两者不是同一个尺寸时，实际读到的字节比声明的少，upload_file
读到末尾不够数就报这个错，重试多少次都一样。

这里用真实的 Telethon upload_file（只截住网络发送），复现这个错，并验证修复后：
照片整张下载、按实际大小上传，下载的是最大的那个尺寸。
"""
import os
import sys
from datetime import datetime, timezone
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
from telethon import TelegramClient, utils  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402
from telethon.tl import functions, types  # noqa: E402

import streamer  # noqa: E402

NOW = datetime.now(timezone.utc)
PEER = types.InputPeerChannel(channel_id=777, access_hash=1)

HEAVY = 300_000      # 最大的尺寸（渐进式 y）
LAST = 100_000       # 列表里最后一个尺寸（x），iter_download 下载的是它


def _photo():
    return types.Photo(
        id=1, access_hash=1, file_reference=b"", date=NOW, dc_id=1, sizes=[
            types.PhotoStrippedSize(type="i", bytes=b"\x01\x02\x03"),
            types.PhotoSize(type="m", w=320, h=320, size=20_000),
            types.PhotoSizeProgressive(type="y", w=1280, h=1280,
                                       sizes=[30_000, 120_000, HEAVY]),
            types.PhotoSize(type="x", w=800, h=800, size=LAST),
        ])


def _msg(mid=1):
    return types.Message(id=mid, peer_id=types.PeerUser(5), date=NOW, message="",
                         media=types.MessageMediaPhoto(photo=_photo()), noforwards=True)


class Client:
    """真实 TelegramClient：upload_file / send_file 的逻辑全部真实执行。

    下载按 Telethon 的真实语义模拟：iter_download(photo) 下最后一个尺寸，
    download_media(msg) 下最大的尺寸。
    """

    def __init__(self):
        self.c = TelegramClient(StringSession(), 1, "0" * 32)
        self.c._call = self._call
        self.parts = 0
        self.c.iter_download = self._iter_download
        self.c.download_media = self._download_media

    def _iter_download(self, media, request_size=None, **kw):
        n = utils._photo_size_byte_count(media.sizes[-1])

        async def gen():
            left = n
            while left > 0:
                k = min(left, request_size or 131072)
                left -= k
                yield b"x" * k
        return gen()

    async def _download_media(self, msg, file=None, thumb=None, progress_callback=None):
        if thumb is not None:
            return None
        assert file is bytes
        return b"x" * HEAVY

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        if isinstance(request, (functions.upload.SaveFilePartRequest,
                                functions.upload.SaveBigFilePartRequest)):
            self.parts += 1
            return True
        if isinstance(request, functions.messages.SendMediaRequest):
            self.sent = request
            return types.Updates(updates=[
                types.UpdateMessageID(id=50, random_id=request.random_id),
                types.UpdateNewChannelMessage(message=types.Message(
                    id=50, peer_id=types.PeerChannel(777), date=NOW, message=""),
                    pts=1, pts_count=1)], users=[], chats=[], date=NOW, seq=0)
        raise AssertionError(type(request).__name__)


def test_sizes_really_disagree():
    """前提：Telethon 报的大小和 iter_download 会下载的大小不是同一个。"""
    from telethon.tl.custom.file import File
    assert File(_photo()).size == HEAVY
    assert utils._get_file_info(_photo()).size == LAST


@pytest.mark.asyncio
async def test_old_streaming_way_fails_like_production():
    """复现线上的错：按最重的尺寸声明大小，却流式下载最后一个尺寸。"""
    cl = Client()
    with pytest.raises(ValueError, match="read less than"):
        async with streamer._DownloadStream(cl.c, _photo(), HEAVY, "p.jpg") as s:
            await cl.c.upload_file(s, file_size=HEAVY, file_name="p.jpg")


@pytest.mark.asyncio
async def test_protected_photo_relays():
    cl = Client()
    ids, nbytes = await streamer.relay_protected(cl.c, _msg(), PEER)
    assert ids == [50]
    assert nbytes == HEAVY, "按实际下载到的字节记账"
    assert isinstance(cl.sent.media, types.InputMediaUploadedPhoto)


@pytest.mark.asyncio
async def test_protected_photo_album_upload():
    cl = Client()
    spec = streamer.inspect_media(_msg())
    handle = await streamer._upload_one(cl.c, _msg(), spec, PEER,
                                        lambda phase: None, None, None)
    assert handle.size == HEAVY and spec.size == HEAVY
