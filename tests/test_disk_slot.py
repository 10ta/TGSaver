"""落盘名额：所有往 TMP_DIR 写大文件的路径共用一个信号量，并且先查余量。

以前只有单条消息的路径在调度层拿名额，相册成员、批量组装、大小未知的
网络媒体都会绕过它同时落盘；网络媒体还完全不查剩余空间。
"""
import asyncio
import os
import sys
from collections import namedtuple
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

import streamer  # noqa: E402

Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from config import CFG
    saved = {k: getattr(CFG, k) for k in ("tmp_dir", "disk_headroom", "disk_concurrency")}
    object.__setattr__(CFG, "tmp_dir", tmp_path)
    object.__setattr__(CFG, "disk_headroom", 1.2)
    object.__setattr__(CFG, "disk_concurrency", 1)
    monkeypatch.setattr(streamer, "_disk_sem", None)
    monkeypatch.setattr(streamer.shutil, "disk_usage", lambda p: Usage(0, 0, 10 ** 9))
    yield CFG
    for k, v in saved.items():
        object.__setattr__(CFG, k, v)


def _spec(size=1000):
    return streamer.MediaSpec(size=size, file_name="big.mkv", mime_type="video/x-matroska",
                              attributes=[], is_photo=False, force_document=True,
                              caption="", entities=[])


class _SlowClient:
    """下载时停一会儿，期间记下临时目录里同时有几个文件。"""

    def __init__(self, tmp_dir):
        self.tmp_dir = tmp_dir
        self.peak = 0

    async def download_media(self, msg, file, progress_callback=None):
        Path(file).write_bytes(b"x" * 100)
        self.peak = max(self.peak, len(list(self.tmp_dir.iterdir())))
        await asyncio.sleep(0.05)
        return file

    async def upload_file(self, path, file_name=None, progress_callback=None):
        return object()


def _prog(phase):
    return lambda d, t: None


async def test_album_members_take_turns_on_disk(cfg):
    """相册 / 批量组装走的 _upload_via_disk 也要排队，不能同时落盘。"""
    cl = _SlowClient(cfg.tmp_dir)
    msgs = [type("Msg", (), {"id": i})() for i in range(3)]
    await asyncio.gather(*(streamer._upload_via_disk(cl, m, _spec(), _prog, 1, None)
                           for m in msgs))
    assert cl.peak == 1
    assert list(cfg.tmp_dir.iterdir()) == []


async def test_single_and_album_share_the_slot(cfg, monkeypatch):
    async def fake_send(client, relay, handle, spec):
        return type("Sent", (), {"id": 1})()
    monkeypatch.setattr(streamer, "_send", fake_send)
    cl = _SlowClient(cfg.tmp_dir)
    a, b = type("Msg", (), {"id": 1})(), type("Msg", (), {"id": 2})()
    await asyncio.gather(
        streamer._via_disk(cl, a, _spec(), -100, _prog, 1, None),
        streamer._upload_via_disk(cl, b, _spec(), _prog, 2, None))
    assert cl.peak == 1


async def test_not_enough_space_fails_before_writing(cfg, monkeypatch):
    monkeypatch.setattr(streamer.shutil, "disk_usage", lambda p: Usage(0, 0, 1000))
    cl = _SlowClient(cfg.tmp_dir)
    with pytest.raises(streamer.TransferError, match="磁盘空间不足"):
        await streamer._upload_via_disk(cl, type("Msg", (), {"id": 1})(), _spec(1000),
                                        _prog, 1, None)
    assert cl.peak == 0
    assert list(cfg.tmp_dir.iterdir()) == []


class _Resp:
    status = 200
    content_length = None          # 大小未知，只能先落盘

    def __init__(self, chunks):
        self.content = self
        self._chunks = chunks

    async def iter_chunked(self, n):
        for c in self._chunks:
            yield c

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Http:
    def __init__(self, chunks):
        self.chunks = chunks

    def get(self, url, **kw):
        return _Resp(self.chunks)


async def test_unknown_size_web_media_stops_when_disk_runs_out(cfg, monkeypatch):
    monkeypatch.setattr(streamer.shutil, "disk_usage", lambda p: Usage(0, 0, 2500))
    item = streamer.WebMedia(kind="video", url="https://example.com/v.mp4")
    http = _Http([b"x" * 1000] * 5)       # 5000 字节，余量只有 2500
    with pytest.raises(streamer.TransferError, match="磁盘空间不足"):
        await streamer._upload_web(object(), http, item, 0, _prog, 1, None)
    assert list(cfg.tmp_dir.iterdir()) == []


async def test_unknown_size_web_media_fits(cfg):
    class Up:
        async def upload_file(self, path, file_name=None, progress_callback=None):
            assert Path(path).stat().st_size == 3000
            return object()
    item = streamer.WebMedia(kind="photo", url="https://example.com/p.jpg")
    handle, spec = await streamer._upload_web(Up(), _Http([b"x" * 1000] * 3), item, 0,
                                              _prog, 1, None)
    assert spec.size == 3000
    assert list(cfg.tmp_dir.iterdir()) == []
