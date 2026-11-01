"""限流 / 重试的等待不占用 worker。

以前 _flood 和 _retry_or_fail 在 worker 里 sleep：快通道只有 4 个 worker，
几次长限流就能把它整个停住，后面的秒转请求全部积压。
"""
import asyncio
import os
import sys
import time
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

import taskqueue  # noqa: E402
from taskqueue import Job, Runner  # noqa: E402


@pytest.fixture
def runner(monkeypatch):
    r = Runner.__new__(Runner)
    r.bot = None
    r.fast, r.slow = asyncio.Queue(), asyncio.Queue()
    r._tasks, r._active, r._started = [], {}, 0
    r.said, r.failed = [], []

    async def noop(*a, **k):
        return None

    async def say(job, text):
        r.said.append(text)

    async def fail(job, reason):
        r.failed.append(reason)

    monkeypatch.setattr(taskqueue.db, "update_task", noop)
    monkeypatch.setattr(r, "_say", say, raising=False)
    monkeypatch.setattr(r, "_fail", fail, raising=False)
    return r


async def test_flood_returns_immediately_and_requeues_later(runner):
    job = Job(1, 42, "t.me/x/1", 1, 1)
    t0 = time.monotonic()
    await runner._flood(job, "fast", 300)
    assert time.monotonic() - t0 < 0.1
    assert runner.fast.empty()
    assert 1 in runner._waiting()
    assert runner.stats()["waiting"] == 1
    runner._waiting()[1][0].cancel()


async def test_requeue_after_delay(runner):
    job = Job(2, 42, "t.me/x/2", 1, 1)
    runner._requeue_later(job, "slow", 0.05)
    await asyncio.sleep(0.15)
    assert runner.slow.get_nowait() is job
    assert runner._waiting() == {}


async def test_flood_is_capped(runner):
    job = Job(3, 42, "t.me/x/3", 1, 1)
    job.floods = taskqueue.FLOOD_MAX
    await runner._flood(job, "fast", 10)
    assert runner.failed and "限流" in runner.failed[0]
    assert runner._waiting() == {}


async def test_flooded_job_does_not_block_the_worker(runner, monkeypatch):
    """一个任务被限流 300 秒，同一个 worker 后面的任务照常完成。"""
    done = []

    async def fake_run(job, lane):
        if job.task_id == 10:
            await runner._flood(job, lane, 300)
        else:
            done.append(job.task_id)

    monkeypatch.setattr(runner, "_run", fake_run, raising=False)
    worker = asyncio.create_task(runner._worker(runner.fast, "fast", 0))
    await runner.fast.put(Job(10, 42, "a", 1, 1))
    await runner.fast.put(Job(11, 42, "b", 1, 2))
    await asyncio.sleep(0.1)
    worker.cancel()
    for timer, _, _ in runner._waiting().values():
        timer.cancel()
    assert done == [11]


async def test_killall_cancels_waiting_jobs_in_scope(runner, monkeypatch):
    async def noop(*a, **k):
        return None

    async def cancel_all(owner_id=None):
        return 0, []

    monkeypatch.setattr(runner, "stop", noop, raising=False)
    monkeypatch.setattr(runner, "_spawn", lambda: None, raising=False)
    monkeypatch.setattr(taskqueue.db, "cancel_all", cancel_all)

    mine, other = Job(20, 7, "m", 1, 1), Job(21, 42, "o", 1, 2)
    runner._requeue_later(mine, "fast", 0.05)
    runner._requeue_later(other, "fast", 0.05)
    res = await runner.killall(7)
    assert res["queued"] == 1
    await asyncio.sleep(0.15)
    got = [runner.fast.get_nowait().task_id for _ in range(runner.fast.qsize())]
    assert got == [21], "被终止的任务不能在定时器到点后回到队列"


async def test_progress_task_is_referenced(runner):
    job = Job(30, 42, "x", 1, 1)
    runner._progress(job, "下载", 50, 100, 1.0)
    assert len(runner.__dict__.get("_bg", ())) == 1
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert runner.said


# ------------------------------------------------- 推文批量：剩余部分只提交一次

async def test_tweet_batch_rest_submitted_once_across_restart(runner, monkeypatch):
    """推文数据不落库，重启后会重新取、重新切；剩下那部分不能再提交一次。"""
    import tweet
    submitted, saved = [], []

    async def fetch(ref):
        return type("Tw", (), {"id": ref})()

    async def submit(*a, **k):
        submitted.append(a[1])

    async def update_task(task_id, **f):
        saved.append(f)

    async def send(job, lane):
        return None

    monkeypatch.setattr(tweet, "parse", lambda l: l)
    monkeypatch.setattr(tweet, "fetch", fetch)
    monkeypatch.setattr(tweet, "split_batch", lambda pairs: (pairs[:1], pairs[1:]))
    monkeypatch.setattr(tweet, "plan_batch",
                        lambda tws, skipped, show_name=False: ({"kind": "tweet"}, []))
    monkeypatch.setattr(tweet, "Tweet", lambda **k: object())
    monkeypatch.setattr(taskqueue.db, "update_task", update_task)
    monkeypatch.setattr(runner, "submit", submit, raising=False)
    monkeypatch.setattr(runner, "_send_tweet_media", send, raising=False)

    links = ["x.com/a/status/1", "x.com/a/status/2"]
    job = Job(40, 42, " ".join(links), 1, 1)
    await runner._run_tweet_batch(job, "fast", links)
    assert submitted == ["x.com/a/status/2"]
    assert job.extra.get("rest_done")

    # 模拟重启：推文数据没了，extra 从库里读回来
    restarted = Job(40, 42, job.link, 1, 1, extra=dict(job.extra))
    await runner._run_tweet_batch(restarted, "fast", links)
    assert submitted == ["x.com/a/status/2"], "重启后不能重复提交剩下的部分"
