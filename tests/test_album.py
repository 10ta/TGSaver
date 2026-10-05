"""受保护相册整组发送。

重点验证三件事：
1. 逐项属性（文件名、视频时长分辨率、缩略图、剧透）不丢 ——
   这正是不能用 Telethon 自带 _send_album 的原因，那条路径不传 attributes
2. 整组发送失败时能降级为逐条，已上传的字节不浪费
3. 超过 10 项时按 Telegram 上限分块
"""
import os
import sys
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
from telethon.tl.types import (  # noqa: E402
    DocumentAttributeFilename,
    DocumentAttributeVideo,
)

import streamer  # noqa: E402


# ------------------------------------------------- 假对象

class FakeDoc:
    def __init__(self, size, name, video=False, mime="video/mp4"):
        self.size = size
        self.mime_type = mime
        self.attributes = [DocumentAttributeFilename(file_name=name)]
        if video:
            self.attributes.append(DocumentAttributeVideo(
                duration=42, w=1920, h=1080, supports_streaming=False))
        self.thumbs = None


class FakeMsg:
    def __init__(self, mid, size, name, video=True, spoiler=False):
        self.id = mid
        self.message = f"caption {mid}"
        self.entities = []
        self.photo = None
        self.document = FakeDoc(size, name, video)
        self.media = type("M", (), {"spoiler": spoiler})()
        self.grouped_id = 999


class FakeClient:
    """记录每一步调用，供断言。"""

    def __init__(self, fail_multi=False, fail_single=()):
        self.uploaded = []
        self.input_media = []
        self.sent_individually = []
        self.sent_by_ref = []
        self.fail_multi = fail_multi
        self.fail_single = set(fail_single)
        self.multi_batches = []
        self._single_n = 0

    def iter_download(self, media, request_size=None, **kw):
        async def gen():
            yield b"x" * media.size
        return gen()

    async def upload_file(self, f, file_size=None, file_name=None,
                          progress_callback=None):
        if hasattr(f, "read") and not hasattr(f, "getvalue"):
            data = await f.read(file_size or 0)
            self.uploaded.append((file_name, len(data)))
        elif hasattr(f, "getvalue"):
            # 内存里的缩略图：像真实的 Telethon 一样从当前位置读，读空就报错
            data = f.read()
            if not data:
                raise RuntimeError("FilePartEmpty: The provided file part is empty")
            self.uploaded.append(("thumb", len(data)))
        else:
            self.uploaded.append((file_name, -1))
        return f"handle:{file_name}"

    async def download_media(self, msg, thumb=None, file=None, **kw):
        return None

    async def send_file(self, relay, file=None, **kw):
        thumb = kw.get("thumb")
        if thumb is not None:
            await self.upload_file(thumb)       # 真实的 send_file 会上传缩略图
        self.sent_individually.append((file, kw))
        return type("S", (), {"id": 500 + len(self.sent_individually)})()

    async def __call__(self, request):
        name = type(request).__name__
        if name == "UploadMediaRequest":
            self.input_media.append(request.media)
            # 真实的 UploadMedia 返回 MessageMediaDocument / MessageMediaPhoto，
            # 这里用等价形状，确保 _to_input_media 取的字段和线上一致。
            if isinstance(request.media, InputMediaUploadedPhoto):
                return MessageMediaPhoto(photo=_FakePhoto())
            return MessageMediaDocument(document=_FakeRealDoc())
        if name == "SendMediaRequest":
            i = self._single_n
            self._single_n += 1
            if i in self.fail_single:
                raise RuntimeError("MEDIA_EMPTY")
            self.sent_by_ref.append(request.media)
            msg = type("M", (), {"id": 800 + i})()
            return type("R", (), {"updates": [_FakeUpdate(msg)]})()
        if name == "SendMultiMediaRequest":
            self.multi_batches.append(request.multi_media)
            if self.fail_multi:
                raise RuntimeError("模拟整组发送失败")
            ups = []
            for i, _ in enumerate(request.multi_media):
                msg = type("M", (), {"id": 900 + i})()
                ups.append(_FakeUpdate(msg))
            return type("R", (), {"updates": ups})()
        raise AssertionError(f"未预期的请求 {name}")


