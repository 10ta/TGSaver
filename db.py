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
    state           TEXT    NOT NULL DEFAULT 'pending',  -- pending|running|relayed|done|failed|cancelled
    attempts        INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    tmp_path        TEXT,
    request_chat_id INTEGER NOT NULL,
    request_msg_id  INTEGER NOT NULL,
    status_msg_id   INTEGER,
    bytes_moved     INTEGER NOT NULL DEFAULT 0,
    -- 已搬进中转频道的结果。投递失败重试时凭它跳过传输，
    -- 不再把几百 MB 重下重传一遍。
    relay_chat_id   INTEGER,
    relay_ids       TEXT,
    relay_is_album  INTEGER NOT NULL DEFAULT 0,
    -- 非 Telegram 来源（如推文）的呈现方式，JSON。投递失败重试时
    -- 凭它重组消息和按钮，不必重新请求外部 API。
    extra           TEXT,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, lane, id);
CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner_id, state);

-- 自动监听的对话。注册一次之后由后台轮询增量抓取，
-- 不需要每次手敲命令。
CREATE TABLE IF NOT EXISTS watches (
    owner_id     INTEGER NOT NULL,
    peer         TEXT    NOT NULL,        -- 用户名或数字 id，原样保存
    title        TEXT,                    -- 展示名，便于 /watched 辨认
    last_seen_id INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1,
    grabbed      INTEGER NOT NULL DEFAULT 0,
    added_at     INTEGER NOT NULL,
    last_poll    INTEGER,
    PRIMARY KEY (owner_id, peer)
);
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
    await _migrate()
    await _db.commit()
    await ensure_owner()
    await recover_stuck()


async def _migrate() -> None:
    """给早于当前版本的数据库补列。CREATE TABLE IF NOT EXISTS 不会改已有表。"""
    cur = await _db.execute("PRAGMA table_info(tasks)")
    have = {r["name"] for r in await cur.fetchall()}
    additions = {
        "relay_chat_id": "INTEGER",
        "relay_ids": "TEXT",
        "relay_is_album": "INTEGER NOT NULL DEFAULT 0",
        "extra": "TEXT",
    }
    for col, decl in additions.items():
        if col not in have:
            await _db.execute(f"ALTER TABLE tasks ADD COLUMN {col} {decl}")

    # 管理员这个中间身份已取消，老库里若有则降为普通用户
    await _db.execute("UPDATE users SET role='user' WHERE role='admin'")
    await _db.commit()


async def close() -> None:
    if _db is not None:
        await _db.close()


def conn() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("db.init() 尚未调用")
    return _db


# ------------------------------------------------------------------ users

async def ensure_owner() -> None:
    """owner 永远存在、永远 active。

    中转频道以 .env 为准：改了 RELAY_CHANNEL_ID 重启就该生效。
    这里曾经用 COALESCE 保留库中旧值，导致改配置不起作用，且新 bot
    因为不在旧频道里而投递失败 —— 已修正。
    多用户的 relay_channel_id 由各自登录流程写入，不走这个函数。
    """
    old = await get_user(CFG.owner_id)
    await conn().execute(
        """INSERT INTO users (user_id, role, status, relay_channel_id, added_at)
           VALUES (?, 'owner', 'active', ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               role='owner', status='active',
               relay_channel_id=excluded.relay_channel_id""",
        (CFG.owner_id, CFG.relay_channel_id, now()),
    )
    await conn().commit()
    if old and old["relay_channel_id"] and \
            int(old["relay_channel_id"]) != CFG.relay_channel_id:
        import logging
        logging.getLogger("db").warning(
            "中转频道已更新：%s -> %s（以 .env 为准）",
            old["relay_channel_id"], CFG.relay_channel_id,
        )


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
    """进程启动时捡回未完成的任务。relayed 也算，它只差投递一步。"""
    cur = await conn().execute(
        "SELECT * FROM tasks WHERE state IN ('pending','running','relayed') ORDER BY id"
    )
    return list(await cur.fetchall())


async def recover_stuck() -> None:
    """上次崩溃时标为 running 的任务，重置回 pending 以便重跑。

    relayed 不动 —— 那些已经搬完了，重启后只需重投递。
    """
    await conn().execute(
        "UPDATE tasks SET state='pending', updated_at=? WHERE state='running'",
        (now(),),
    )
    await conn().commit()


async def cancel_all(owner_id: Optional[int] = None) -> tuple[int, list[str]]:
    """终止所有未完成任务，返回 (条数, 需要清理的临时文件)。"""
    where = "state IN ('pending','running','relayed')"
    args: tuple = ()
    if owner_id is not None:
        where += " AND owner_id=?"
        args = (owner_id,)

    cur = await conn().execute(
        f"SELECT id, tmp_path FROM tasks WHERE {where}", args)
    rows = list(await cur.fetchall())
    tmps = [r["tmp_path"] for r in rows if r["tmp_path"]]

    await conn().execute(
        f"""UPDATE tasks SET state='cancelled', error='用户终止',
                             tmp_path=NULL, updated_at=?
            WHERE {where}""",
        (now(), *args),
    )
    await conn().commit()
    return len(rows), tmps


