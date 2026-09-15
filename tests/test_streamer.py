"""验证有界管道：字节完整、分块精确、内存有界、错误可传播。"""
import asyncio
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# streamer 依赖 config，config 会校验 .env。这里注入假值。
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "x")
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("RELAY_CHANNEL_ID", "-1001")
os.environ.setdefault("SECRET_KEY", "x" * 43 + "=")

import pytest  # noqa: E402

from streamer import _DownloadStream, human_size  # noqa: E402


class FakeClient:
    """模拟 Telethon 的 iter_download。"""

    def __init__(self, data: bytes, chunk: int = 4096, fail_at: int = -1):
        self.data, self.chunk, self.fail_at = data, chunk, fail_at
        self.peak_outstanding = 0

    def iter_download(self, media, request_size=None, **kw):
        async def gen():
            sent = 0
            while sent < len(self.data):
                if 0 <= self.fail_at <= sent:
                    raise IOError("模拟网络中断")
                piece = self.data[sent:sent + self.chunk]
                sent += len(piece)
                yield piece
                await asyncio.sleep(0)
        return gen()


async def _drain(stream, part_size):
    out = bytearray()
    while True:
        b = await stream.read(part_size)
        if not b:
            break
        out.extend(b)
        if len(b) < part_size:
            break
    return bytes(out)


@pytest.mark.asyncio
@pytest.mark.parametrize("size,part", [
    (0, 4096),
    (1, 4096),
    (4095, 4096),
    (4096, 4096),
    (100_000, 32_768),
    (1_000_003, 524_288),   # 非整除，最后一块是零头
])
async def test_roundtrip_exact(size, part):
    data = os.urandom(size)
    c = FakeClient(data)
    async with _DownloadStream(c, None, len(data), "t.bin") as s:
        got = await _drain(s, part)
    assert got == data
    assert hashlib.md5(got).hexdigest() == hashlib.md5(data).hexdigest()


@pytest.mark.asyncio
async def test_read_returns_exact_part_size():
    """upload_file 要求每次拿到恰好 part_size，最后一块除外。"""
    data = os.urandom(300_000)
    part = 65_536
    c = FakeClient(data, chunk=7777)   # 故意用不整除的下载块
    sizes = []
    async with _DownloadStream(c, None, len(data), "t.bin") as s:
        while True:
            b = await s.read(part)
            if not b:
                break
            sizes.append(len(b))
    assert all(n == part for n in sizes[:-1]), sizes
    assert sizes[-1] == len(data) % part
    assert sum(sizes) == len(data)


@pytest.mark.asyncio
async def test_buffer_is_bounded():
    """10MB 数据，缓冲区任意时刻都不该接近全量。"""
    data = os.urandom(10 * 1024 * 1024)
    c = FakeClient(data, chunk=512 * 1024)
    peak = 0
    async with _DownloadStream(c, None, len(data), "t.bin") as s:
        while True:
            b = await s.read(524_288)
            peak = max(peak, len(s._buf) + s._q.qsize() * 512 * 1024)
            if not b:
                break
    assert peak < 24 * 1024 * 1024, f"缓冲峰值 {human_size(peak)} 超出预期"


@pytest.mark.asyncio
async def test_download_error_propagates():
    data = os.urandom(200_000)
    c = FakeClient(data, chunk=4096, fail_at=50_000)
    with pytest.raises(Exception) as ei:
        async with _DownloadStream(c, None, len(data), "t.bin") as s:
            await _drain(s, 65_536)
    assert "下载中断" in str(ei.value)


@pytest.mark.asyncio
async def test_progress_callback_monotonic():
    data = os.urandom(50_000)
    seen = []
    c = FakeClient(data, chunk=4096)
    async with _DownloadStream(c, None, len(data), "t.bin",
                               lambda d, t: seen.append(d)) as s:
        await _drain(s, 8192)
    assert seen == sorted(seen)
    assert seen[-1] == len(data)


def test_human_size():
    assert human_size(0) == "0B"
    assert human_size(1023) == "1023B"
    assert human_size(1024) == "1.0KB"
    assert human_size(349 * 1024 * 1024) == "349.0MB"
    assert human_size(2 * 1024 ** 3) == "2.0GB"
