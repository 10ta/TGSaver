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
import menu
import streamer
import tweet
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

HELP = """<b>TgSaver</b> — 把 Telegram 消息原样取回来给你。

<b>① 保存消息</b>
直接发消息链接，一条消息里可以贴多个：

<code>t.me/频道名/123</code>
<code>t.me/c/1234567890/123</code>  私有频道
<code>t.me/频道名/45/123</code>  论坛话题
<code>t.me/频道名/181?comment=4832</code>  评论区

相册自动整组，禁止转存的内容也能搬，大文件不受 50MB 限制。

<b>② 去掉剧透遮罩</b>
链接后面加 <code>nosp</code>：

<code>t.me/频道名/123 nosp</code>

会重新下载上传，比直转慢。受保护的内容本来就是重传，遮罩一律自动去掉，不用加。

<b>③ 保存推文</b>
直接发 X / Twitter 帖子链接：

<code>x.com/用户/status/123</code>
<code>fxtwitter.com/用户/status/123</code>  twitter / vxtwitter / fixupx 等也认

整理成引用块「昵称 : 正文」+ 原帖链接 · #ID，
原帖的图片视频以大图预览显示在消息上方；
预览抓不到媒体时（比如长视频）自动改发原始媒体。
一次发多条链接会尽量拼成一条消息，昵称和链接后带媒体序号。

<b>④ 抓私聊内容</b>
私聊里的单条消息没有链接（Telegram 只给公开频道和超级群生成），
所以改发<b>对话地址</b>，后面跟要抓几条：

<code>t.me/some_bot 5</code>  抓最近 5 条媒体
<code>t.me/some_bot</code>  不写数字就抓 1 条
<code>https://t.me/some_bot 3</code>  完整 URL 也行
<code>t.me/c/1234567890 3</code>  私有频道，自动补 -100 前缀
<code>.some_bot 5</code>  点号简写，手机上打字更快
<code>123456789 3</code>  数字 id

<code>@some_bot 5</code> 这种写法代码里也认，但<b>不推荐</b>——
Telegram 客户端看到消息以 @某bot 开头，会拦成对那个 bot 的 inline 查询，
消息根本发不出来。

单次最多 20 条，会往回翻 200 条消息找媒体。

<b>命令</b>
/status — 登录状态、流量与消息统计
/killall — 终止所有进行中的任务并清空队列
/logout — 注销登录凭据
/help — 本说明

机主另有 /admin 查看管理命令。"""


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(m: Message) -> None:
    if not await _allowed(m):
        return
    if acl.is_owner(m.from_user.id):
        # 启动时机主若还没和 bot 建立会话，管理菜单会注册失败，这里补一次
        await menu.ensure_owner_menu(m.bot)
    await m.reply(HELP)


@router.message(Command("status"))
async def cmd_status(m: Message) -> None:
    if not await _allowed(m):
        return
    row = await db.get_user(acl.session_user(m.from_user.id))
    sess = {"ok": "正常", "invalid": "已失效，需机主重新登录",
            "none": "未登录，需机主先登录"}
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


# 抓取目标的几种写法。
#
# 为什么不能只用 @name：Telegram 客户端看到消息以 "@botname " 开头
# 会拦截成对该 bot 的 inline 查询，消息根本发不出去。所以主推链接形式，
# 它既不触发 inline，也不用记语法 —— 从对话资料页直接复制就有。
GRAB_LINK_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?t\.me/(c/)?(@?[A-Za-z0-9_]{4,32}|-?\d{4,})/?"
    r"(?:\s+(\d{1,3}))?$", re.IGNORECASE)
# 前缀点号：.some_bot 5  —— 手机上比打链接快
GRAB_DOT_RE = re.compile(
    r"^[.>]\s*@?([A-Za-z][A-Za-z0-9_]{3,31})(?:\s+(\d{1,3}))?$")
# @name 仍然保留：用在 /grab 后面、或那个 bot 不支持 inline 时可以直接发
GRAB_USERNAME_RE = re.compile(
    r"^@([A-Za-z][A-Za-z0-9_]{3,31})(?:\s+(\d{1,3}))?$")
# 数字 id 至少 6 位。太短的数字更可能是用户随手打的东西，
# 当成对话 id 去解析只会得到一句莫名其妙的报错。
GRAB_NUMERIC_RE = re.compile(r"^(-?\d{6,})(?:\s+(\d{1,3}))?$")


def _clamp(n: str | None) -> int:
    return max(1, min(int(n), GRAB_MAX)) if n else 1


def parse_grab_target(text: str):
    """从一行裸文本里认出抓取目标，认不出返回 None。

    接受：
        t.me/some_bot        -> ("some_bot", 1)     推荐，不触发 inline
        t.me/some_bot 5      -> ("some_bot", 5)
        t.me/c/1234567890 3  -> ("-1001234567890", 3)
        .some_bot 5          -> ("some_bot", 5)
        @some_bot 5          -> ("some_bot", 5)     bot 支持 inline 时发不出去
        123456789 3          -> ("123456789", 3)
    """
    t = (text or "").strip()

    mm = GRAB_LINK_RE.match(t)
    if mm:
        is_channel, peer, n = mm.group(1), mm.group(2).lstrip("@"), mm.group(3)
        if is_channel:
            # t.me/c/ 里的数字是频道 id，要补回 -100 前缀
            if not peer.lstrip("-").isdigit():
                return None
            peer = peer if peer.startswith("-100") else f"-100{peer.lstrip('-')}"
        return peer, _clamp(n)

    for rx in (GRAB_DOT_RE, GRAB_USERNAME_RE, GRAB_NUMERIC_RE):
        mm = rx.match(t)
        if mm:
            return mm.group(1), _clamp(mm.group(2))
    return None


