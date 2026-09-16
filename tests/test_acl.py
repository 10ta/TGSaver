"""白名单授权模型。

核心不变量：**发起任务的人**和**执行任务用谁的凭据**是两回事。
任何一处把这两者混同，都会导致授权用户拿不到凭据（任务全失败）
或者用错中转频道（投递失败）。
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

import acl  # noqa: E402
import db  # noqa: E402
from acl import Access  # noqa: E402

OWNER = 42
GUEST = 777
STRANGER = 999


def _set_db_path(p):
    from config import CFG
    object.__setattr__(CFG, "db_path", p)


@pytest.fixture
async def fresh(tmp_path):
    _set_db_path(tmp_path / "t.db")
    await db.init()
    yield
    await db.close()


# ------------------------------------------------- 准入

@pytest.mark.asyncio
async def test_owner_always_allowed(fresh):
    assert await acl.check(OWNER) is Access.OK
    assert acl.is_owner(OWNER)


@pytest.mark.asyncio
async def test_stranger_denied(fresh):
    assert await acl.check(STRANGER) is Access.NOT_ALLOWED
    assert not acl.is_owner(STRANGER)


@pytest.mark.asyncio
async def test_added_user_allowed(fresh):
    await db.upsert_user(GUEST, status="active", role="user", added_by=OWNER)
    assert await acl.check(GUEST) is Access.OK
    assert not acl.is_owner(GUEST), "授权用户不是机主"


@pytest.mark.asyncio
async def test_banned_user_blocked(fresh):
    await db.upsert_user(GUEST, status="active", role="user")
    await db.upsert_user(GUEST, status="banned")
    assert await acl.check(GUEST) is Access.BANNED


@pytest.mark.asyncio
async def test_unban_restores(fresh):
    await db.upsert_user(GUEST, status="banned")
    await db.upsert_user(GUEST, status="active")
    assert await acl.check(GUEST) is Access.OK


@pytest.mark.asyncio
async def test_removed_user_denied(fresh):
    await db.upsert_user(GUEST, status="active")
    await db.conn().execute("DELETE FROM users WHERE user_id=?", (GUEST,))
    await db.conn().commit()
    assert await acl.check(GUEST) is Access.NOT_ALLOWED


@pytest.mark.asyncio
async def test_no_middle_role(fresh):
    """身份只有两种。就算库里残留 admin 角色，也不该获得任何特权。"""
    await db.upsert_user(GUEST, status="active", role="admin")
    assert not acl.is_owner(GUEST)
    assert not hasattr(acl, "is_admin"), "is_admin 应已彻底移除"


@pytest.mark.asyncio
async def test_legacy_admin_demoted_on_migrate(tmp_path):
    """老库里的 admin 角色，init 时应自动降为普通用户。"""
    import aiosqlite
    p = tmp_path / "legacy.db"
    async with aiosqlite.connect(p) as c:
        await c.execute("""CREATE TABLE users (
            user_id INTEGER PRIMARY KEY, username TEXT,
            role TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'pending',
            session_enc BLOB, session_status TEXT NOT NULL DEFAULT 'none',
            relay_channel_id INTEGER, added_by INTEGER,
            added_at INTEGER NOT NULL, last_active INTEGER,
            task_count INTEGER NOT NULL DEFAULT 0,
            bytes_total INTEGER NOT NULL DEFAULT 0)""")
        await c.execute(
            "INSERT INTO users (user_id, role, status, added_at) "
            "VALUES (777, 'admin', 'active', 0)")
        await c.commit()

    _set_db_path(p)
    await db.init()
    assert (await db.get_user(777))["role"] == "user"
    await db.close()


# ------------------------------------------------- 凭据归属

def test_session_always_owner():
    """核心不变量：谁提交的任务都跑在机主凭据上。"""
    assert acl.session_user(OWNER) == OWNER
    assert acl.session_user(GUEST) == OWNER
    assert acl.session_user(STRANGER) == OWNER


@pytest.mark.asyncio
async def test_relay_channel_follows_session_owner(fresh):
    """授权用户没有自己的中转频道，必须用机主那个。"""
    assert await acl.relay_channel_for(OWNER) == -1001111111111
    await db.upsert_user(GUEST, status="active")
    assert await acl.relay_channel_for(GUEST) == -1001111111111


@pytest.mark.asyncio
async def test_guest_own_channel_field_is_ignored(fresh):
    """就算 guest 记录里存了频道 id，也该走机主的 —— 凭据是机主的，
    往 guest 的频道发会因为机主不在那个频道而失败。"""
    await db.upsert_user(GUEST, status="active",
                         relay_channel_id=-1009999999999)
    assert await acl.relay_channel_for(GUEST) == -1001111111111


# ------------------------------------------------- 接线检查

def test_taskqueue_takes_credentials_via_session_user():
    src = (Path(__file__).resolve().parent.parent / "taskqueue.py").read_text()
    assert "POOL.acquire(job.owner_id)" not in src, \
        "不能直接用请求者 id 取凭据"
    assert "POOL.acquire(acl.session_user(job.owner_id))" in src


def test_only_owner_gates_admin_commands():
    """所有管理命令必须挂在 is_owner 上，不能有别的门槛。"""
    src = (Path(__file__).resolve().parent.parent / "admin.py").read_text()
    assert "is_admin" not in src
    assert "acl.is_owner(m.from_user.id)" in src
    for gone in ("promote", "demote"):
        assert f'Command("{gone}")' not in src, f"/{gone} 应已移除"


def test_bot_takes_credentials_via_session_user():
    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text()
    assert "POOL.acquire(m.from_user.id)" not in src
    assert "acl.session_user(m.from_user.id)" in src


@pytest.mark.asyncio
async def test_usage_is_attributed_to_requester(fresh):
    """用量记在发起人头上，不是记在凭据归属者头上。"""
    await db.upsert_user(GUEST, status="active")
    tid = await db.add_task(GUEST, "l", 1, 1)
    await db.update_task(tid, state="done", bytes_moved=500)
    await db.bump_usage(GUEST, 500)

    assert (await db.totals(GUEST))["done"] == 1
    assert (await db.totals(OWNER))["done"] == 0
    assert (await db.get_user(GUEST))["bytes_total"] == 500


# ------------------------------------------------- killall 的范围

@pytest.mark.asyncio
async def test_killall_scoped_spares_others(fresh, monkeypatch):
    """普通用户的 /killall 不能打断别人的任务。

    早先的版本只在数据库侧按 scope 过滤，队列和 worker 却是一刀切，
    导致别人的任务被中断后还一直标着 running。
    """
    import taskqueue
    from taskqueue import Job, Runner

    r = Runner.__new__(Runner)
    r.bot = None
    r.fast = __import__("asyncio").Queue()
    r.slow = __import__("asyncio").Queue()
    r._tasks = []
    r._active = {}
    r._started = 0

    monkeypatch.setattr(r, "_spawn", lambda: None, raising=False)

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(r, "stop", noop, raising=False)
    monkeypatch.setattr(r, "_say", noop, raising=False)

    mine_q = await db.add_task(GUEST, "mine-queued", 1, 1)
    mine_r = await db.add_task(GUEST, "mine-running", 1, 2)
    other_q = await db.add_task(OWNER, "other-queued", 1, 3)
    other_r = await db.add_task(OWNER, "other-running", 1, 4)
    await db.update_task(mine_r, state="running")
    await db.update_task(other_r, state="running", lane="slow")

    r.fast.put_nowait(Job(mine_q, GUEST, "mine-queued", 1, 1))
    r.fast.put_nowait(Job(other_q, OWNER, "other-queued", 1, 3))
    r._active[mine_r] = Job(mine_r, GUEST, "mine-running", 1, 2)
    r._active[other_r] = Job(other_r, OWNER, "other-running", 1, 4,
                             lane="slow")

    res = await r.killall(GUEST)

    assert res["running"] == 1, "只该终止 guest 的 1 个进行中任务"
    assert res["queued"] == 1
    assert res["spared"] == 2, "owner 的 2 个任务应被放回"

    assert (await db.get_task(mine_q))["state"] == "cancelled"
    assert (await db.get_task(mine_r))["state"] == "cancelled"
    assert (await db.get_task(other_q))["state"] == "pending"
    assert (await db.get_task(other_r))["state"] == "pending", \
        "别人的任务不能停在 running，否则重启前永远不会被捡回"

    # 放回的任务要回到原来的通道
    assert r.fast.qsize() == 1 and r.slow.qsize() == 1


@pytest.mark.asyncio
async def test_killall_global_takes_everything(fresh, monkeypatch):
    import asyncio as aio
    from taskqueue import Job, Runner

    r = Runner.__new__(Runner)
    r.bot = None
    r.fast, r.slow = aio.Queue(), aio.Queue()
    r._tasks, r._active, r._started = [], {}, 0
    monkeypatch.setattr(r, "_spawn", lambda: None, raising=False)

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(r, "stop", noop, raising=False)
    monkeypatch.setattr(r, "_say", noop, raising=False)

    a = await db.add_task(GUEST, "a", 1, 1)
    b = await db.add_task(OWNER, "b", 1, 2)
    r.fast.put_nowait(Job(a, GUEST, "a", 1, 1))
    r.fast.put_nowait(Job(b, OWNER, "b", 1, 2))

    res = await r.killall(None)
    assert res["queued"] == 2
    assert res["spared"] == 0
    assert (await db.get_task(a))["state"] == "cancelled"
    assert (await db.get_task(b))["state"] == "cancelled"
    assert r.fast.qsize() == 0


# ------------------------------------------------- 管理命令的拒绝方式

class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.username = None
        self.full_name = "t"


class FakeMsg:
    """只够 _guard 用的最小消息对象。"""

    def __init__(self, uid):
        self.from_user = FakeUser(uid)
        self.replies = []

    async def reply(self, text, **kw):
        self.replies.append(text)
        return self


@pytest.mark.asyncio
async def test_guard_lets_owner_through(fresh):
    import admin
    m = FakeMsg(OWNER)
    assert await admin._guard(m) is True
    assert m.replies == [], "放行时不该有多余回复"


@pytest.mark.asyncio
async def test_guard_tells_authorized_user_why(fresh):
    """授权用户敲管理命令要有反馈，否则会以为 bot 卡住了。"""
    import admin
    await db.upsert_user(GUEST, status="active", role="user")
    m = FakeMsg(GUEST)
    assert await admin._guard(m) is False
    assert len(m.replies) == 1
    assert "机主" in m.replies[0]


@pytest.mark.asyncio
async def test_guard_is_silent_to_strangers(fresh):
    """陌生人不该知道这些命令存在。"""
    import admin
    m = FakeMsg(STRANGER)
    assert await admin._guard(m) is False
    assert m.replies == [], "对未授权者应完全静默"


@pytest.mark.asyncio
async def test_guard_is_silent_to_banned(fresh):
    import admin
    await db.upsert_user(GUEST, status="banned")
    m = FakeMsg(GUEST)
    assert await admin._guard(m) is False
    assert m.replies == []
