#!/usr/bin/env python3
"""TgSaver 主入口。

单进程、单 event loop：
  aiogram bot（长轮询）  +  Telethon session 池  +  双通道调度器
"""
from __future__ import annotations

import asyncio
import logging
import re
import signal
import sys

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message
from telethon import utils
from telethon.tl.types import MessageService

import acl
import admin
import db
import streamer
from config import CFG
from parser import (
    ParseError, find_links, make_internal, parse_link, wants_nosp, with_nosp,
)
from session_pool import POOL, NoSession
from taskqueue import Runner

logging.basicConfig(
    level=CFG.log_level,
    format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s",
    datefmt="%m-%d %H:%M:%S",
)
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("aiogram").setLevel(logging.WARNING)
log = logging.getLogger("main")

router = Router(name="main")
RUNNER: Runner | None = None

GRAB_MAX = 20      # 单次最多抓几条，再多容易触发 FloodWait
GRAB_SCAN = 200    # 往回翻多少条消息去找媒体

HELP = """把 Telegram 消息链接发给我，我原样取回来给你。

支持公开 / 私有 / 论坛话题 / 评论区链接，相册自动整组，
禁止转存的内容也能搬，大文件不受 50MB 限制。

链接后面加 <code>nosp</code> 可去掉剧透遮罩（会重新上传，慢一些）。
受保护的内容本来就是重传，遮罩一律自动去掉。

@对话名    抓取私聊内容，如 @some_bot 5（无法转发、没有链接的用这个）
/status   登录状态、流量与消息统计
/killall  终止所有进行中的任务并清空队列
/logout   注销登录凭据
/help     本说明"""


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(m: Message) -> None:
    if not await _allowed(m):
        return
    await m.reply(HELP)


@router.message(Command("status"))
async def cmd_status(m: Message) -> None:
    if not await _allowed(m):
        return
    row = await db.get_user(m.from_user.id)
    sess = {"ok": "已登录", "invalid": "已失效，请重新运行 login.py",
            "none": "未登录，请运行 login.py"}
    q = RUNNER.stats() if RUNNER else {"fast": 0, "slow": 0, "uptime": 0}
    t = await db.totals(m.from_user.id)
    p = await db.traffic_by_path()
    up = q["uptime"]

    pending = q["fast"] + q["slow"]
    lines = [
        f"<b>凭据</b>　{sess.get(row['session_status'] if row else 'none', '未知')}",
        f"<b>运行</b>　{up // 3600}h{(up % 3600) // 60}m",
        f"<b>中转</b>　<code>{await acl.relay_channel_for(m.from_user.id)}</code>",
        "",
        f"<b>任务</b>　完成 {t['done']} · 失败 {t['failed']}"
        + (f" · 取消 {t['cancelled']}" if t["cancelled"] else ""),
        f"<b>消息</b>　已投递 {t['messages']} 条",
        f"<b>流量</b>　搬运 {streamer.human_size(t['bytes'])}"
        f"（下载+上传约 {streamer.human_size(t['bytes'] * 2)}）",
        f"<b>直转</b>　{p['direct']} 次零流量 · {p['moved']} 次需搬运",
    ]
    if pending:
        lines += ["", f"<b>队列</b>　快 {q['fast']} · 慢 {q['slow']}"]
    await m.reply("\n".join(lines))


GRAB_USERNAME_RE = re.compile(
    r"^@([A-Za-z][A-Za-z0-9_]{3,31})(?:\s+(\d{1,3}))?$")
# 数字 id 至少 6 位。太短的数字更可能是用户随手打的东西，
# 当成对话 id 去解析只会得到一句莫名其妙的报错。
GRAB_NUMERIC_RE = re.compile(r"^(-?\d{6,})(?:\s+(\d{1,3}))?$")