async def orphan_tmp_files() -> list[str]:
    """崩溃遗留的临时文件路径，供启动时清理。"""
    cur = await conn().execute(
        "SELECT tmp_path FROM tasks WHERE tmp_path IS NOT NULL AND state!='done'"
    )
    return [r["tmp_path"] for r in await cur.fetchall() if r["tmp_path"]]


async def queue_stats() -> dict[str, int]:
    cur = await conn().execute(
        """SELECT lane, state, COUNT(*) c FROM tasks
           WHERE state IN ('pending','running','relayed') GROUP BY lane, state"""
    )
    out = {"fast_pending": 0, "fast_running": 0, "fast_relayed": 0,
           "slow_pending": 0, "slow_running": 0, "slow_relayed": 0}
    for r in await cur.fetchall():
        key = f"{r['lane']}_{r['state']}"
        if key in out:
            out[key] = r["c"]
    return out


async def totals(owner_id: Optional[int] = None) -> dict[str, int]:
    """完成/失败/取消数、搬运字节、投递消息条数。

    relay_ids 存的就是 JSON 数组，用 json_array_length 数条数，
    比拿逗号做字符串计数可靠（空数组、单元素都不会算错）。
    """
    where, args = ("WHERE owner_id=?", (owner_id,)) if owner_id is not None else ("", ())
    cur = await conn().execute(
        f"""SELECT
              COUNT(*)                     total,
              SUM(state='done')            done,
              SUM(state='failed')          failed,
              SUM(state='cancelled')       cancelled,
              COALESCE(SUM(bytes_moved),0) bytes,
              COALESCE(SUM(CASE
                  WHEN state='done' AND relay_ids IS NOT NULL
                  THEN json_array_length(relay_ids)
                  WHEN state='done' THEN 1
                  ELSE 0 END), 0)          messages
            FROM tasks {where}""",
        args,
    )
    r = await cur.fetchone()
    return {k: (r[k] or 0) for k in
            ("total", "done", "failed", "cancelled", "bytes", "messages")}


async def traffic_by_path() -> dict[str, int]:
    """零流量（服务端直转）与实际搬运的字节数对比。"""
    cur = await conn().execute(
        """SELECT
             SUM(CASE WHEN bytes_moved=0 THEN 1 ELSE 0 END) direct,
             SUM(CASE WHEN bytes_moved>0 THEN 1 ELSE 0 END) moved,
             COALESCE(SUM(bytes_moved),0) bytes
           FROM tasks WHERE state='done'"""
    )
    r = await cur.fetchone()
    return {"direct": r["direct"] or 0, "moved": r["moved"] or 0,
            "bytes": r["bytes"] or 0}


# ------------------------------------------------------------------ watches

async def add_watch(owner_id: int, peer: str, title: str,
                    last_seen_id: int) -> bool:
    """注册监听。返回 True 表示新建，False 表示已存在（重新启用）。"""
    row = await get_watch(owner_id, peer)
    if row is not None:
        await conn().execute(
            "UPDATE watches SET enabled=1, title=? WHERE owner_id=? AND peer=?",
            (title, owner_id, peer))
        await conn().commit()
        return False
    await conn().execute(
        """INSERT INTO watches (owner_id, peer, title, last_seen_id, added_at)
           VALUES (?,?,?,?,?)""",
        (owner_id, peer, title, last_seen_id, now()))
    await conn().commit()
    return True


async def get_watch(owner_id: int, peer: str) -> Optional[aiosqlite.Row]:
    cur = await conn().execute(
        "SELECT * FROM watches WHERE owner_id=? AND peer=?", (owner_id, peer))
    return await cur.fetchone()


async def remove_watch(owner_id: int, peer: str) -> bool:
    cur = await conn().execute(
        "DELETE FROM watches WHERE owner_id=? AND peer=?", (owner_id, peer))
    await conn().commit()
    return cur.rowcount > 0


async def list_watches(owner_id: Optional[int] = None,
                       only_enabled: bool = False) -> list[aiosqlite.Row]:
    sql = "SELECT * FROM watches"
    cond, args = [], []
    if owner_id is not None:
        cond.append("owner_id=?")
        args.append(owner_id)
    if only_enabled:
        cond.append("enabled=1")
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY added_at"
    cur = await conn().execute(sql, tuple(args))
    return list(await cur.fetchall())


async def bump_watch(owner_id: int, peer: str, last_seen_id: int,
                     grabbed: int = 0) -> None:
    await conn().execute(
        """UPDATE watches SET last_seen_id=?, grabbed=grabbed+?, last_poll=?
           WHERE owner_id=? AND peer=?""",
        (last_seen_id, grabbed, now(), owner_id, peer))
    await conn().commit()


async def set_watch_enabled(owner_id: int, peer: str, on: bool) -> None:
    await conn().execute(
        "UPDATE watches SET enabled=? WHERE owner_id=? AND peer=?",
        (1 if on else 0, owner_id, peer))
    await conn().commit()