GRAB_HELP = (
    "抓私聊内容，直接发对话地址就行，不用带命令：\n\n"
    "<code>t.me/some_bot 5</code>  抓最近 5 条媒体\n"
    "<code>t.me/some_bot</code>  不写数字就抓 1 条\n"
    "<code>https://t.me/some_bot 3</code>  完整 URL 也行\n"
    "<code>t.me/c/1234567890 3</code>  私有频道，自动补 -100 前缀\n"
    "<code>.some_bot 5</code>  点号简写，手机上打字更快\n"
    "<code>123456789 3</code>  数字 id\n\n"
    "<code>@some_bot 5</code> 也认，但 Telegram 常把它拦成 inline 查询，"
    "消息发不出来，不推荐。\n\n"
    "单次最多 20 条，会往回翻 200 条消息找媒体。"
)


async def do_grab(m: Message, target: str, count: int) -> None:
    """按对话直接寻址抓取。

    私聊没有 t.me 链接 —— Telegram 只为公开频道和超级群生成链接。
    这里把找到的消息合成内部伪链接丢进同一套队列，
    下游的传输和投递逻辑完全复用。
    """
    row = await db.get_user(acl.session_user(m.from_user.id))
    if not row or row["session_status"] != "ok":
        await m.reply("还没有可用的登录凭据，请联系机主。")
        return

    status = await m.reply(f"正在查找 {target} 最近的内容…")
    try:
        client = await POOL.acquire(acl.session_user(m.from_user.id))
        async with POOL.lock_for(acl.session_user(m.from_user.id)):
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

    # 机主可以终止全部；授权用户只能终止自己的
    scope = None if acl.is_owner(m.from_user.id) else m.from_user.id
    r = await RUNNER.killall(scope)

    parts = [f"已终止 {r['running']} 个进行中、{r['queued']} 个排队中的任务"]
    if r["files"]:
        parts.append(f"清理临时文件 {r['files']} 个")
    if r.get("spared"):
        parts.append(f"其他用户的 {r['spared']} 个任务未受影响，继续执行。")
    parts.append("服务继续运行。")
    await m.reply("⛔ " + "\n".join(parts))


@router.message(Command("logout"))
async def cmd_logout(m: Message) -> None:
    """只有机主能注销。

    全体共用机主的那一份凭据，注销会让所有人都用不了，
    不该由任何一个授权用户单方面触发。
    """
    if not await _allowed(m):
        return
    if not acl.is_owner(m.from_user.id):
        await m.reply("只有机主能注销登录凭据。")
        return
    await POOL.logout(acl.session_user(m.from_user.id))
    await m.reply(
        "已从 Telegram 服务端撤销该 session 并清除本地记录。\n"
        "所有用户都将无法使用，直到机主重新运行 login.py。")


@router.message(F.text)
async def on_text(m: Message) -> None:
    if not await _allowed(m):
        return
    text = m.text or ""

    # 抓取判断必须排在 find_links 前面：t.me/some_bot 这种"只有对话名、
    # 没有消息 id"的地址也会被 find_links 抓到，然后在 parse_link 那里
    # 报一句"缺少消息 id"，用户就看不到抓取效果了。
    parsed = parse_grab_target(text)
    if parsed:
        await do_grab(m, *parsed)
        return

    tweets = tweet.find_links(text)
    links = find_links(text)
    if not links and not tweets:
        await m.reply(
            "没看到消息链接。\n\n"
            "发一条 <code>t.me/频道/123</code> 这样的消息链接，\n"
            "或者 <code>x.com/用户/status/123</code> 推文链接，\n"
            "或者发 <code>t.me/对话名 5</code> 抓取私聊内容。")
        return

    row = await db.get_user(acl.session_user(m.from_user.id))
    if not row or row["session_status"] != "ok":
        await m.reply("还没有可用的登录凭据，请联系机主。")
        return

    # 推文：多条合并成一个任务，拼成一条消息返回
    if tweets:
        await RUNNER.submit(m.from_user.id, " ".join(tweets),
                            m.chat.id, m.message_id)

    # 消息里单独出现 nosp 时，把标记写进每条链接本身，
    # 这样它能随任务落库，重试和重启后依然有效。
    nosp = wants_nosp(text)

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

    ok += 1 if tweets else 0
    if ok > 1:
        await m.reply(f"已接收 {ok} 条链接，按顺序处理。")


async def _allowed(m: Message) -> bool:
    if m.from_user is None:
        return False
    verdict = await acl.check(m.from_user.id)
    if verdict is acl.Access.OK:
        return True
    # 未授权的人不解释、不引导申请 —— 增删由机主主动完成
    await m.reply(acl.DENY_TEXT.get(verdict, "无权使用。"))
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
    await menu.setup(bot)

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
        dp.start_polling(bot, handle_signals=False,
                         allowed_updates=["message"])
    )
    await stop.wait()
    log.info("收到退出信号，正在收尾…")
    poll.cancel()
    await RUNNER.stop()
    await tweet.close()
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
