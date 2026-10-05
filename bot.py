#!/usr/bin/env python3
"""TgSaver 主入口。

单进程、单 event loop：
  aiogram bot（长轮询）  +  Telethon session 池  +  双通道调度器
"""
from __future__ import annotations

import asyncio
from typing import Optional
from dataclasses import dataclass
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

GRAB_MAX = 20      # 单次最多抓几条，再多容易触发 FloodWait
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

论坛话题的链接后面加数字，抓话题里的内容：
<code>t.me/c/1234567890/45 1-7</code>  话题里最近的第 1~7 条
<code>t.me/c/1234567890/45 4632-4638</code>  按消息 id（4 位以上）

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
所以改发<b>对话地址</b>，后面跟数字：

<code>t.me/some_bot 5</code>  最近 5 条，各发各的
<code>some_bot 5</code>  以 bot 结尾的用户名可以不加前缀
<code>t.me/some_bot 1 3 5</code>  第 1、3、5 条，拼成一个相册
<code>t.me/some_bot 1-3 7</code>  区间也行
<code>.a_bot 1-2 .b_bot 3</code>  一次写多个对话，一起拼
<code>t.me/c/1234567890 3</code>  私有频道
<code>123456789 3</code>  数字 id

一个相册算一条。拼好的相册会把所有文字汇总到说明里，带序号，
同一来源归在一个引用块中；图片视频、文件、音频按 Telegram 的规则分开成组，
超过 10 个自动拆成多条。

<code>@some_bot 5</code> 不推荐：Telegram 会把它拦成 inline 查询，发不出去。

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
#
# 整条消息按空格切成词，逐个识别：目标词开启一组，后面跟的数字和区间
# 归这一组。所以一条消息里可以写多个对话：.a 1 .b 2-4
_T_TME = re.compile(
    r"^(?:https?://)?(?:www\.)?t\.me/(c/)?(@?[A-Za-z0-9_]{4,32}|-?\d{4,})/?$",
    re.IGNORECASE)
_T_DOT = re.compile(r"^[.>]@?([A-Za-z][A-Za-z0-9_]{3,31})$")   # .some_bot 手机上打得快
_T_AT = re.compile(r"^@([A-Za-z][A-Za-z0-9_]{3,31})$")         # 可能被 inline 拦截
# 对话 id：负数（频道、群）任何位置都认；正数至少 6 位，且只能写在开头 ——
# 后面的 4 位以上正数是消息 id，否则两者分不清。
_T_NUMID = re.compile(r"^(-?\d{6,})$")
# 论坛话题：t.me/c/群/话题、t.me/群名/话题。单独出现时是普通消息链接，
# 只有后面跟着数字时才当作「话题」这个抓取目标。
_T_TOPIC = re.compile(
    r"^(?:https?://)?(?:www\.)?t\.me/(?:(c)/(\d{4,})|([A-Za-z][A-Za-z0-9_]{3,31}))"
    r"/(\d+)/?$", re.IGNORECASE)
# 不带任何前缀的用户名只认以 bot 结尾的 —— Telegram 规定 bot 用户名必须
# 以 bot 结尾，所以 "hello 5" 这种普通文字不会被误判成抓取指令。
_T_BOT = re.compile(r"^(?=.{5,32}$)([A-Za-z][A-Za-z0-9_]*bot)$", re.IGNORECASE)

_N_ONE = re.compile(r"^(\d{1,3})$")                    # 第几条
_N_RANGE = re.compile(r"^(\d{1,3})-(\d{1,3})$")
_ID_ONE = re.compile(r"^(\d{4,})$")                    # 消息 id
_ID_RANGE = re.compile(r"^(\d{4,})-(\d{4,})$")
ID_RANGE_MAX = 100      # 一个 id 区间最多展开这么多条
POS_MAX = 50        # 单个对话最多挑这么多条，防止写成 1-999


@dataclass
class GrabSpec:
    """一个对话（或论坛话题）要抓什么。count 和 picks 二选一。"""
    target: str
    count: Optional[int] = None             # 单个数字：最近 N 条（原有逻辑）
    picks: Optional[list[tuple[str, int]]] = None   # ("pos", 第几条) / ("id", 消息 id)，按写的顺序
    topic: Optional[int] = None             # 论坛话题 id；None 表示整个对话

    @property
    def is_legacy(self) -> bool:
        return self.picks is None

    @property
    def positions(self) -> Optional[list[int]]:
        if self.picks is None:
            return None
        return [n for k, n in self.picks if k == "pos"]

    @property
    def ids(self) -> list[int]:
        return [n for k, n in (self.picks or []) if k == "id"]


def _clamp(n: str | None) -> int:
    return max(1, min(int(n), GRAB_MAX)) if n else 1