from telethon.tl.types import (  # noqa: E402
    Document,
    InputMediaUploadedPhoto,
    MessageMediaDocument,
    MessageMediaPhoto,
    Photo,
    UpdateNewChannelMessage,
)


def _FakeRealDoc():
    return Document(id=1, access_hash=2, file_reference=b"", date=None,
                    mime_type="video/mp4", size=100, dc_id=2,
                    attributes=[])


def _FakePhoto():
    return Photo(id=1, access_hash=2, file_reference=b"", date=None,
                 sizes=[], dc_id=2, has_stickers=False)


class _FakeUpdate(UpdateNewChannelMessage):
    def __init__(self, message):
        self.message = message
        self.pts = 0
        self.pts_count = 0


@pytest.fixture(autouse=True)
def _no_disk(monkeypatch):
    """强制走流式路径，避免测试碰磁盘。"""
    from config import CFG
    object.__setattr__(CFG, "stream_max_size", 0)
    object.__setattr__(CFG, "max_upload_size", 2 * 1024 ** 3)


# ------------------------------------------------- 测试

@pytest.mark.asyncio
async def test_album_sends_as_one_group():
    msgs = [FakeMsg(1, 1000, "a.mp4"), FakeMsg(2, 2000, "b.mp4"),
            FakeMsg(3, 3000, "c.mp4")]
    c = FakeClient()
    ids, total, note = await streamer.relay_protected_album(c, msgs, -100)

    assert len(c.multi_batches) == 1, "三项应合成一次 SendMultiMedia"
    assert len(c.multi_batches[0]) == 3
    assert len(ids) == 3
    assert total == 6000
    assert note == ""
    assert c.sent_individually == [], "不该退化成逐条"


@pytest.mark.asyncio
async def test_per_item_attributes_preserved():
    """核心断言：每一项都带着自己的文件名和视频属性。

    Telethon 的 _send_album 不传 attributes，用它这些全会丢。
    """
    msgs = [FakeMsg(1, 1000, "first.mp4"), FakeMsg(2, 2000, "second.mp4")]
    c = FakeClient()
    await streamer.relay_protected_album(c, msgs, -100)

    names = []
    for im in c.input_media:
        for a in im.attributes:
            if isinstance(a, DocumentAttributeFilename):
                names.append(a.file_name)
    assert names == ["first.mp4", "second.mp4"], f"文件名丢失: {names}"

    for im in c.input_media:
        vids = [a for a in im.attributes if isinstance(a, DocumentAttributeVideo)]
        assert vids, "视频属性丢失"
        assert vids[0].duration == 42
        assert vids[0].w == 1920 and vids[0].h == 1080
        assert vids[0].supports_streaming, "应补齐为可流式播放"


@pytest.mark.asyncio
async def test_captions_are_per_item():
    msgs = [FakeMsg(1, 100, "a.mp4"), FakeMsg(2, 100, "b.mp4")]
    c = FakeClient()
    await streamer.relay_protected_album(c, msgs, -100)
    caps = [sm.message for sm in c.multi_batches[0]]
    assert caps == ["caption 1", "caption 2"]


@pytest.mark.asyncio
async def test_spoiler_always_stripped():
    """剧透遮罩一律去掉：原消息带 spoiler，副本也不该带。"""
    msgs = [FakeMsg(1, 100, "a.mp4", spoiler=True),
            FakeMsg(2, 100, "b.mp4", spoiler=False)]
    c = FakeClient()
    await streamer.relay_protected_album(c, msgs, -100)
    assert all(not getattr(im, "spoiler", False) for im in c.input_media)


@pytest.mark.asyncio
async def test_falls_back_using_already_uploaded_media():
    """整组发送失败时，直接用已经在服务器上的媒体逐条发，不重传、不重读缩略图。"""
    msgs = [FakeMsg(1, 100, "a.mp4"), FakeMsg(2, 100, "b.mp4")]
    c = FakeClient(fail_multi=True)
    ids, total, note = await streamer.relay_protected_album(c, msgs, -100)

    assert len(c.sent_by_ref) == 2, "应直接用服务器上的媒体逐条发"
    assert c.sent_individually == [], "不该走重新上传"
    assert len([u for u in c.uploaded if u[0] != "thumb"]) == 2, "文件只传过一次"
    assert len(ids) == 2 and "分组未能保持" in note


