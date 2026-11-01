"""落盘临时文件：路径不受远端文件名影响，启动时清干净。

以前的路径里拼了远端文件名。名字没有扩展名时 Telethon 会自己补一个
（README → README.mp4），代码再按原路径 stat / 删除就对不上：任务失败，
每次重试都留下一个没登记的大文件。
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
from telethon.client.downloads import DownloadMethods  # noqa: E402

import streamer  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    from config import CFG
    old = CFG.tmp_dir
    object.__setattr__(CFG, "tmp_dir", tmp_path)
    object.__setattr__(CFG, "disk_headroom", 1.0)
    yield CFG
    object.__setattr__(CFG, "tmp_dir", old)


def test_path_is_not_rewritten_by_telethon(cfg):
    p = str(streamer.tmp_path("t", 7, 123))
    assert DownloadMethods._get_proper_filename(p, "document", ".mp4") == p
    assert Path(p).parent == cfg.tmp_dir


class _Client:
    """download_media 按 Telethon 的规则决定真正写到哪里。"""

    def __init__(self):
        self.uploaded_from = None

    async def download_media(self, msg, file, progress_callback=None):
        real = DownloadMethods._get_proper_filename(file, "document", ".mp4")
        Path(real).write_bytes(b"x" * 1000)
        return real

    async def upload_file(self, path, file_name=None, progress_callback=None):
        assert Path(path).is_file(), "上传时文件应该在"
        self.uploaded_from = path
        return object()


@pytest.mark.parametrize("name", ["README", "a/b.mp4", "movie.mp4", "..", "x/../../y"])
async def test_disk_path_ignores_remote_name(cfg, monkeypatch, name):
    async def fake_send(client, relay, handle, spec):
        return type("Sent", (), {"id": 5})()
    monkeypatch.setattr(streamer, "_send", fake_send)

    msg = type("Msg", (), {"id": 123})()
    spec = streamer.MediaSpec(size=1000, file_name=name, mime_type="video/mp4",
                              attributes=[], is_photo=False, force_document=False,
                              caption="", entities=[])
    cl = _Client()
    prog = lambda phase: (lambda d, t: None)  # noqa: E731
    sent = await streamer._via_disk(cl, msg, spec, -100, prog, 7, None)

    assert sent.id == 5
    assert spec.file_name == name                    # 上传用的名字不变
    assert list(cfg.tmp_dir.iterdir()) == [], "临时文件没清理"


async def test_upload_via_disk_cleans_up(cfg):
    msg = type("Msg", (), {"id": 9})()
    spec = streamer.MediaSpec(size=1000, file_name="README", mime_type=None,
                              attributes=[], is_photo=False, force_document=True,
                              caption="", entities=[])
    cl = _Client()
    await streamer._upload_via_disk(cl, msg, spec, lambda p: (lambda d, t: None), 3, None)
    assert cl.uploaded_from is not None
    assert list(cfg.tmp_dir.iterdir()) == []


def test_clear_tmp_dir_only_removes_own_files(cfg):
    own = ["t12_345.part", "w3_0.part", "t5_678_old name.mp4", "w9_1"]
    other = ["notes.txt", "tgsaver.db", "t_no_digits"]
    for n in own + other:
        (cfg.tmp_dir / n).write_text("x")
    (cfg.tmp_dir / "t1_dir").mkdir()

    assert streamer.clear_tmp_dir() == len(own)
    left = sorted(p.name for p in cfg.tmp_dir.iterdir())
    assert left == sorted(other + ["t1_dir"])


def test_clear_tmp_dir_missing_dir(cfg):
    object.__setattr__(cfg, "tmp_dir", cfg.tmp_dir / "nope")
    assert streamer.clear_tmp_dir() == 0