def _grab_target(tok: str, first: bool = True) -> Optional[str]:
    """一个词是不是抓取目标，是就返回规整后的对话标识。

    first=False 时不认正数对话 id：那个位置上的 4 位以上正数是消息 id。
    """
    mm = _T_TME.match(tok)
    if mm:
        is_channel, peer = mm.group(1), mm.group(2).lstrip("@")
        if is_channel:
            # t.me/c/ 里的数字是频道 id，要补回 -100 前缀
            if not peer.lstrip("-").isdigit():
                return None
            return peer if peer.startswith("-100") else f"-100{peer.lstrip('-')}"
        return peer
    for rx in (_T_DOT, _T_AT, _T_NUMID, _T_BOT):
        mm = rx.match(tok)
        if mm:
            if rx is _T_NUMID and not first and not mm.group(1).startswith("-"):
                continue
            return mm.group(1)
    return None


def _topic_target(tok: str) -> Optional[tuple[str, int]]:
    """t.me/c/群/话题 或 t.me/群名/话题 -> (对话, 话题 id)。"""
    mm = _T_TOPIC.match(tok)
    if not mm:
        return None
    if mm.group(1):
        return f"-100{mm.group(2)}", int(mm.group(4))
    return mm.group(3), int(mm.group(4))


def _number(tok: str):
    """数字词 -> ("pos"|"id", n 或 (a, b))，不是数字返回 None。"""
    for rx, kind in ((_N_RANGE, "pos"), (_N_ONE, "pos"),
                     (_ID_RANGE, "id"), (_ID_ONE, "id")):
        mm = rx.match(tok)
        if mm:
            if mm.lastindex == 2:
                return kind, (int(mm.group(1)), int(mm.group(2)))
            return kind, int(mm.group(1))
    return None


def _finish_spec(target: str, nums: list, as_positions: bool = False,
                 topic: Optional[int] = None) -> Optional[GrabSpec]:
    """nums 里每项是 ("pos"|"id", n) 或 ("pos"|"id", (a, b))。

    只有一个「第几条」数字时是原有的「最近 N 条」；as_positions=True 时
    即便只有一个数字也按位置理解。
    """
    if not nums and not as_positions:
        return GrabSpec(target, count=1, topic=topic)
    if (not as_positions and len(nums) == 1 and nums[0][0] == "pos"
            and isinstance(nums[0][1], int)):
        return GrabSpec(target, count=max(1, min(nums[0][1], GRAB_MAX)), topic=topic)
    nums = nums or [("pos", 1)]
    picks: list[tuple[str, int]] = []
    for kind, n in nums:
        if isinstance(n, int):
            seq = [n]
        else:
            lo, hi = min(n), max(n)
            cap = POS_MAX if kind == "pos" else ID_RANGE_MAX
            seq = range(lo, min(hi, lo + cap - 1) + 1)
        for k in seq:
            if k >= 1 and (kind, k) not in picks:
                picks.append((kind, k))
    # 「第几条」最多 POS_MAX 个；消息 id 的上限已在展开区间时限住
    pos_seen, kept = 0, []
    for x in picks:
        if x[0] == "pos":
            pos_seen += 1
            if pos_seen > POS_MAX:
                continue
        kept.append(x)
    if not kept:
        return None
    return GrabSpec(target, picks=kept, topic=topic)


def parse_grab_command(text: str) -> Optional[list[GrabSpec]]:
    """认出一条消息里的所有抓取目标，认不出返回 None。

        t.me/some_bot            最近 1 条
        t.me/some_bot 5          最近 5 条              （原有逻辑，各发各的）
        t.me/some_bot 1 3 5      第 1、3、5 条          （组装成相册）
        t.me/some_bot 1-3 7      第 1、2、3、7 条
        .a 1 .b 2-4              两个对话，一起组装
        some_bot 2               不带前缀只认以 bot 结尾的
        t.me/c/123/45 1-7        论坛话题 45 里的第 1~7 条
        t.me/c/123/45 4632-4638  消息 id（4 位以上的数字按消息 id 理解）

    有任何一个词认不出来，整条都不算抓取指令 —— 宁可不认，也别猜错。
    """
    toks = (text or "").split()
    if not toks:
        return None
    groups: list[tuple[tuple[str, Optional[int]], list]] = []
    for i, tok in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        # 话题链接单独出现时是普通消息链接，后面跟数字才是抓取目标
        tp = _topic_target(tok) if _number(nxt) else None
        if tp is not None:
            groups.append(((tp[0], tp[1]), []))
            continue
        t = _grab_target(tok, first=(i == 0))
        if t is not None:
            groups.append(((t, None), []))
            continue
        if not groups:
            return None
        num = _number(tok)
        if num is None:
            return None
        groups[-1][1].append(num)

    # 同一个对话出现多次（.a 1 .a 2），意图是「第 1 条和第 2 条」而不是
    # 「最近 1 条 + 最近 2 条」（会重叠）。合并起来，数字一律按位置理解。
    order: list = []
    merged: dict = {}
    seen_twice: set = set()
    for key, nums in groups:
        if key in merged:
            seen_twice.add(key)
            merged[key].extend(nums or [("pos", 1)])
        else:
            order.append(key)
            merged[key] = list(nums)

    specs: list[GrabSpec] = []
    for key in order:
        target, topic = key
        sp = _finish_spec(target, merged[key], as_positions=key in seen_twice,
                          topic=topic)
        if sp is None:
            return None
        specs.append(sp)
    return specs


