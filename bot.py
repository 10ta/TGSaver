#!/usr/bin/env python3
"""TgSaver 主入口。

单进程、单 event loop：
  aiogram bot（长轮询）  +  Telethon session 池  +  双通道调度器
"""
from __future__ import annotations

import asyncio
from typing import Optional
from dataclasses import dataclass, field
import html
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
import forward
import menu
import streamer
import tweet
from config import CFG
from parser import (
    ParseError, find_links, make_internal, parse_forward, parse_link,
    unrecognized_forward, wants_nosp, with_nosp,
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

GRAB_SCAN = 200    # 往回翻多少条消息去找媒体

HELP = """<b>TgSaver</b> — 把 Telegram 消息原样取回来给你。

<b>① 保存消息</b>
直接发消息链接：

<code>t.me/频道名/123</code>
<code>t.me/c/1234567890/123</code>  私有频道
<code>t.me/频道名/45/123</code>  论坛话题里的某条
<code>t.me/频道名/181?comment=4832</code>  评论区

一条消息里贴多个链接会拼成相册、汇总说明，一次返回。
相册自动整组，禁止转存的内容也能搬，大文件不受 50MB 限制。

论坛话题的链接后面加数字，抓话题里的内容；
链接最后一段写成区间，就是一段消息 id：
<code>t.me/c/1234567890/45 1-7</code>  话题里的第 1~7 条
<code>t.me/c/1234567890/45/4632-4638</code>  消息 4632~4638

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

<b>④ 转发到别处</b>
消息末尾加 <code>fw</code> 和去向，结果不发给你，直接进那个频道 / 群：

<code>x.com/用户/status/123 fw 我的频道</code>
<code>t.me/频道/456 fw -1001234567890</code>
<code>x.com/用户/status/123 fw</code>  用上次的去向

你照常收到那一份，之后额外转一份过去。
用账号转发并隐藏来源，看不到 bot；转发失败只提示，不影响已收到的内容。

<b>⑤ 抓私聊内容</b>
私聊里的单条消息没有链接（Telegram 只给公开频道和超级群生成），
所以改发<b>对话地址</b>，后面的数字<b>永远是「第几条」</b>：

<code>t.me/some_bot</code>  第 1 条（最近一条）
<code>t.me/some_bot 5</code>  第 5 条
<code>t.me/some_bot 1-5</code>  第 1~5 条
<code>t.me/some_bot 1 3 7-9</code>  混着写，按写的顺序
<code>.some_bot 1-5</code>  点号简写
<code>some_bot 1-5</code>  以 bot 结尾的可以不加前缀
<code>.a_bot 1-2 .b_bot 3</code>  一次写多个对话

一个相册算一条。超过一条就拼成相册，文字汇总到说明里，带序号；
图片视频、文件、音频按 Telegram 的规则分开成组，超过 10 个自动拆成多条。

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


# 抓取的写法。只有一条规则：**数字永远是「第几条」**。
#
#   t.me/some_bot              第 1 条（最近一条）
#   t.me/some_bot 5            第 5 条
#   t.me/some_bot 1-5          第 1~5 条
#   t.me/some_bot 1 3 7-9      混着写，按写的顺序
#   t.me/c/群/话题 1-5          论坛话题里的第 1~5 条
#   t.me/c/群/话题/4632-4638    消息 id 写进链接路径里，和普通消息链接一个形状
#   .a_bot 1-2 .b_bot 3        一次写多个对话
#
# 目标只认三种写法：t.me/…（标准）、.名字（手机上打得快）、名字_bot
# （不带前缀只认以 bot 结尾的 —— Telegram 规定 bot 用户名必须以 bot 结尾，
# 所以 "hello 5" 这种普通文字不会被误判）。对话 id 只认 -100… 或 t.me/c/…。
#
# 不用 @名字：Telegram 客户端看到消息以 "@botname " 开头会拦截成对该 bot
# 的 inline 查询，消息根本发不出去。
_T_TME = re.compile(
    r"^(?:https?://)?(?:www\.)?t\.me/(c/)?(@?[A-Za-z0-9_]{4,32}|-?\d{4,})/?$",
    re.IGNORECASE)
_T_DOT = re.compile(r"^\.([A-Za-z][A-Za-z0-9_]{3,31})$")
_T_NUMID = re.compile(r"^(-\d{6,})$")
_T_BOT = re.compile(r"^(?=.{5,32}$)([A-Za-z][A-Za-z0-9_]*bot)$", re.IGNORECASE)
# 论坛话题：t.me/c/群/话题、t.me/群名/话题。单独出现时是普通消息链接，
# 只有后面跟着数字时才当作「话题」这个抓取目标。
_T_TOPIC = re.compile(
    r"^(?:https?://)?(?:www\.)?t\.me/(?:(c)/(\d{4,})|([A-Za-z][A-Za-z0-9_]{3,31}))"
    r"/(\d+)/?$", re.IGNORECASE)
# 消息 id 区间：普通消息链接的最后一段写成 a-b
_T_IDRANGE = re.compile(
    r"^(?:https?://)?(?:www\.)?t\.me/(?:(c)/(\d{4,})|([A-Za-z][A-Za-z0-9_]{3,31}))"
    r"(?:/(\d+))?/(\d+)-(\d+)/?$", re.IGNORECASE)

_N_ONE = re.compile(r"^(\d{1,3})$")
_N_RANGE = re.compile(r"^(\d{1,3})-(\d{1,3})$")
# 4 位以上的裸数字：旧写法里的消息 id。现在不认，但要提示新写法
_OLD_ID = re.compile(r"^\d{4,}(?:-\d{4,})?$")

POS_MAX = 50            # 一个对话最多挑这么多条「第几条」
ID_RANGE_MAX = 100      # 一个 id 区间最多展开这么多条


@dataclass
class GrabSpec:
    """一个对话（或论坛话题）要抓什么。"""
    target: str
    picks: list[tuple[str, int]] = field(default_factory=list)  # ("pos"|"id", n)，按写的顺序
    topic: Optional[int] = None             # 论坛话题 id；None 表示整个对话

    @property
    def positions(self) -> list[int]:
        return [n for k, n in self.picks if k == "pos"]

    @property
    def ids(self) -> list[int]:
        return [n for k, n in self.picks if k == "id"]


def _chat_of(c_flag, digits, name) -> str:
    return f"-100{digits}" if c_flag else name


def _grab_target(tok: str) -> Optional[str]:
    """一个词是不是抓取目标，是就返回规整后的对话标识。"""
    mm = _T_TME.match(tok)
    if mm:
        is_channel, peer = mm.group(1), mm.group(2).lstrip("@")
        if is_channel:
            # t.me/c/ 里的数字是频道 id，要补回 -100 前缀
            if not peer.lstrip("-").isdigit():
                return None
            return peer if peer.startswith("-100") else f"-100{peer.lstrip('-')}"
        return peer
    for rx in (_T_DOT, _T_NUMID, _T_BOT):
        mm = rx.match(tok)
        if mm:
            return mm.group(1)
    return None


def _topic_target(tok: str) -> Optional[tuple[str, int]]:
    mm = _T_TOPIC.match(tok)
    if not mm:
        return None
    return _chat_of(mm.group(1), mm.group(2), mm.group(3)), int(mm.group(4))


def _id_range_target(tok: str) -> Optional[tuple[str, Optional[int], int, int]]:
    mm = _T_IDRANGE.match(tok)
    if not mm:
        return None
    topic = int(mm.group(4)) if mm.group(4) else None
    return (_chat_of(mm.group(1), mm.group(2), mm.group(3)), topic,
            int(mm.group(5)), int(mm.group(6)))


def _position(tok: str):
    mm = _N_RANGE.match(tok)
    if mm:
        return int(mm.group(1)), int(mm.group(2))
    mm = _N_ONE.match(tok)
    return int(mm.group(1)) if mm else None


def parse_grab_command(text: str) -> Optional[list[GrabSpec]]:
    """认出一条消息里的所有抓取目标，认不出返回 None。

    有任何一个词认不出来，整条都不算抓取指令 —— 宁可不认，也别猜错。
    同一个对话（同一个话题）写了多次会合并成一组。
    """
    toks = (text or "").split()
    if not toks:
        return None

    # 每组：[对话, 话题, 选择列表, 是否已锁定（id 区间后面不能再跟数字）]
    groups: list[list] = []
    for i, tok in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        idr = _id_range_target(tok)
        if idr is not None:
            chat, topic, lo, hi = idr
            groups.append([chat, topic, [("id", (lo, hi))], True])
            continue
        # 话题链接单独出现时是普通消息链接，后面跟数字才是抓取目标
        tp = _topic_target(tok) if _position(nxt) is not None else None
        if tp is not None:
            groups.append([tp[0], tp[1], [], False])
            continue
        t = _grab_target(tok)
        if t is not None:
            groups.append([t, None, [], False])
            continue
        num = _position(tok)
        if num is None or not groups or groups[-1][3]:
            return None
        groups[-1][2].append(("pos", num))

    merged: dict[tuple, list] = {}
    for chat, topic, sel, _ in groups:
        merged.setdefault((chat, topic), []).extend(sel or [("pos", 1)])

    specs = []
    for (chat, topic), sel in merged.items():
        picks: list[tuple[str, int]] = []
        for kind, n in sel:
            if isinstance(n, int):
                seq = [n]
            else:
                lo, hi = min(n), max(n)
                cap = POS_MAX if kind == "pos" else ID_RANGE_MAX
                seq = range(lo, min(hi, lo + cap - 1) + 1)
            for k in seq:
                if k >= 1 and (kind, k) not in picks:
                    picks.append((kind, k))
        pos_seen, kept = 0, []
        for x in picks:
            if x[0] == "pos":
                pos_seen += 1
                if pos_seen > POS_MAX:
                    continue
            kept.append(x)
        if not kept:
            return None
        specs.append(GrabSpec(chat, kept, topic))
    return specs


def old_syntax_hint(text: str) -> Optional[str]:
    """认出旧写法并给出新写法。不是旧写法返回 None。"""
    toks = (text or "").split()
    if not toks:
        return None
    if toks[0].startswith(("@", ">")) and len(toks[0]) > 1:
        name = toks[0].lstrip("@>")
        return (f"现在不用 <code>{toks[0]}</code> 这种写法了，"
                f"改用 <code>t.me/{name}</code> 或 <code>.{name}</code>。")
    is_tme = bool(_T_TME.match(toks[0]) or _T_TOPIC.match(toks[0]))
    if is_tme and len(toks) >= 2 and any(_OLD_ID.match(t) for t in toks[1:]):
        base = toks[0].split("?")[0].rstrip("/")
        ids = next(t for t in toks[1:] if _OLD_ID.match(t))
        return ("消息 id 现在写进链接路径里，和普通消息链接一个形状：\n"
                f"<code>{base}/{ids}</code>\n"
                "后面跟的数字一律表示「第几条」。")
    if toks[0].isdigit() and len(toks[0]) >= 6:
        return (f"对话 id 请写成 <code>-100…</code> 或 "
                f"<code>t.me/c/{toks[0]}</code> 的形式。")
    return None


GRAB_HELP = (
    "抓私聊内容，直接发对话地址就行，不用带命令。"
    "数字永远是「第几条」：\n\n"
    "<code>t.me/some_bot</code>  第 1 条（最近一条）\n"
    "<code>t.me/some_bot 1-5</code>  第 1~5 条\n"
    "<code>t.me/some_bot 1 3 7-9</code>  混着写，按写的顺序\n"
    "<code>.some_bot 1-5</code>  点号简写\n"
    "<code>some_bot 1-5</code>  以 bot 结尾的可以不加前缀\n"
    "<code>.a_bot 1-2 .b_bot 3</code>  多个对话\n\n"
    "超过一条就拼成相册。一个相册算一条，会往回翻 200 条消息找媒体。"
)


class ScanResult(list):
    """翻找结果。额外记下翻了多少条消息，抓不够数时用来解释原因。"""
    scanned: int = 0


async def _scan_items(client, entity, need: int,
                      topic: Optional[int] = None) -> ScanResult:
    """从新到旧列出带媒体的消息，相册只算一条。最多翻 GRAB_SCAN 条。

    topic 给了就只在那个论坛话题里翻。
    """
    picked, seen_groups = ScanResult(), set()
    kw = {"reply_to": topic} if topic else {}
    async for msg in client.iter_messages(entity, limit=GRAB_SCAN, **kw):
        picked.scanned += 1
        if msg.media is None or isinstance(msg, MessageService):
            continue
        gid = getattr(msg, "grouped_id", None)
        if gid is not None:
            if gid in seen_groups:
                continue          # 相册只取一条，下游会自动凑齐整组
            seen_groups.add(gid)
        picked.append(msg)
        if len(picked) >= need:
            break
    return picked


def _scan_note(items: "ScanResult", topic: Optional[int]) -> str:
    """抓不够数时的解释：到底翻了多少、找到多少。"""
    where = "这个话题" if topic else "这个对话"
    if items.scanned < GRAB_SCAN:
        return (f"{where}一共只有 {items.scanned} 条消息，"
                f"其中 {len(items)} 条带媒体（相册算一条）")
    return (f"{where}最近 {items.scanned} 条消息里只有 {len(items)} 条带媒体"
            f"（相册算一条），更早的没有翻")


async def _resolve_picks(client, entity, sp) -> tuple[list, list[str], str]:
    """按写的顺序把「第几条」和「消息 id」换成具体消息。

    返回 (消息, 缺失说明, 翻找统计)。翻找统计只在「第几条」不够数时给出。

    消息 id 指到相册里的某一张时，同一个相册只取一次 —— 下游会凑齐整组，
    否则 4632-4638 刚好覆盖一个 7 张图的相册时会把它重复搬 7 次。
    """
    positions = sp.positions or []
    items = (await _scan_items(client, entity, max(positions), sp.topic)
             if positions else ScanResult())
    by_id = {}
    if sp.ids:
        got = await client.get_messages(entity, ids=sp.ids)
        by_id = {m.id: m for m in (got or []) if m is not None}

    picks, missing, seen_groups, seen_ids = [], [], set(), set()
    for kind, n in sp.picks:
        if kind == "pos":
            msg = items[n - 1] if n <= len(items) else None
            label = f"第 {n} 条"
        else:
            msg = by_id.get(n)
            if msg is not None and isinstance(msg, MessageService):
                msg = None
            label = f"消息 {n}"
        if msg is None:
            missing.append(label)
            continue
        gid = getattr(msg, "grouped_id", None)
        if gid is not None:
            if gid in seen_groups:
                continue
            seen_groups.add(gid)
        if msg.id not in seen_ids:
            seen_ids.add(msg.id)
            picks.append(msg)
    short = positions and max(positions) > len(items)
    return picks, missing, (_scan_note(items, sp.topic) if short else "")


async def do_grab(m: Message, specs: list, fw_to: Optional[str] = None) -> None:
    """按对话直接寻址抓取。

    私聊没有 t.me 链接 —— Telegram 只为公开频道和超级群生成链接。
    这里把找到的消息合成内部伪链接丢进同一套队列，
    下游的传输和投递逻辑完全复用。

    挑出来只有一条就原样发；超过一条就组装成相册、汇总说明。
    """
    uid = m.from_user.id
    row = await db.get_user(acl.session_user(uid))
    if not row or row["session_status"] != "ok":
        await m.reply("还没有可用的登录凭据，请联系机主。")
        return

    names = "、".join(sp.target for sp in specs)
    status = await m.reply(f"正在查找 {names} 的内容…")

    chosen: list[tuple[int, int]] = []       # (peer, msg_id)，按最终顺序
    notes: list[str] = []
    try:
        client = await POOL.acquire(acl.session_user(uid))
        async with POOL.lock_for(acl.session_user(uid)):
            for sp in specs:
                try:
                    entity = await client.get_entity(
                        int(sp.target) if sp.target.lstrip("-").isdigit()
                        else sp.target)
                except Exception as e:  # noqa: BLE001
                    notes.append(f"找不到对话 {sp.target}（{type(e).__name__}）")
                    continue
                peer = utils.get_peer_id(entity)

                picks, missing, why = await _resolve_picks(client, entity, sp)
                if missing:
                    notes.append(f"{sp.target} 没有 {'、'.join(missing)}"
                                 + (f"\n{why}" if why else ""))
                for msg in picks:
                    key = (peer, msg.id)
                    if key not in chosen:
                        chosen.append(key)
    except NoSession:
        await status.edit_text("登录凭据已失效，请联系机主。")
        return

    note = ("\n⚠️ " + "；".join(notes)) if notes else ""
    if not chosen:
        await status.edit_text("没有抓到任何内容。" + note)
        return

    links = [make_internal(peer, mid) for peer, mid in chosen]
    # 一条就原样发；多条组成一个任务、拼相册、一次投递
    await RUNNER.submit(uid, " ".join(links), m.chat.id, m.message_id,
                        forward_to=fw_to)
    verb = "正在组装" if len(links) > 1 else "正在处理"
    await status.edit_text(f"已找到 {len(links)} 条，{verb}…" + note)


@router.message(Command("grab"))
async def cmd_grab(m: Message, command: CommandObject) -> None:
    """/grab 保留作为别名，但直接发 `@对话 条数` 更省事。"""
    if not await _allowed(m):
        return
    specs = parse_grab_command(command.args or "")
    if specs is None:
        await m.reply(old_syntax_hint(command.args or "") or GRAB_HELP)
        return
    await do_grab(m, specs)


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

    # fw 必须最先剥掉：`fw t.me/mychannel` 里的 t.me/mychannel 是转发去向，
    # 不是"抓取该对话最近 1 条"。
    has_fw, fw_spec, text = parse_forward(text)
    fw_to = None
    fw_bad = None if has_fw else unrecognized_forward(text)
    if has_fw:
        fw_to = await _resolve_forward(m, fw_spec)
        if fw_to is None:
            return
        if not text.strip():
            return                      # 只写了 fw，用途是设默认去向

    # 抓取判断必须排在 find_links 前面：t.me/some_bot 这种"只有对话名、
    # 没有消息 id"的地址也会被 find_links 抓到，然后在 parse_link 那里
    # 报一句"缺少消息 id"，用户就看不到抓取效果了。
    specs = parse_grab_command(text)
    if specs:
        await do_grab(m, specs, fw_to)
        return
    hint = old_syntax_hint(text)
    if hint:
        await m.reply(hint)
        return

    tweets = tweet.find_links(text)
    links = find_links(text)
    if not links and not tweets:
        await m.reply(
            "没看到消息链接。\n\n"
            "发一条 <code>t.me/频道/123</code> 这样的消息链接，\n"
            "或者 <code>x.com/用户/status/123</code> 推文链接，\n"
            "或者发 <code>t.me/对话名 1-5</code> 抓取私聊内容。")
        return

    row = await db.get_user(acl.session_user(m.from_user.id))
    if not row or row["session_status"] != "ok":
        await m.reply("还没有可用的登录凭据，请联系机主。")
        return

    if fw_bad:
        # 写了 fw 但去向看不懂：链接照常处理，只是不转发。
        # 明确说出来，免得以为转发了。只在有链接时提示，普通句子里的 fw 不打扰。
        await m.reply(
            f"fw 后面的 <code>{html.escape(fw_bad)}</code> 不像转发去向，"
            f"这次不转发。\n"
            f"去向可以写 <code>optv4</code>、<code>@optv4</code>、"
            f"<code>t.me/optv4</code> 或数字 id。")

    # 推文：多条合并成一个任务，拼成一条消息返回
    if tweets:
        await RUNNER.submit(m.from_user.id, " ".join(tweets),
                            m.chat.id, m.message_id, forward_to=fw_to)

    # 消息里单独出现 nosp 时，把标记写进每条链接本身，
    # 这样它能随任务落库，重试和重启后依然有效。
    nosp = wants_nosp(text)

    valid = []
    for link in links:
        if nosp:
            link = with_nosp(link)
        try:
            parse_link(link)
        except ParseError as e:
            await m.reply(f"跳过 {link}\n原因：{e}")
            continue
        valid.append(link)

    # 多个消息链接和推文一样，拼成相册、汇总说明，一次返回
    if valid:
        await RUNNER.submit(m.from_user.id, " ".join(valid), m.chat.id,
                            m.message_id, forward_to=fw_to)


async def _resolve_forward(m: Message, spec: str | None) -> str | None:
    """确定 fw 的去向并校验权限。出错时已经回复了用户，返回 None。

    没写去向就用上次的；校验通过后记下来，下次可以省略。
    """
    uid = m.from_user.id
    row = await db.get_user(uid)

    if not spec:
        last = row["last_forward"] if row else None
        if not last:
            await m.reply("还没用过 fw，第一次请指定去向：\n"
                          "<code>fw @频道名</code>")
            return None
        return last

    try:
        target = forward.normalize(spec)
    except forward.ForwardError as e:
        await m.reply(str(e))
        return None

    if target != (row["last_forward"] if row else None):
        # 换了去向就当场验一次，别等任务跑完才发现发不出去。
        # 凭据属于机主，所以要查机主那条记录 —— 查发起人自己的话，
        # 授权用户永远是"未登录"。
        owner_row = await db.get_user(acl.session_user(uid))
        if not owner_row or owner_row["session_status"] != "ok":
            await m.reply("还没有可用的登录凭据，请联系机主。")
            return None
        try:
            client = await POOL.acquire(acl.session_user(uid))
            async with POOL.lock_for(acl.session_user(uid)):
                await forward.resolve(client, target)
        except forward.ForwardError as e:
            await m.reply(str(e))
            return None
        except NoSession:
            await m.reply("登录凭据已失效，请联系机主。")
            return None
        await db.upsert_user(uid, last_forward=target)
        await m.reply(f"转发去向已设为 <code>{target}</code>")
    return target


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
