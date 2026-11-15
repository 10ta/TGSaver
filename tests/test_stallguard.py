"""卡死看门狗。

线上：抓取 10 条，状态停在「正在搬运 1/10…」十个小时，任务一直 running，不报错。
Telethon 的单个请求没有总超时，连到别的数据中心下载的那条连接半死不活时会永远等下去。
看门狗只看「有没有进展」：一直在动的两小时大文件不打断，一动不动超过 STALL_TIMEOUT 就中断、
说清卡在哪、换条新连接再重试。
"""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "h")
os.environ.setdefault("BOT_TOKEN", "t")
os.environ.setdefault("OWNER_ID", "42")
os.environ.setdefault("RELAY_CHANNEL_ID", "-1001111111111")
os.environ.setdefault("SECRET_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402

import stallguard  # noqa: E402
from stallguard import Stalled, guard  # noqa: E402


class GetFileRequest:     # 名字像 Telethon 的请求就行
    pass


async def _telethon_like_call():
    request = GetFileRequest()  # noqa: F841  看门狗从帧的局部变量里读请求类型
    await asyncio.Event().wait()                       # 永远不返回


async def _upload_photo_like():
    await _telethon_like_call()


# ================================================================ 基本行为

@pytest.mark.asyncio
async def test_normal_result_passes_through():
    async def work():
        await asyncio.sleep(0.01)
        return 42
    assert await guard(work(), 1) == 42


@pytest.mark.asyncio
async def test_exceptions_pass_through():
    async def work():
        raise ValueError("原样抛出")
    with pytest.raises(ValueError, match="原样抛出"):
        await guard(work(), 1)


@pytest.mark.asyncio
async def test_stuck_is_interrupted_with_location():
    cleaned = []

    async def work():
        try:
            await _upload_photo_like()
        finally:
            cleaned.append(True)          # 临时文件、名额之类的清理照常执行

    with pytest.raises(Stalled) as ei:
        await guard(work(), 0.3, label="task=1")
    assert ei.value.where == "_telethon_like_call → GetFileRequest", \
        "说清卡在我们哪一步、哪个 Telegram 请求上"
    assert "卡住" in str(ei.value)
    await asyncio.sleep(0)
    assert cleaned == [True]


@pytest.mark.asyncio
async def test_progress_keeps_long_transfer_alive():
    """总时长远超超时，但一直在动：不打断。"""
    async def work():
        for _ in range(12):
            await asyncio.sleep(0.05)
            stallguard.touch()
        return "完成"
    assert await guard(work(), 0.2) == "完成"


@pytest.mark.asyncio
async def test_touch_from_child_task_counts():
    """下载管道的生产者是另起的任务，它报的进展也要算。"""
    async def producer():
        for _ in range(8):
            await asyncio.sleep(0.05)
            stallguard.touch()

    async def work():
        await asyncio.create_task(producer())
        return "ok"
    assert await guard(work(), 0.2) == "ok"


@pytest.mark.asyncio
async def test_outer_cancel_cancels_inner():
    """/killall 取消 worker 时，被看管的任务也要停。"""
    stopped = []

    async def work():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.append(True)
            raise

    t = asyncio.create_task(guard(work(), 5))
    await asyncio.sleep(0.05)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    await asyncio.sleep(0)
    assert stopped == [True]


@pytest.mark.asyncio
async def test_disabled_when_zero():
    async def work():
        await asyncio.sleep(0.3)
        return 1
    assert await guard(work(), 0) == 1


def test_touch_outside_guard_is_harmless():
    stallguard.touch()


# ================================================================ 等磁盘名额不算卡死

@pytest.mark.asyncio
async def test_waiting_for_disk_slot_is_not_a_stall(monkeypatch, tmp_path):
    import streamer
    from config import CFG
    object.__setattr__(CFG, "tmp_dir", tmp_path)
    monkeypatch.setattr(streamer, "DISK_WAIT_TICK", 0.05)
    monkeypatch.setattr(streamer, "_disk_sem", asyncio.Semaphore(1))
    release = asyncio.Event()

    async def holder():
        async with streamer.disk_slot(0):
            await release.wait()

    async def waiter():
        async with streamer.disk_slot(0):
            return "拿到名额"

    h = asyncio.create_task(holder())
    await asyncio.sleep(0.01)
    asyncio.get_running_loop().call_later(0.6, release.set)
    assert await guard(waiter(), 0.2) == "拿到名额"
    await h


# ================================================================ 接进调度器

@pytest.mark.asyncio
async def test_runner_interrupts_retires_connection_and_retries(monkeypatch):
    import db
    import taskqueue
    from config import CFG

    object.__setattr__(CFG, "stall_timeout", 0.3)
    said, retired, requeued = [], [], []

    async def nop(*a, **kw):
        return None

    async def get_prefs(uid):
        import prefs
        return prefs.defaults()

    class Pool:
        async def retire(self, uid):
            retired.append(uid)

    monkeypatch.setattr(db, "update_task", nop)
    monkeypatch.setattr(taskqueue.prefs, "get", get_prefs)
    monkeypatch.setattr(taskqueue, "POOL", Pool())

    r = taskqueue.Runner.__new__(taskqueue.Runner)

    async def say(job, text):
        said.append(text)
    r._say = say
    r._requeue_later = lambda job, lane, delay: requeued.append((job.task_id, lane))

    async def hang(job, lane):
        await _upload_photo_like()
    r._dispatch = hang

    job = taskqueue.Job(7, 42, "https://t.me/c/1/2 https://t.me/c/1/3", 9, 5)
    try:
        await r._run(job, "slow")
    finally:
        object.__setattr__(CFG, "stall_timeout", 300)
    assert retired == [42], "重试前换一条新连接"
    assert requeued == [(7, "slow")], "走正常的重试流程"
    assert "卡住" in said[-1] and "GetFileRequest" in said[-1], "用户能看到卡在哪"


# ================================================================ 换连接

@pytest.mark.asyncio
async def test_retire_waits_until_unused(monkeypatch):
    import session_pool
    closed = []

    async def fake_disconnect(c):
        closed.append(c)
    monkeypatch.setattr(session_pool, "_safe_disconnect", fake_disconnect)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(session_pool.asyncio, "sleep", lambda s: real_sleep(0.01))

    pool = session_pool.SessionPool()
    client = SimpleNamespace(is_connected=lambda: True)
    entry = session_pool._Entry(client)
    entry.busy = 1
    pool._entries[42] = entry

    await pool.retire(42)
    assert 42 not in pool._entries, "之后拿到的是新连接"
    await real_sleep(0.05)
    assert closed == [], "还有别的任务在用，先不断"
    entry.busy = 0
    await real_sleep(0.05)
    assert closed == [client]


@pytest.mark.asyncio
async def test_real_telethon_request_named():
    """真实的 Telethon 调用栈：第三方帧不算「我们的一步」，请求类型从它的帧里读出来。"""
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl import functions, types

    c = TelegramClient(StringSession(), 1, "0" * 32)
    c._call = lambda *a, **kw: asyncio.get_running_loop().create_future()   # 永不返回

    async def download_part():
        await c(functions.upload.GetFileRequest(
            location=types.InputDocumentFileLocation(id=1, access_hash=1,
                                                     file_reference=b"", thumb_size=""),
            offset=0, limit=4096))

    with pytest.raises(Stalled) as ei:
        await guard(download_part(), 0.3)
    assert ei.value.where == "download_part → GetFileRequest"
