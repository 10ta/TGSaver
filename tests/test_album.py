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

    def __init__(self, fail_multi=False):
        self.uploaded = []
        self.input_media = []
        self.sent_individually = []
        self.fail_multi = fail_multi
        self.multi_batches = []

    def iter_download(self, media, request_size=None, **kw):
        async def gen():
            yield b"x" * media.size
        return gen()

    async def upload_file(self, f, file_size=None, file_name=None,
                          progress_callback=None):
        if hasattr(f, "read"):
            data = await f.read(file_size or 0)
            self.uploaded.append((file_name, len(data)))
        else:
            self.uploaded.append((file_name, -1))
        return f"handle:{file_name}"

    async def download_media(self, msg, thumb=None, file=None, **kw):
        return None

    async def send_file(self, relay, file=None, **kw):
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
async def test_falls_back_to_individual_on_failure():
    """整组发送失败时退化为逐条，已上传的字节不能白费。"""
    msgs = [FakeMsg(1, 100, "a.mp4"), FakeMsg(2, 100, "b.mp4")]
    c = FakeClient(fail_multi=True)
    ids, total, note = await streamer.relay_protected_album(c, msgs, -100)

    assert len(c.sent_individually) == 2, "应逐条补发"
    assert len(ids) == 2
    assert "分组未能保持" in note
    # 关键：没有重新上传
    assert len(c.uploaded) == 2, f"不该重传，实际上传 {len(c.uploaded)} 次"


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
