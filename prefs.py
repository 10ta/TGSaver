"""每个人自己的偏好设置，发 /setting 弹出一排按钮，点一下切换，立即生效。

存在 users.settings（JSON）里，只存改过的项；没改过的跟随默认值。
推文形态的默认值来自 .env 的 TWEET_MODE，所以老配置照样有效。

    keep_spoiler  剧透        默认去掉。打开后原样保留媒体遮罩和文字剧透
    tw_name       推文昵称    默认显示。关掉后引用块里只有正文
    tg_source     TG 来源名   默认隐藏。组装 Telegram 消息时，说明里写不写
                              来源的名字和 #用户名
    pack_delete   组装后删除  默认不删。/pack 之后删掉你转发来的原消息
    tweet_mode    推文形态    auto / preview / media 轮换
    fw 默认去向   只能清除；设置去向仍然用 `fw 去向`
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields, replace

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.filters import Command

import acl
import db
import tweet

log = logging.getLogger("prefs")

TWEET_MODES = ("auto", "preview", "media")


@dataclass(frozen=True)
class Prefs:
    keep_spoiler: bool = False
    tw_name: bool = True
    tg_source: bool = False
    pack_delete: bool = False
    tweet_mode: str = "auto"


def defaults() -> Prefs:
    mode = tweet.TWEET_MODE if tweet.TWEET_MODE in TWEET_MODES else "auto"
    return Prefs(tweet_mode=mode)


def _decode(raw) -> Prefs:
    base = defaults()
    if not raw:
        return base
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("设置数据损坏，按默认值处理")
        return base
    names = {f.name: f.type for f in fields(Prefs)}
    clean = {}
    for k, v in (data or {}).items():
        if k not in names:
            continue                        # 已废弃的项，忽略
        if k == "tweet_mode":
            if v in TWEET_MODES:
                clean[k] = v
        elif isinstance(v, bool):
            clean[k] = v
    return replace(base, **clean)


async def get(user_id: int) -> Prefs:
    row = await db.get_user(user_id)
    return _decode(row["settings"] if row else None)


async def update(user_id: int, **changes) -> Prefs:
    """改几项并保存。只存和默认值不同的项，默认值以后改了也能跟上。"""
    cur = replace(await get(user_id), **changes)
    base = defaults()
    diff = {k: v for k, v in asdict(cur).items() if getattr(base, k) != v}
    await db.upsert_user(user_id, settings=json.dumps(diff) if diff else None)
    return cur


# ------------------------------------------------------------------ 界面

_MODE_TEXT = {"auto": "自动", "preview": "预览", "media": "原始媒体"}

SETTING_TEXT = (
    "<b>⚙️ 设置</b>　点按钮切换，立即生效\n\n"
    "<b>剧透</b>　保留或去掉原消息的剧透遮罩和文字剧透，零流量\n"
    "<b>推文昵称</b>　推文引用块里写不写昵称\n"
    "<b>TG 来源名</b>　组装 Telegram 消息时，说明里写不写来源名和 #用户名\n"
    "<b>组装后删除</b>　/pack 之后删掉你转发来的原消息\n"
    "<b>推文形态</b>　自动：预览有媒体就发预览，没有就发原始媒体"
)


def keyboard(p: Prefs, last_forward: str | None) -> InlineKeyboardMarkup:
    def btn(text: str, key: str) -> list[InlineKeyboardButton]:
        return [InlineKeyboardButton(text=text, callback_data=f"pf:{key}")]

    rows = [
        btn(f"剧透：{'保留' if p.keep_spoiler else '去掉'}", "keep_spoiler"),
        btn(f"推文昵称：{'显示' if p.tw_name else '隐藏'}", "tw_name"),
        btn(f"TG 来源名：{'显示' if p.tg_source else '隐藏'}", "tg_source"),
        btn(f"组装后删除原消息：{'是' if p.pack_delete else '否'}", "pack_delete"),
        btn(f"推文形态：{_MODE_TEXT[p.tweet_mode]}", "tweet_mode"),
    ]
    if last_forward:
        rows.append(btn(f"fw 默认去向：{last_forward}（点击清除）", "fw_clear"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def toggle(p: Prefs, key: str) -> dict:
    """按下某个按钮后要改的项。认不出的键返回空。"""
    if key == "tweet_mode":
        i = TWEET_MODES.index(p.tweet_mode)
        return {"tweet_mode": TWEET_MODES[(i + 1) % len(TWEET_MODES)]}
    if key in ("keep_spoiler", "tw_name", "tg_source", "pack_delete"):
        return {key: not getattr(p, key)}
    return {}


router = Router(name="prefs")


async def _ok(user_id: int) -> bool:
    return await acl.check(user_id) is acl.Access.OK


@router.message(Command("setting"))
@router.message(Command("settings"))
async def cmd_setting(m: Message) -> None:
    if m.from_user is None or not await _ok(m.from_user.id):
        return
    uid = m.from_user.id
    row = await db.get_user(uid)
    await m.reply(SETTING_TEXT, reply_markup=keyboard(
        await get(uid), row["last_forward"] if row else None))


@router.callback_query(F.data.startswith("pf:"))
async def on_press(q: CallbackQuery) -> None:
    uid = q.from_user.id
    if not await _ok(uid):
        await q.answer("无权使用。", show_alert=True)
        return
    key = q.data.split(":", 1)[1]
    if key == "fw_clear":
        await db.upsert_user(uid, last_forward=None)
        toast = "已清除 fw 默认去向"
        p = await get(uid)
    else:
        changes = toggle(await get(uid), key)
        if not changes:
            await q.answer()
            return
        p = await update(uid, **changes)
        toast = "已保存"
    row = await db.get_user(uid)
    try:
        await q.message.edit_reply_markup(
            reply_markup=keyboard(p, row["last_forward"] if row else None))
    except Exception as e:  # noqa: BLE001
        log.debug("刷新设置按钮失败: %s", e)
    await q.answer(toast)
