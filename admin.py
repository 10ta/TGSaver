"""管理命令与审批流程。

代码全部写好了，但受 acl.MULTI_USER 控制：
  - False（当前）：只有 owner 能用 bot，管理命令会提示多用户未开启
  - True：申请审批、加人、封禁全部生效，无需改动其它模块
"""
from __future__ import annotations

import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import acl
import db
import streamer
from config import CFG
from session_pool import POOL

log = logging.getLogger("admin")
router = Router(name="admin")


def _need_multi() -> str:
    return ("多用户模式当前未开启。\n"
            "要开启：编辑 acl.py，把 MULTI_USER 改成 True 后重启服务。")


async def _guard(m: Message) -> bool:
    if not await acl.is_admin(m.from_user.id):
        return False
    if not acl.MULTI_USER:
        await m.reply(_need_multi())
        return False
    return True


# ------------------------------------------------------------------ 查询

@router.message(Command("users"))
async def cmd_users(m: Message) -> None:
    if not await acl.is_admin(m.from_user.id):
        return
    rows = await db.list_users()
    if not rows:
        await m.reply("暂无用户记录。")
        return
    icon = {"active": "✅", "pending": "⏳", "banned": "🚫"}
    sess = {"ok": "已登录", "invalid": "已失效", "none": "未登录"}
    lines = []
    for r in rows:
        name = f"@{r['username']}" if r["username"] else str(r["user_id"])
        lines.append(
            f"{icon.get(r['status'], '?')} {name} · {r['role']} · "
            f"{sess.get(r['session_status'], '?')} · "
            f"{r['task_count']} 次 · {streamer.human_size(r['bytes_total'])}"
        )
    tail = "" if acl.MULTI_USER else f"\n\n（{_need_multi()}）"
    await m.reply("\n".join(lines) + tail)


@router.message(Command("queue"))
async def cmd_queue(m: Message, runner=None) -> None:
    if not await acl.is_admin(m.from_user.id):
        return
    s = await db.queue_stats()
    await m.reply(
        f"快通道 等待 {s['fast_pending']} / 执行 {s['fast_running']}\n"
        f"慢通道 等待 {s['slow_pending']} / 执行 {s['slow_running']}"
    )


@router.message(Command("stats"))
async def cmd_stats(m: Message) -> None:
    if not await acl.is_admin(m.from_user.id):
        return
    t = await db.totals()
    me = await db.get_user(m.from_user.id)
    up = int(time.time() - _BOOT)
    await m.reply(
        f"运行时长 {up // 3600}h{(up % 3600) // 60}m\n"
        f"累计完成 {t['done']} · 失败 {t['failed']}\n"
        f"搬运流量 {streamer.human_size(t['bytes'])}\n"
        f"登录状态 {me['session_status'] if me else 'none'}\n"
        f"多用户 {'开启' if acl.MULTI_USER else '关闭'}"
    )


# ------------------------------------------------------------------ 增删改

@router.message(Command("adduser"))
async def cmd_adduser(m: Message, command: CommandObject) -> None:
    if not await _guard(m):
        return
    uid = _parse_uid(command.args)
    if uid is None:
        await m.reply("用法：/adduser <数字id>")
        return
    await db.upsert_user(uid, status="active", role="user",
                         added_by=m.from_user.id)
    await m.reply(f"已添加 {uid}")
    await _notify(m.bot, uid, "管理员已通过你的申请，发送 /login 开始登录。")


@router.message(Command("deluser"))
async def cmd_deluser(m: Message, command: CommandObject) -> None:
    if not await _guard(m):
        return
    uid = _parse_uid(command.args)
    if uid is None or uid == CFG.owner_id:
        await m.reply("用法：/deluser <数字id>（不能删除机主）")
        return
    await POOL.invalidate(uid)
    await db.conn().execute("DELETE FROM users WHERE user_id=?", (uid,))
    await db.conn().commit()
    await m.reply(f"已移除 {uid}")


@router.message(Command("ban"))
async def cmd_ban(m: Message, command: CommandObject) -> None:
    if not await _guard(m):
        return
    uid = _parse_uid(command.args)
    if uid is None or uid == CFG.owner_id:
        await m.reply("用法：/ban <数字id>")
        return
    await db.upsert_user(uid, status="banned")
    await POOL.invalidate(uid)
    await m.reply(f"已封禁 {uid}")