def parse_grab_target(text: str):
    """兼容旧接口：只有「单个对话 + 单个数字」时返回 (对话, 条数)，否则 None。"""
    specs = parse_grab_command(text)
    if specs and len(specs) == 1 and specs[0].is_legacy:
        return specs[0].target, specs[0].count
    return None


GRAB_HELP = (
    "抓私聊内容，直接发对话地址就行，不用带命令：\n\n"
    "<code>t.me/some_bot 5</code>  最近 5 条，各发各的\n"
    "<code>some_bot 5</code>  以 bot 结尾的可以不加前缀\n"
    "<code>t.me/some_bot 1 3 5</code>  第 1、3、5 条，拼成相册\n"
    "<code>t.me/some_bot 1-3 7</code>  区间也行\n"
    "<code>.a_bot 1-2 .b_bot 3</code>  多个对话一起拼\n\n"
    "一个相册算一条，会往回翻 200 条消息找媒体。"
)


async def _scan_items(client, entity, need: int, topic: Optional[int] = None) -> list:
    """从新到旧列出带媒体的消息，相册只算一条。最多翻 GRAB_SCAN 条。

    topic 给了就只在那个论坛话题里翻。
    """
    picked, seen_groups = [], set()
    kw = {"reply_to": topic} if topic else {}
    async for msg in client.iter_messages(entity, limit=GRAB_SCAN, **kw):
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


async def _resolve_picks(client, entity, sp) -> tuple[list, list[str]]:
    """按写的顺序把「第几条」和「消息 id」换成具体消息。返回 (消息, 缺失说明)。

    消息 id 指到相册里的某一张时，同一个相册只取一次 —— 下游会凑齐整组，
    否则 4632-4638 刚好覆盖一个 7 张图的相册时会把它重复搬 7 次。
    """
    positions = sp.positions or []
    items = (await _scan_items(client, entity, max(positions), sp.topic)
             if positions else [])
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
    return picks, missing


async def do_grab(m: Message, specs: list, fw_to: Optional[str] = None) -> None:
    """按对话直接寻址抓取。

    私聊没有 t.me 链接 —— Telegram 只为公开频道和超级群生成链接。
    这里把找到的消息合成内部伪链接丢进同一套队列，
    下游的传输和投递逻辑完全复用。

    单个对话 + 单个数字：原有逻辑，最近 N 条各发各的。
    其余情况（多个数字、区间、多个对话）：挑出来的内容组装成相册。
    """
    uid = m.from_user.id
    row = await db.get_user(acl.session_user(uid))
    if not row or row["session_status"] != "ok":
        await m.reply("还没有可用的登录凭据，请联系机主。")
        return

    legacy = len(specs) == 1 and specs[0].is_legacy
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

                if sp.is_legacy:
                    items = await _scan_items(client, entity, sp.count, sp.topic)
                    picks = list(reversed(items))   # 由旧到新，保持原顺序
                else:
                    picks, missing = await _resolve_picks(client, entity, sp)
                    if missing:
                        notes.append(f"{sp.target} 没有 {'、'.join(missing)}")
                if not picks and sp.is_legacy:
                    notes.append(f"{sp.target} 最近 {GRAB_SCAN} 条消息里没找到媒体")
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
    if legacy or len(links) == 1:
        for link in links:
            await RUNNER.submit(uid, link, m.chat.id, m.message_id,
                                forward_to=fw_to)
        await status.edit_text(f"已找到 {len(links)} 条，正在处理…" + note)
        return

    # 多条组装：一个任务、一次投递
    await RUNNER.submit(uid, " ".join(links), m.chat.id, m.message_id,
                        forward_to=fw_to)
    await status.edit_text(f"已找到 {len(links)} 条，正在组装…" + note)


@router.message(Command("grab"))
async def cmd_grab(m: Message, command: CommandObject) -> None:
    """/grab 保留作为别名，但直接发 `@对话 条数` 更省事。"""
    if not await _allowed(m):
        return
    specs = parse_grab_command(command.args or "")
    if specs is None:
        await m.reply(GRAB_HELP)
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
