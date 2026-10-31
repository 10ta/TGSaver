"""上传的某一块在服务器上丢了：FILE_PART_X_MISSING。

线上：一个约 1.7GB 的受保护文件，「重试 3 次仍失败：Part 3502 of the file is missing
from storage (caused by SendMediaRequest)」。每一块上传时服务器都回了「收到」，
拼文件时却说第 3502 块不在了。以前不认识这个错，整个任务从头重来，
1.7GB 重下重传三遍、三遍都撞上。

Telegram 给的处理办法是把那一块重新存一次再调用。这里用真实的 Telethon
（upload_file / send_file 全部真实执行，只截住网络发送）验证：只补传缺的那一块，
内容和原文件那一段一致，然后重发成功；补传次数有上限。
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
from telethon import TelegramClient  # noqa: E402
from telethon.errors import FilePartMissingError  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402
from telethon.tl import functions, types  # noqa: E402

import streamer  # noqa: E402

NOW = datetime.now(timezone.utc)
PEER = types.InputPeerChannel(channel_id=777, access_hash=1)
SIZE = 12 * 1024 * 1024 + 12345          # 超过 10MB：走 SaveBigFilePart，每块 128KB
PART = 128 * 1024
DATA = bytes((i * 7 + i // 977) % 251 for i in range(SIZE))


def _doc_msg():
    doc = types.Document(
        id=9, access_hash=1, file_reference=b"", date=NOW, mime_type="application/zip",
        size=SIZE, dc_id=1, attributes=[types.DocumentAttributeFilename(file_name="a.zip")])
    return types.Message(id=1, peer_id=types.PeerChannel(5), date=NOW, message="",
                         media=types.MessageMediaDocument(document=doc), noforwards=True)


class Client:
    """真实 TelegramClient。missing 是服务器依次报丢失的块号。"""

    def __init__(self, missing=(), where="send"):
        self.c = TelegramClient(StringSession(), 1, "0" * 32)
        self.c._call = self._call
        self.c.iter_download = self._iter_download
        self.missing = list(missing)
        self.where = where
        self.saved = {}             # 首次上传的块
        self.resaved = []           # (块号, 字节)
        self.sends = 0
        self.downloads = []

    def _iter_download(self, media, offset=0, request_size=512 * 1024, **kw):
        self.downloads.append(offset)

        async def gen():
            pos = offset
            while pos < SIZE:
                yield DATA[pos:pos + request_size]
                pos += request_size
        return gen()

    def _ups(self, random_id):
        return types.Updates(updates=[
            types.UpdateMessageID(id=50, random_id=random_id),
            types.UpdateNewChannelMessage(message=types.Message(
                id=50, peer_id=types.PeerChannel(777), date=NOW, message=""),
                pts=1, pts_count=1)], users=[], chats=[], date=NOW, seq=0)

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        if isinstance(request, (functions.upload.SaveBigFilePartRequest,
                                functions.upload.SaveFilePartRequest)):
            if request.file_part in self.saved and self.sends:
                self.resaved.append((request.file_part, request.bytes))
            else:
                self.saved[request.file_part] = request.bytes
            return True
        if isinstance(request, functions.messages.SendMediaRequest):
            self.sends += 1
            if self.where == "send" and self.missing:
                raise FilePartMissingError(request, capture=self.missing.pop(0))
            return self._ups(request.random_id)
        if isinstance(request, functions.messages.UploadMediaRequest):
            self.sends += 1
            if self.where == "upload_media" and self.missing:
                raise FilePartMissingError(request, capture=self.missing.pop(0))
            return types.MessageMediaDocument(document=types.Document(
                id=1, access_hash=1, file_reference=b"", date=NOW,
                mime_type="application/zip", size=SIZE, dc_id=1, attributes=[]))
        raise AssertionError(type(request).__name__)


@pytest.mark.asyncio
async def test_missing_part_resaved_then_sent():
    cl = Client(missing=[5])
    ids, nbytes = await streamer.relay_protected(cl.c, _doc_msg(), PEER)
    assert ids == [50] and nbytes == SIZE
    assert [p for p, _ in cl.resaved] == [5], "只补传缺的那一块"
    assert cl.resaved[0][1] == DATA[5 * PART:6 * PART], "补传的内容就是原文件那一段"
    assert cl.downloads == [0, 5 * PART], "补传只从原文件读那一段，不重下整个文件"


@pytest.mark.asyncio
async def test_last_part_shorter():
    last = (SIZE - 1) // PART
    cl = Client(missing=[last])
    await streamer.relay_protected(cl.c, _doc_msg(), PEER)
    assert cl.resaved[0][1] == DATA[last * PART:]


@pytest.mark.asyncio
async def test_several_parts_one_after_another():
    cl = Client(missing=[3, 40, 7])
    ids, _ = await streamer.relay_protected(cl.c, _doc_msg(), PEER)
    assert ids == [50] and [p for p, _ in cl.resaved] == [3, 40, 7]


@pytest.mark.asyncio
async def test_gives_up_after_limit():
    cl = Client(missing=[1] * (streamer.RESAVE_TRIES + 1))
    with pytest.raises(FilePartMissingError):
        await streamer.relay_protected(cl.c, _doc_msg(), PEER)
    assert len(cl.resaved) == streamer.RESAVE_TRIES


@pytest.mark.asyncio
async def test_album_upload_media_resaves():
    """相册路径：UploadMedia 时报丢块，同样补传。"""
    cl = Client(missing=[2], where="upload_media")
    spec = streamer.inspect_media(_doc_msg())
    spec.read_part = streamer._tg_reader(cl.c, _doc_msg().document)
    handle = await cl.c.upload_file(DATA, file_name="a.zip")
    im, kind = await streamer._to_input_media(cl.c, handle, spec, PEER)
    assert kind == "file" and [p for p, _ in cl.resaved] == [2]


@pytest.mark.asyncio
async def test_without_reader_raises_as_before():
    cl = Client(missing=[1])
    spec = streamer.inspect_media(_doc_msg())
    handle = await cl.c.upload_file(DATA, file_name="a.zip")
    with pytest.raises(FilePartMissingError):
        await streamer._send(cl.c, PEER, handle, spec)


# ================================================================ 网络媒体的读取

class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

        class C:
            async def iter_chunked(_, n):
                for i in range(0, len(body), n):
                    yield body[i:i + n]
        self.content = C()

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Http:
    def __init__(self, ranged):
        self.ranged = ranged
        self.headers = []

    def get(self, url, headers=None):
        self.headers.append(headers)
        if self.ranged:
            a, b = headers["Range"][6:].split("-")
            return _Resp(206, DATA[int(a):int(b) + 1])
        return _Resp(200, DATA)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("ranged", [True, False])
async def test_http_reader(monkeypatch, ranged):
    http = _Http(ranged)
    monkeypatch.setattr(streamer, "http_session", lambda: http)
    got = await streamer._http_reader("https://v")(3 * PART, PART)
    assert got == DATA[3 * PART:4 * PART]
    assert http.headers[0] == {"Range": f"bytes={3 * PART}-{4 * PART - 1}"}


@pytest.mark.asyncio
async def test_bytes_reader():
    assert await streamer._bytes_reader(b"abcdef")(2, 3) == b"cde"