def parse_grab_target(text: str):
    """从一行裸文本里认出抓取目标，认不出返回 None。

    接受：
        @some_bot        -> ("some_bot", 1)
        @some_bot 5      -> ("some_bot", 5)
        123456789 3      -> ("123456789", 3)

    用户名必须带 @：否则 "hello" 这种普通词也会被当成对话名，
    然后抛一个让人摸不着头脑的错误。
    """
    t = (text or "").strip()
    for rx in (GRAB_USERNAME_RE, GRAB_NUMERIC_RE):
        mm = rx.match(t)
        if mm:
            n = int(mm.group(2)) if mm.group(2) else 1
            return mm.group(1), max(1, min(n, GRAB_MAX))
    return None


GRAB_HELP = (
    "直接发对话名就行，不用带命令：\n\n"
    "<code>@some_bot</code> — 抓最近 1 条媒体\n"
    "<code>@some_bot 5</code> — 抓最近 5 条\n"
    "<code>123456789 3</code> — 数字 id 也行\n\n"
    "用于保存私聊里那些无法转发、也没有链接的内容。"
)


async def do_grab(m: Message, target: str, count: int) -> None:
    """按对话直接寻址抓取。

    私聊没有 t.me 链接 —— Telegram 只为公开频道和超级群生成链接。
    这里把找到的消息合成内部伪链接丢进同一套队列，
    下游的传输和投递逻辑完全复用。
    """
    row = await db.get_user(m.from_user.id)
    if not row or row["session_status"] != "ok":
        await m.reply("还没有可用的登录凭据。请在服务器上运行 login.py。")
        return

    status = await m.reply(f"正在查找 {target} 最近的内容…")
    try:
        client = await POOL.acquire(m.from_user.id)
        async with POOL.lock_for(m.from_user.id):
            try:
                entity = await client.get_entity(
                    int(target) if target.lstrip("-").isdigit() else target)
            except Exception as e:  # noqa: BLE001
                await status.edit_text(
                    f"找不到对话 {target}。\n"
                    f"请确认你和它有过对话，用户名或 id 拼写正确。\n"
                    f"（{type(e).__name__}）")
                return

            picked, seen_groups = [], set()
            async for msg in client.iter_messages(entity, limit=GRAB_SCAN):
                if msg.media is None or isinstance(msg, MessageService):
                    continue
                gid = getattr(msg, "grouped_id", None)
                if gid is not None:
                    if gid in seen_groups:
                        continue          # 相册只取一条，下游会自动凑齐整组
                    seen_groups.add(gid)
                picked.append(msg)
                if len(picked) >= count:
                    break
    except NoSession:
        await status.edit_text("登录凭据已失效，请重新运行 login.py。")
        return

    if not picked:
        await status.edit_text(
            f"在 {target} 最近 {GRAB_SCAN} 条消息里没找到媒体内容。")
        return

    peer = utils.get_peer_id(entity)
    for msg in reversed(picked):          # 由旧到新，保持原顺序
        await RUNNER.submit(m.from_user.id, make_internal(peer, msg.id),
                            m.chat.id, m.message_id)

    await status.edit_text(f"已找到 {len(picked)} 条，正在处理…")


@router.message(Command("grab"))
async def cmd_grab(m: Message, command: CommandObject) -> None:
    """/grab 保留作为别名，但直接发 `@对话 条数` 更省事。"""
    if not await _allowed(m):
        return
    parsed = parse_grab_target(command.args or "")
    if parsed is None:
        await m.reply(GRAB_HELP)
        return
    await do_grab(m, *parsed)


@router.message(Command("killall"))
async def cmd_killall(m: Message) -> None:
    """终止所有进行中的任务并清空队列。"""
    if not await _allowed(m):
        return
    if RUNNER is None:
        await m.reply("调度器尚未就绪。")
        return

    q = RUNNER.stats()
    if q["fast"] + q["slow"] + q["active"] == 0:
        await m.reply("当前没有进行中或排队中的任务。")
        return

    # 管理员可以终止全部；普通用户只能终止自己的
    scope = None if await acl.is_admin(m.from_user.id) else m.from_user.id
    r = await RUNNER.killall(scope)

    parts = [f"已终止 {r['running']} 个进行中、{r['queued']} 个排队中的任务"]
    if r["files"]:
        parts.append(f"清理临时文件 {r['files']} 个")
    parts.append("队列已清空，服务继续运行。")
    await m.reply("⛔ " + "\n".join(parts))


