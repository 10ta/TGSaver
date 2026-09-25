"""由 user 账号生成消息的几个函数，用真实的 Telethon 客户端测。

之前这几个函数的测试都把函数本身替换掉了，真正构造请求的代码一行都没执行，
结果有两个错没被发现：
  - SendMessageRequest 不收 media 参数（要用 SendMediaRequest）——线上已触发
  - 带说明的相册只给了一份扁平的实体列表，Telethon 把它当成第 1 项的，
    其余各项拿到 None，内部 `for ent in None` 崩掉——潜伏，fw + 多图 +
    Telegram 能直接拉取时必然触发

这里只替换最底层的网络发送（client._call），Telethon 自己的参数校验、
相册组装、TL 请求构造全部真实执行。再有这类问题，在这里就会炸。
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
from telethon.sessions import StringSession  # noqa: E402
from telethon.tl import functions, types  # noqa: E402

import streamer  # noqa: E402
from streamer import WebMedia  # noqa: E402

CHAN = 1234567
PEER = types.InputPeerChannel(channel_id=CHAN, access_hash=1)


def _now():
    return datetime.now(timezone.utc)


def _updates(random_ids, text=""):
    ups = []
    for i, rid in enumerate(random_ids):
        mid = 1000 + i
        ups.append(types.UpdateMessageID(id=mid, random_id=rid))
        ups.append(types.UpdateNewChannelMessage(
            message=types.Message(id=mid, peer_id=types.PeerChannel(CHAN),
                                  date=_now(), message=text),
            pts=1, pts_count=1))
    return types.Updates(updates=ups, users=[], chats=[], date=_now(), seq=0)


class RealClient:
    """真实 TelegramClient，只截住最底层的网络发送。"""

    def __init__(self):
        self.client = TelegramClient(StringSession(), 1, "0" * 32)
        self.requests = []
        self.client._call = self._call

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        self.requests.append(request)
        if isinstance(request, functions.messages.UploadMediaRequest):
            if isinstance(request.media, types.InputMediaPhotoExternal):
                return types.MessageMediaPhoto(photo=types.Photo(
                    id=1, access_hash=1, file_reference=b"", date=_now(),
                    sizes=[], dc_id=1))
            return types.MessageMediaDocument(document=types.Document(
                id=1, access_hash=1, file_reference=b"", date=_now(),
                mime_type="video/mp4", size=1, dc_id=1, attributes=[]))
        if isinstance(request, functions.messages.SendMultiMediaRequest):
            return _updates([m.random_id for m in request.multi_media])
        if isinstance(request, (functions.messages.SendMediaRequest,
                                functions.messages.SendMessageRequest)):
            return _updates([request.random_id])
        raise AssertionError(f"没想到会发 {type(request).__name__}")

    def of(self, cls):
        return [r for r in self.requests if isinstance(r, cls)]


# ================================================================ 带预览的文字

@pytest.mark.asyncio
async def test_preview_uses_send_media_not_send_message():
    """线上那个错：预览必须用 sendMedia，sendMessage 不收 media。"""
    rc = RealClient()
    ids = await streamer.send_text_message(
        rc.client, PEER, "<blockquote><b>J</b> : hi</blockquote>",
        preview_url="https://fxtwitter.com/j/status/12")
    assert ids == [1000]
    req = rc.of(functions.messages.SendMediaRequest)[0]
    assert isinstance(req.media, types.InputMediaWebPage)
    assert req.media.url == "https://fxtwitter.com/j/status/12"
    assert req.media.force_large_media is True
    assert req.invert_media is True, "预览要在正文上方"
    assert req.message == "J : hi"
    assert any(isinstance(e, types.MessageEntityBlockquote) for e in req.entities)
    assert not rc.of(functions.messages.SendMessageRequest)


@pytest.mark.asyncio
async def test_plain_text_disables_preview():
    rc = RealClient()
    await streamer.send_text_message(rc.client, PEER, "<b>J</b> : hi")
    req = rc.of(functions.messages.SendMessageRequest)[0]
    assert req.no_webpage is True


# ================================================================ 外部媒体

@pytest.mark.asyncio
async def test_single_external_photo_with_caption():
    rc = RealClient()
    ids = await streamer.send_external_media(
        rc.client, [WebMedia("photo", "https://pbs.twimg.com/a.jpg")], PEER,
        caption_html="<b>J</b> : hi")
    assert ids == [1000]
    req = rc.of(functions.messages.SendMediaRequest)[0]
    assert isinstance(req.media, types.InputMediaPhotoExternal), "URL 交给 Telegram 拉"
    assert req.message == "J : hi"


@pytest.mark.asyncio
async def test_single_external_without_caption():
    rc = RealClient()
    await streamer.send_external_media(
        rc.client, [WebMedia("photo", "https://pbs.twimg.com/a.jpg")], PEER)
    assert rc.of(functions.messages.SendMediaRequest)[0].message == ""


@pytest.mark.asyncio
async def test_album_external_without_caption():
    """不带说明的相册。（Telethon 会把 None 规整成 []，这条本身不崩，留作回归。）"""
    rc = RealClient()
    ids = await streamer.send_external_media(
        rc.client,
        [WebMedia("photo", f"https://pbs.twimg.com/{i}.jpg") for i in range(3)],
        PEER)
    assert ids == [1000, 1001, 1002]
    multi = rc.of(functions.messages.SendMultiMediaRequest)[0].multi_media
    assert len(multi) == 3 and all(m.message == "" for m in multi)


@pytest.mark.asyncio
async def test_album_external_caption_on_first_only():
    """潜伏的那个错：带说明的相册，实体必须每项一份，否则第 2 项起拿到 None 崩掉。"""
    rc = RealClient()
    await streamer.send_external_media(
        rc.client,
        [WebMedia("photo", "https://pbs.twimg.com/a.jpg"),
         WebMedia("video", "https://video.twimg.com/b.mp4")],
        PEER, caption_html="<blockquote><b>J</b> 1-2 : hi</blockquote>")
    multi = rc.of(functions.messages.SendMultiMediaRequest)[0].multi_media
    assert multi[0].message == "J 1-2 : hi"
    assert any(isinstance(e, types.MessageEntityBlockquote)
               for e in multi[0].entities)
    assert multi[1].message == "" and not multi[1].entities, "说明只挂第一项"


@pytest.mark.asyncio
async def test_album_external_uses_url_not_upload():
    """相册里每一项都是外部 URL，不经过本机上传。"""
    rc = RealClient()
    await streamer.send_external_media(
        rc.client,
        [WebMedia("photo", "https://pbs.twimg.com/a.jpg"),
         WebMedia("photo", "https://pbs.twimg.com/b.jpg")], PEER)
    uploads = rc.of(functions.messages.UploadMediaRequest)
    assert uploads and all(
        isinstance(u.media, (types.InputMediaPhotoExternal,
                             types.InputMediaDocumentExternal))
        for u in uploads)
    assert not rc.of(functions.upload.SaveFilePartRequest)
    assert not rc.of(functions.upload.SaveBigFilePartRequest)