@pytest.mark.asyncio
async def test_chunks_at_ten():
    """Telegram 相册上限 10 项，超出要分块。"""
    msgs = [FakeMsg(i, 100, f"f{i}.mp4") for i in range(12)]
    c = FakeClient()
    await streamer.relay_protected_album(c, msgs, -100)
    assert len(c.multi_batches) == 2
    assert [len(b) for b in c.multi_batches] == [10, 2]


@pytest.mark.asyncio
async def test_oversize_item_rejects_whole_album():
    from config import CFG
    object.__setattr__(CFG, "max_upload_size", 1500)
    msgs = [FakeMsg(1, 100, "small.mp4"), FakeMsg(2, 99999, "huge.mp4")]
    c = FakeClient()
    with pytest.raises(streamer.TransferError) as ei:
        await streamer.relay_protected_album(c, msgs, -100)
    assert "huge.mp4" in str(ei.value)
    assert c.uploaded == [], "超限时不该先传一半再失败"


@pytest.mark.asyncio
async def test_progress_reports_item_index():
    msgs = [FakeMsg(1, 100, "a.mp4"), FakeMsg(2, 100, "b.mp4")]
    c = FakeClient()
    seen = []
    await streamer.relay_protected_album(
        c, msgs, -100,
        on_progress=lambda phase, d, t, el, i, n: seen.append((i, n)))
    assert seen, "应有进度回调"
    assert max(i for i, _ in seen) == 2
    assert all(n == 2 for _, n in seen)


@pytest.mark.asyncio
async def test_photo_album_uses_photo_branch():
    """照片走 InputMediaUploadedPhoto，取 r.photo 而不是 r.document。"""
    class PhotoMsg(FakeMsg):
        def __init__(self, mid, spoiler=False):
            super().__init__(mid, 500, "p.jpg", video=False, spoiler=spoiler)
            self.document = None
            self.photo = type("P", (), {"size": 500})()
            self.file = type("F", (), {"size": 500, "name": "p.jpg"})()

    c = FakeClient()
    ids, total, note = await streamer.relay_protected_album(
        c, [PhotoMsg(1), PhotoMsg(2)], -100)
    assert all(isinstance(im, InputMediaUploadedPhoto) for im in c.input_media)
    assert len(ids) == 2
    assert note == ""



# ================================================================ 这次线上的 bug

class ThumbMsg(FakeMsg):
    """带缩略图的视频，复现线上那条：相册被拒 -> 退回逐条 -> 缩略图读空。"""


@pytest.mark.asyncio
async def test_thumbnail_reusable_after_album_failure(monkeypatch):
    """线上报错：相册整组被拒后退回逐条，缩略图在组相册时已上传过一次，
    内存流读到了末尾；重传时读出 0 字节，Telegram 报 FilePartEmpty。"""
    import io

    async def fake_thumb(client, msg):
        b = io.BytesIO(b"J" * 500)
        b.name = "thumb.jpg"
        return b
    monkeypatch.setattr(streamer, "fetch_thumb", fake_thumb)

    msgs = [FakeMsg(1, 100, "a.mp4"), FakeMsg(2, 100, "b.mp4")]
    # 整组失败，且第 2 项单独发也失败 -> 第 2 项必须重新上传（带缩略图）
    c = FakeClient(fail_multi=True, fail_single={1})
    ids, _, _ = await streamer.relay_protected_album(c, msgs, -100)
    assert len(ids) == 2
    assert len(c.sent_individually) == 1, "只有失败的那一项重传"
    thumbs = [u for u in c.uploaded if u[0] == "thumb"]
    assert thumbs and all(n == 500 for _, n in thumbs), "缩略图每次都应读到完整内容"


@pytest.mark.asyncio
async def test_send_rewinds_thumbnail():
    import io
    t = io.BytesIO(b"J" * 300)
    t.name = "thumb.jpg"
    t.read()                                    # 模拟已经被读过一次
    spec = streamer.MediaSpec(size=1, file_name="v.mp4", mime_type="video/mp4",
                              attributes=[], is_photo=False, force_document=False,
                              caption="", entities=[], thumb=t)
    c = FakeClient()
    await streamer._send(c, -100, "h", spec)
    assert ("thumb", 300) in c.uploaded
