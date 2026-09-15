#!/usr/bin/env python3
"""TgSaver 主入口。

单进程、单 event loop：
  aiogram bot（长轮询）  +  Telethon session 池  +  双通道调度器
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

import acl
import admin
import db
import streamer
from config import CFG
from parser import ParseError, find_links, parse_link
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

HELP = """把 Telegram 消息链接发给我，我原样取回来给你。

支持公开 / 私有 / 论坛话题链接，相册自动整组，
禁止转存的内容也能搬，大文件不受 50MB 限制。

/status  查看登录状态与队列
/logout  注销登录凭据
/help    本说明"""


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
    s = RUNNER.stats() if RUNNER else {"fast": 0, "slow": 0, "uptime": 0}
    up = s["uptime"]
    await m.reply(
        f"凭据：{sess.get(row['session_status'] if row else 'none', '未知')}\n"
        f"队列：快 {s['fast']} · 慢 {s['slow']}\n"
        f"中转频道：<code>{await acl.relay_channel_for(m.from_user.id)}</code>\n"
        f"运行：{up // 3600}h{(up % 3600) // 60}m"
    )


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
        await m.reply("没看到消息链接。发一条 t.me/... 给我试试。")
        return

    row = await db.get_user(m.from_user.id)
    if not row or row["session_status"] != "ok":
        await m.reply("还没有可用的登录凭据。请在服务器上运行 python login.py。")
        return

    ok = 0
    for link in links:
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
