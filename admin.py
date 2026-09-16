"""管理命令。只有机主能用。

身份只有两种：机主和授权用户，没有中间层。
机主主动增删，被加的人无感知 —— 没有申请、没有审批、没有通知。
加进来就能用，移出去就不能用。
"""
from __future__ import annotations

import logging
import time

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

import acl
import db
import streamer
from config import CFG
from session_pool import POOL

log = logging.getLogger("admin")
router = Router(name="admin")

_BOOT = time.time()

ADMIN_HELP = """<b>管理命令</b>（仅机主可用）

/users — 列出所有授权用户
/adduser <code>id [id...]</code> — 添加，可一次多个
/deluser <code>id [id...]</code> — 移除
/ban <code>id</code> — 封禁（保留用量记录）
/unban <code>id</code> — 解封
/queue — 队列状态
/stats — 全局统计

被加的人不会收到任何通知，直接就能用。
让对方找 @userinfobot 拿自己的数字 id。

授权用户与你的区别只有三点：用不了上面这些命令、
/killall 只能终止自己的任务、/status 只看自己的用量。

<b>注意</b>：授权用户是用你的账号去取消息的。
他们能读你账号能读到的一切，包括你的私有频道和私聊记录。
只加你自己的其他号，或完全信任的人。"""


async def _guard(m: Message) -> bool:
    """管理命令一律只有机主能用。

    拒绝方式分两种，是故意的：
      授权用户 —— 明确告诉他这是机主命令，否则他会以为 bot 卡住了
      未授权的人 —— 一声不吭，免得把命令的存在暴露出去

    这些处理函数挂在 admin 路由上，比主路由先匹配。一旦匹配就不再
    往下传，所以这里不回复就等于彻底没有反应。
    """
    if acl.is_owner(m.from_user.id):
        return True
    if await acl.check(m.from_user.id) is acl.Access.OK:
        await m.reply("这是机主命令，你没有权限。")
    return False


def _ids(args: str | None) -> list[int]:
    """从参数里取出所有数字 id，忽略无关内容。"""
    out = []
    for tok in (args or "").replace(",", " ").split():
        tok = tok.strip().lstrip("@")
        try:
            out.append(int(tok))
        except ValueError:
            continue
    return out


# ------------------------------------------------------------------ 查询

@router.message(Command("admin"))
async def cmd_admin(m: Message) -> None:
    if not await _guard(m):
        return
    await m.reply(ADMIN_HELP)


@router.message(Command("users"))
async def cmd_users(m: Message) -> None:
    if not await _guard(m):
        return
    rows = await db.list_users()
    icon = {"active": "✅", "banned": "🚫"}
    lines = ["<b>授权用户</b>", ""]
    for r in rows:
        if r["status"] not in ("active", "banned"):
            continue
        tag = "机主" if r["role"] == "owner" else "用户"
        name = f"@{r['username']}" if r["username"] else ""
        used = (f" · {r['task_count']} 次 · "
                f"{streamer.human_size(r['bytes_total'])}"
                if r["task_count"] else "")
        lines.append(
            f"{icon.get(r['status'], '?')} <code>{r['user_id']}</code> "
            f"{name} [{tag}]{used}")
    if len(lines) == 2:
        lines.append("（只有你自己）")
    await m.reply("\n".join(lines))


@router.message(Command("queue"))
async def cmd_queue(m: Message) -> None:
    if not await _guard(m):
        return
    s = await db.queue_stats()
    await m.reply(
        f"快通道　等待 {s['fast_pending']} · 执行 {s['fast_running']}\n"
        f"慢通道　等待 {s['slow_pending']} · 执行 {s['slow_running']}\n"
        f"待投递　{s['fast_relayed'] + s['slow_relayed']}"
    )


@router.message(Command("stats"))
async def cmd_stats(m: Message) -> None:
    if not await _guard(m):
        return
    t = await db.totals()
    p = await db.traffic_by_path()
    sess = await db.get_user(CFG.owner_id)
    up = int(time.time() - _BOOT)
    n_users = len([r for r in await db.list_users() if r["status"] == "active"])
    await m.reply(
        f"<b>全局统计</b>\n\n"
        f"运行　{up // 3600}h{(up % 3600) // 60}m\n"
        f"用户　{n_users} 个\n"
        f"凭据　{sess['session_status'] if sess else 'none'}\n\n"
        f"任务　完成 {t['done']} · 失败 {t['failed']} · 取消 {t['cancelled']}\n"
        f"消息　已投递 {t['messages']} 条\n"
        f"流量　搬运 {streamer.human_size(t['bytes'])}\n"
        f"直转　{p['direct']} 次零流量 · {p['moved']} 次需搬运"
    )


# ------------------------------------------------------------------ 增删

@router.message(Command("adduser"))
async def cmd_adduser(m: Message, command: CommandObject) -> None:
    if not await _guard(m):
        return
    ids = _ids(command.args)
    if not ids:
        await m.reply(
            "用法：<code>/adduser 123456789</code>\n"
            "可一次加多个：<code>/adduser 111 222 333</code>\n\n"
            "对方找 @userinfobot 发一句话就能拿到自己的数字 id。")
        return

    done, skip = [], []
    for uid in ids:
        if uid == CFG.owner_id:
            skip.append(f"{uid}（机主本人）")
            continue
        await db.upsert_user(uid, status="active", role="user",
                             added_by=m.from_user.id)
        done.append(uid)

    parts = []
    if done:
        parts.append("已添加：" + " ".join(f"<code>{u}</code>" for u in done))
        parts.append("对方不会收到通知，直接就能用。")
    if skip:
        parts.append("已跳过：" + "、".join(skip))
    await m.reply("\n".join(parts))


@router.message(Command("deluser"))
async def cmd_deluser(m: Message, command: CommandObject) -> None:
    if not await _guard(m):
        return
    ids = _ids(command.args)
    if not ids:
        await m.reply("用法：<code>/deluser 123456789</code>")
        return

    done, skip = [], []
    for uid in ids:
        if uid == CFG.owner_id:
            skip.append(str(uid))
            continue
        await db.conn().execute("DELETE FROM users WHERE user_id=?", (uid,))
        done.append(uid)
    await db.conn().commit()

    parts = []
    if done:
        parts.append("已移除：" + " ".join(f"<code>{u}</code>" for u in done))
    if skip:
        parts.append(f"机主不能移除自己（{'、'.join(skip)}）。")
    await m.reply("\n".join(parts))


@router.message(Command("ban"))
async def cmd_ban(m: Message, command: CommandObject) -> None:
    if not await _guard(m):
        return
    ids = [u for u in _ids(command.args) if u != CFG.owner_id]
    if not ids:
        await m.reply("用法：<code>/ban 123456789</code>（不能封禁机主）")
        return
    for uid in ids:
        await db.upsert_user(uid, status="banned")
    await m.reply("已封禁：" + " ".join(f"<code>{u}</code>" for u in ids)
                  + "\n用量记录保留，/unban 可恢复。")


@router.message(Command("unban"))
async def cmd_unban(m: Message, command: CommandObject) -> None:
    if not await _guard(m):
        return
    ids = _ids(command.args)
    if not ids:
        await m.reply("用法：<code>/unban 123456789</code>")
        return
    for uid in ids:
        await db.upsert_user(uid, status="active")
    await m.reply("已解封：" + " ".join(f"<code>{u}</code>" for u in ids))
