"""SQLite 持久层。

schema 从一开始就按多用户设计：users 表、tasks.owner_id、每用户
自己的 session 与中转频道。当前只有一条 owner 记录在用，日后开放
多用户不需要迁移表结构。
"""
from __future__ import annotations

import time
from typing import Any, Optional

import aiosqlite

from config import CFG

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    user_id          INTEGER PRIMARY KEY,
    username         TEXT,
    role             TEXT    NOT NULL DEFAULT 'user',    -- owner|admin|user
    status           TEXT    NOT NULL DEFAULT 'pending', -- pending|active|banned
    session_enc      BLOB,
    session_status   TEXT    NOT NULL DEFAULT 'none',    -- none|ok|invalid
    relay_channel_id INTEGER,
    added_by         INTEGER,
    added_at         INTEGER NOT NULL,
    last_active      INTEGER,
    task_count       INTEGER NOT NULL DEFAULT 0,
    bytes_total      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        INTEGER NOT NULL,
    link            TEXT    NOT NULL,
    lane            TEXT    NOT NULL DEFAULT 'fast',     -- fast|slow
    state           TEXT    NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    tmp_path        TEXT,
    request_chat_id INTEGER NOT NULL,
    request_msg_id  INTEGER NOT NULL,
    status_msg_id   INTEGER,
    bytes_moved     INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, lane, id);
CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner_id, state);
"""

_db: Optional[aiosqlite.Connection] = None


def now() -> int:
    return int(time.time())


async def init() -> None:
    """建库建表，并确保 owner 记录存在。"""
    global _db
    CFG.db_path.parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(CFG.db_path)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(SCHEMA)
    await _db.commit()
    await ensure_owner()
    await recover_stuck()


async def close() -> None:
    if _db is not None:
        await _db.close()


def conn() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("db.init() 尚未调用")
    return _db


# ------------------------------------------------------------------ users

async def ensure_owner() -> None:
    """owner 永远存在、永远 active。中转频道默认取 .env 里配置的那个。"""
    await conn().execute(
        """INSERT INTO users (user_id, role, status, relay_channel_id, added_at)
           VALUES (?, 'owner', 'active', ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               role='owner', status='active',
               relay_channel_id=COALESCE(users.relay_channel_id, excluded.relay_channel_id)""",
        (CFG.owner_id, CFG.relay_channel_id, now()),
    )
    await conn().commit()


async def get_user(user_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    return await cur.fetchone()


async def upsert_user(user_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values())
    cur = await conn().execute(
        f"UPDATE users SET {cols} WHERE user_id=?", (*vals, user_id)
    )
    if cur.rowcount == 0:
        await conn().execute(
            "INSERT INTO users (user_id, added_at) VALUES (?, ?)", (user_id, now())
        )
        await conn().execute(
            f"UPDATE users SET {cols} WHERE user_id=?", (*vals, user_id)
        )
    await conn().commit()


async def list_users(status: Optional[str] = None) -> list[aiosqlite.Row]:
    if status:
        cur = await conn().execute(
            "SELECT * FROM users WHERE status=? ORDER BY added_at", (status,)
        )
    else:
        cur = await conn().execute("SELECT * FROM users ORDER BY added_at")
    return list(await cur.fetchall())


async def bump_usage(user_id: int, nbytes: int = 0) -> None:
    await conn().execute(
        """UPDATE users SET task_count=task_count+1,
                            bytes_total=bytes_total+?,
                            last_active=?
           WHERE user_id=?""",
        (nbytes, now(), user_id),
    )
    await conn().commit()


# ------------------------------------------------------------------ session

async def save_session(user_id: int, enc: bytes) -> None:
    await upsert_user(user_id, session_enc=enc, session_status="ok")


async def load_session(user_id: int) -> Optional[bytes]:
    row = await get_user(user_id)
    if row and row["session_status"] == "ok" and row["session_enc"]:
        return row["session_enc"]
    return None


async def mark_session_invalid(user_id: int) -> None:
    await upsert_user(user_id, session_status="invalid")


async def clear_session(user_id: int) -> None:
    await upsert_user(user_id, session_enc=None, session_status="none")


# ------------------------------------------------------------------ tasks

async def add_task(owner_id: int, link: str, request_chat_id: int,
                   request_msg_id: int, status_msg_id: Optional[int] = None) -> int:
    t = now()
    cur = await conn().execute(
        """INSERT INTO tasks
           (owner_id, link, request_chat_id, request_msg_id, status_msg_id,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (owner_id, link, request_chat_id, request_msg_id, status_msg_id, t, t),
    )
    await conn().commit()
    return cur.lastrowid


async def update_task(task_id: int, **fields: Any) -> None:
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    await conn().execute(
        f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id)
    )
    await conn().commit()


async def get_task(task_id: int) -> Optional[aiosqlite.Row]:
    cur = await conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,))
    return await cur.fetchone()


async def pending_tasks() -> list[aiosqlite.Row]:
    """进程启动时捡回未完成的任务。"""
    cur = await conn().execute(
        "SELECT * FROM tasks WHERE state IN ('pending','running') ORDER BY id"
    )
    return list(await cur.fetchall())


async def recover_stuck() -> None:
    """上次崩溃时标为 running 的任务，重置回 pending 以便重跑。"""
    await conn().execute(
        "UPDATE tasks SET state='pending', updated_at=? WHERE state='running'",
        (now(),),
    )
    await conn().commit()


async def orphan_tmp_files() -> list[str]:
    """崩溃遗留的临时文件路径，供启动时清理。"""
    cur = await conn().execute(
        "SELECT tmp_path FROM tasks WHERE tmp_path IS NOT NULL AND state!='done'"
    )
    return [r["tmp_path"] for r in await cur.fetchall() if r["tmp_path"]]


async def queue_stats() -> dict[str, int]:
    cur = await conn().execute(
        """SELECT lane, state, COUNT(*) c FROM tasks
           WHERE state IN ('pending','running') GROUP BY lane, state"""
    )
    out = {"fast_pending": 0, "fast_running": 0, "slow_pending": 0, "slow_running": 0}
    for r in await cur.fetchall():
        out[f"{r['lane']}_{r['state']}"] = r["c"]
    return out


async def totals() -> dict[str, int]:
    cur = await conn().execute(
        """SELECT
             SUM(state='done')   done,
             SUM(state='failed') failed,
             COALESCE(SUM(bytes_moved),0) bytes
           FROM tasks"""
    )
    r = await cur.fetchone()
    return {"done": r["done"] or 0, "failed": r["failed"] or 0, "bytes": r["bytes"] or 0}