@router.message(Command("unban"))
async def cmd_unban(m: Message, command: CommandObject) -> None:
    if not await _guard(m):
        return
    uid = _parse_uid(command.args)
    if uid is None:
        await m.reply("用法：/unban <数字id>")
        return
    await db.upsert_user(uid, status="active")
    await m.reply(f"已解封 {uid}")


@router.message(Command("promote"))
async def cmd_promote(m: Message, command: CommandObject) -> None:
    if not acl.is_owner(m.from_user.id) or not await _guard(m):
        return
    uid = _parse_uid(command.args)
    if uid is None:
        await m.reply("用法：/promote <数字id>")
        return
    await db.upsert_user(uid, role="admin", status="active")
    await m.reply(f"{uid} 已提升为管理员")


@router.message(Command("demote"))
async def cmd_demote(m: Message, command: CommandObject) -> None:
    if not acl.is_owner(m.from_user.id) or not await _guard(m):
        return
    uid = _parse_uid(command.args)
    if uid is None or uid == CFG.owner_id:
        await m.reply("用法：/demote <数字id>")
        return
    await db.upsert_user(uid, role="user")
    await m.reply(f"{uid} 已降为普通用户")


@router.message(Command("revoke"))
async def cmd_revoke(m: Message, command: CommandObject) -> None:
    """强制作废某用户的 session，不动其账号权限。"""
    if not await _guard(m):
        return
    uid = _parse_uid(command.args)
    if uid is None:
        await m.reply("用法：/revoke <数字id>")
        return
    await POOL.invalidate(uid)
    await db.clear_session(uid)
    await m.reply(f"已作废 {uid} 的 session")


# ------------------------------------------------------------------ 审批

async def request_access(bot: Bot, user) -> None:
    """陌生人首次接触 bot 时调用：落一条 pending 记录并通知 owner。"""
    if not acl.MULTI_USER:
        return
    existing = await db.get_user(user.id)
    if existing and existing["status"] in ("active", "banned"):
        return
    if not existing:
        await db.upsert_user(user.id, username=user.username, status="pending")

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ 通过", callback_data=f"ap:ok:{user.id}"),
        InlineKeyboardButton(text="❌ 拒绝", callback_data=f"ap:no:{user.id}"),
        InlineKeyboardButton(text="🚫 封禁", callback_data=f"ap:ban:{user.id}"),
    ]])
    name = f"@{user.username}" if user.username else user.full_name
    await bot.send_message(
        CFG.owner_id,
        f"新的使用申请\n{name}\nid: <code>{user.id}</code>",
        reply_markup=kb, parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("ap:"))
async def on_approval(cb: CallbackQuery) -> None:
    if not await acl.is_admin(cb.from_user.id):
        await cb.answer("无权限", show_alert=True)
        return
    _, action, uid_s = cb.data.split(":")
    uid = int(uid_s)

    if action == "ok":
        await db.upsert_user(uid, status="active", role="user",
                             added_by=cb.from_user.id)
        verdict = "已通过"
        await _notify(cb.bot, uid, "申请已通过，发送 /login 开始登录。")
    elif action == "no":
        await db.conn().execute("DELETE FROM users WHERE user_id=?", (uid,))
        await db.conn().commit()
        verdict = "已拒绝"
        await _notify(cb.bot, uid, "你的申请未被通过。")
    else:
        await db.upsert_user(uid, status="banned")
        await POOL.invalidate(uid)
        verdict = "已封禁"

    await cb.message.edit_text(f"{cb.message.text}\n\n→ {verdict}")
    await cb.answer(verdict)


# ------------------------------------------------------------------ 工具

def _parse_uid(args: str | None) -> int | None:
    if not args:
        return None
    try:
        return int(args.strip().split()[0])
    except (ValueError, IndexError):
        return None


async def _notify(bot: Bot, uid: int, text: str) -> None:
    try:
        await bot.send_message(uid, text)
    except Exception as e:  # noqa: BLE001
        log.debug("通知 %s 失败: %s", uid, e)


_BOOT = time.time()
