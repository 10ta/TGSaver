"""针对实际踩到的三个 bug 的回归测试。

1. 改了 .env 的 RELAY_CHANNEL_ID 重启后必须生效（曾被 COALESCE 挡住）
2. 投递侧的确定性错误必须当场判死，不能重试
3. 投递失败重试时不能重新搬运字节（曾导致 679MB 传了三遍）
"""
import asyncio
import json
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

import db  # noqa: E402
import taskqueue  # noqa: E402
from taskqueue import DELIVER_FATAL, Job, _explain_deliver  # noqa: E402


def _set_db_path(p):
    """CFG 是 frozen dataclass，测试里只能绕过 __setattr__。"""
    from config import CFG
    object.__setattr__(CFG, "db_path", p)


@pytest.fixture
async def freshdb(tmp_path):
    _set_db_path(tmp_path / "t.db")
    await db.init()
    yield
    await db.close()


# ------------------------------------------------- bug 2：错误分类

def test_deliver_errors_are_fatal():
    """这三类 aiogram 异常必须走「不重试」分支。"""
    from aiogram.exceptions import (
        TelegramBadRequest, TelegramForbiddenError, TelegramNotFound)
    for cls in (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound):
        assert issubclass(cls, DELIVER_FATAL)


def test_chat_not_found_message_is_actionable():
    """报错要告诉人怎么修，不能只甩一句英文。"""
    class Fake(Exception):
        def __str__(self):
            return "Telegram server says - Bad Request: chat not found"

    txt = _explain_deliver(Fake(), -1002222222222)
    assert "-1002222222222" in txt
    assert "BOT_TOKEN" in txt          # 点出最常见的原因
    assert "管理员" in txt              # 给出具体动作


def test_other_deliver_errors_explained():
    class E1(Exception):
        def __str__(self): return "Bad Request: message to copy not found"

    class E2(Exception):
        def __str__(self): return "Forbidden: bot was kicked from the channel"

    assert "已被删除" in _explain_deliver(E1(), 1)
    assert "移出" in _explain_deliver(E2(), 1)


# ------------------------------------------------- bug 3：不重传

@pytest.mark.asyncio
async def test_relayed_task_skips_transfer(freshdb):
    """已搬运的任务重启后应直接投递，不碰传输层。"""
    tid = await db.add_task(42, "https://t.me/a/1", 9, 9)
    await db.update_task(
        tid, state="relayed", lane="slow", bytes_moved=679 * 1024 * 1024,
        relay_chat_id=-1001111111111, relay_ids=json.dumps([3924, 3925]),
        relay_is_album=0,
    )

    rows = await db.pending_tasks()
    assert len(rows) == 1, "relayed 状态必须能被 _restore 捡回"

    r = rows[0]
    job = Job(
        task_id=r["id"], owner_id=r["owner_id"], link=r["link"],
        request_chat_id=r["request_chat_id"], request_msg_id=r["request_msg_id"],
        relay_chat_id=r["relay_chat_id"],
        relay_ids=json.loads(r["relay_ids"]),
        relay_is_album=bool(r["relay_is_album"]),
    )
    assert job.relayed
    assert job.relay_ids == [3924, 3925]


@pytest.mark.asyncio
async def test_run_uses_deliver_not_transfer(freshdb, monkeypatch):
    """核心断言：relayed 的任务走 _deliver，绝不进 _run_slow。"""
    tid = await db.add_task(42, "https://t.me/a/1", 9, 9)
    await db.update_task(tid, state="relayed", bytes_moved=123,
                         relay_chat_id=-100, relay_ids=json.dumps([1]))

    calls = []
    runner = taskqueue.Runner.__new__(taskqueue.Runner)
    runner._active = {}

    async def fake_deliver(job, note=""):
        calls.append("deliver")

    async def fake_slow(job):
        calls.append("slow")      # 一旦被调用就说明又要重传了

    async def fake_fast(job):
        calls.append("fast")

    monkeypatch.setattr(runner, "_deliver", fake_deliver, raising=False)
    monkeypatch.setattr(runner, "_run_slow", fake_slow, raising=False)
    monkeypatch.setattr(runner, "_run_fast", fake_fast, raising=False)

    job = Job(task_id=tid, owner_id=42, link="x", request_chat_id=9,
              request_msg_id=9, relay_chat_id=-100, relay_ids=[1])
    await taskqueue.Runner._run(runner, job, "slow")

    assert calls == ["deliver"], f"期望只投递，实际发生了 {calls}"


# ------------------------------------------------- 取消与统计

@pytest.mark.asyncio
async def test_cancel_all(freshdb):
    a = await db.add_task(42, "l1", 1, 1)
    b = await db.add_task(42, "l2", 1, 2)
    c = await db.add_task(42, "l3", 1, 3)
    await db.update_task(a, state="running", tmp_path="/tmp/nope.bin")
    await db.update_task(b, state="relayed")
    await db.update_task(c, state="done")

    n, tmps = await db.cancel_all()
    assert n == 2, "done 的任务不该被取消"
    assert tmps == ["/tmp/nope.bin"]
    assert (await db.get_task(a))["state"] == "cancelled"
    assert (await db.get_task(c))["state"] == "done"


@pytest.mark.asyncio
async def test_totals_counts_messages_and_bytes(freshdb):
    a = await db.add_task(42, "l1", 1, 1)
    b = await db.add_task(42, "l2", 1, 2)
    await db.update_task(a, state="done", bytes_moved=0,
                         relay_ids=json.dumps([1]))            # 直转，1 条
    await db.update_task(b, state="done", bytes_moved=100,
                         relay_ids=json.dumps([2, 3, 4]))      # 搬运，3 条

    t = await db.totals()
    assert t["done"] == 2
    assert t["bytes"] == 100
    assert t["messages"] == 4, "应按 relay_ids 里的条数累计"

    p = await db.traffic_by_path()
    assert p["direct"] == 1 and p["moved"] == 1


@pytest.mark.asyncio
async def test_migration_adds_columns(tmp_path):
    """老版本数据库（没有 relay_* 列）应能平滑升级。"""
    import aiosqlite

    p = tmp_path / "old.db"
    async with aiosqlite.connect(p) as c:
        await c.execute("""CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL,
            link TEXT NOT NULL, lane TEXT NOT NULL DEFAULT 'fast',
            state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT, tmp_path TEXT, request_chat_id INTEGER NOT NULL,
            request_msg_id INTEGER NOT NULL, status_msg_id INTEGER,
            bytes_moved INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""")
        await c.execute(
            "INSERT INTO tasks (owner_id,link,request_chat_id,request_msg_id,"
            "created_at,updated_at) VALUES (42,'old',1,1,0,0)")
        await c.commit()

    _set_db_path(p)
    await db.init()
    row = await db.get_task(1)
    assert row["link"] == "old", "旧数据必须保留"
    assert row["relay_ids"] is None, "新列应已补上"
    await db.close()