@router.message(Command("logout"))
async def cmd_logout(m: Message) -> None:
    if not await _allowed(m):
        return
    await POOL.logout(m.from_user.id)
    await m.reply("已从 Telegram 服务端撤销该 session 并清除本地记录。")


@router.message(F.text)
async def on_text(m: Message) -> None:
    if not await _allowed(m):
        return
    links = find_links(m.text or "")
    if not links:
        # 没有链接时，看看是不是「@对话 条数」这种抓取写法
        parsed = parse_grab_target(m.text or "")
        if parsed:
            await do_grab(m, *parsed)
        else:
            await m.reply(
                "没看到消息链接。\n\n"
                "发一条 <code>t.me/...</code> 链接，"
                "或者发 <code>@对话名</code> 抓取私聊内容。")
        return

    row = await db.get_user(m.from_user.id)
    if not row or row["session_status"] != "ok":
        await m.reply("还没有可用的登录凭据。请在服务器上运行 python login.py。")
        return

    # 消息里单独出现 nosp 时，把标记写进每条链接本身，
    # 这样它能随任务落库，重试和重启后依然有效。
    nosp = wants_nosp(m.text or "")

    ok = 0
    for link in links:
        if nosp:
            link = with_nosp(link)
        try:
            parse_link(link)
        except ParseError as e:
            await m.reply(f"跳过 {link}\n原因：{e}")
            continue
        await RUNNER.submit(m.from_user.id, link, m.chat.id, m.message_id)
        ok += 1

    if ok > 1:
        await m.reply(f"已接收 {ok} 条链接，按顺序处理。")


async def _allowed(m: Message) -> bool:
    if m.from_user is None:
        return False
    verdict = await acl.check(m.from_user.id)
    if verdict is acl.Access.OK:
        return True
    if verdict is acl.Access.NEED_APPLY:
        await admin.request_access(m.bot, m.from_user)
        await m.reply("已向管理员提交使用申请，请等待审批。")
        return False
    await m.reply(acl.DENY_TEXT.get(verdict, "无权限。"))
    return False


async def main() -> None:
    global RUNNER

    await db.init()
    orphans = await db.orphan_tmp_files()
    if orphans:
        n = streamer.cleanup_orphans(orphans)
        log.info("清理崩溃遗留临时文件 %d 个", n)

    bot = Bot(CFG.bot_token,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    log.info("bot @%s 已就绪", me.username)

    await _check_relay(bot)

    dp = Dispatcher()
    dp.include_router(admin.router)
    dp.include_router(router)

    await POOL.start()
    RUNNER = Runner(bot)
    await RUNNER.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    poll = asyncio.create_task(
        dp.start_polling(bot, handle_signals=False, allowed_updates=[
            "message", "callback_query"])
    )
    await stop.wait()
    log.info("收到退出信号，正在收尾…")
    poll.cancel()
    await RUNNER.stop()
    await POOL.stop()
    await db.close()
    await bot.session.close()


async def _check_relay(bot: Bot) -> None:
    """启动时验证中转频道配置是否正确，不对就立刻报错，别等到跑任务时才发现。"""
    try:
        chat = await bot.get_chat(CFG.relay_channel_id)
        me = await bot.get_chat_member(CFG.relay_channel_id, (await bot.get_me()).id)
        if me.status not in ("administrator", "creator"):
            log.error("bot 在中转频道《%s》里不是管理员，请去频道设置里加管理员。",
                      chat.title)
            sys.exit(1)
        log.info("中转频道《%s》正常", chat.title)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        log.error(
            "无法访问中转频道 %s：%s\n"
            "请检查 .env 里的 RELAY_CHANNEL_ID，并确认 bot 已被加为该频道管理员。",
            CFG.relay_channel_id, e,
        )
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
