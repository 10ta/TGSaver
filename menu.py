"""Telegram 命令菜单。

注册之后，输入框旁边会出现 Menu 按钮，打 `/` 也会弹出候选列表。

用的是 setMyCommands 的作用域机制：
  默认作用域   所有人可见 —— 只列普通功能
  机主的私聊   单独覆盖 —— 额外列出管理命令

作用域是 Telegram 服务端记住的，不是每次消息现算的，所以管理命令
不会出现在授权用户的菜单里。注意这只是**显示**层面的隐藏，真正的
鉴权在 admin._guard 里，两者不能互相替代。
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    MenuButtonCommands,
)

from config import CFG

log = logging.getLogger("menu")

# 所有人可见。描述要短，菜单里一行显示得下。
PUBLIC: list[tuple[str, str]] = [
    ("status", "📊 登录状态、流量与消息统计"),
    ("killall", "⛔ 终止进行中的任务并清空队列"),
    ("help", "❓ 使用说明"),
]

# 只有机主看得到
OWNER_ONLY: list[tuple[str, str]] = [
    ("users", "👥 列出所有授权用户"),
    ("adduser", "➕ 添加用户，如 /adduser 123456789"),
    ("deluser", "➖ 移除用户"),
    ("ban", "🚫 封禁用户"),
    ("unban", "✅ 解封用户"),
    ("queue", "📋 队列状态"),
    ("stats", "📈 全局统计"),
    ("admin", "🔧 管理命令说明"),
    ("logout", "🔑 注销登录凭据"),
]


def _cmds(pairs: list[tuple[str, str]]) -> list[BotCommand]:
    return [BotCommand(command=c, description=d) for c, d in pairs]


async def setup(bot: Bot) -> None:
    """启动时注册。失败不影响 bot 运行，只是菜单没有而已。"""
    try:
        await bot.set_my_commands(_cmds(PUBLIC),
                                  scope=BotCommandScopeDefault())
        log.info("命令菜单已注册（公开 %d 条）", len(PUBLIC))
    except Exception as e:  # noqa: BLE001
        log.warning("注册公开命令菜单失败: %s", e)

    try:
        await bot.set_my_commands(
            _cmds(PUBLIC + OWNER_ONLY),
            scope=BotCommandScopeChat(chat_id=CFG.owner_id),
        )
        log.info("机主命令菜单已注册（共 %d 条）",
                 len(PUBLIC) + len(OWNER_ONLY))
    except Exception as e:  # noqa: BLE001
        # 机主还没跟 bot 说过话时会走到这里，属正常情况
        log.info("机主命令菜单待注册（机主与 bot 有过对话后自动生效）: %s", e)

    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as e:  # noqa: BLE001
        log.debug("设置菜单按钮失败: %s", e)


async def ensure_owner_menu(bot: Bot) -> None:
    """机主第一次跟 bot 说话时补一次注册。

    启动时机主若没有和 bot 的会话，setMyCommands 会失败；
    等他发第一条消息再补上，之后就一直有了。
    """
    try:
        await bot.set_my_commands(
            _cmds(PUBLIC + OWNER_ONLY),
            scope=BotCommandScopeChat(chat_id=CFG.owner_id),
        )
    except Exception as e:  # noqa: BLE001
        log.debug("补注册机主菜单失败: %s", e)
