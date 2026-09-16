"""权限判定。

模型很简单：机主 + 一份白名单，两种身份，全部共用机主的登录凭据。

>>> 这里有一个必须清楚的前提 <<<
白名单用户提交的任务，是用**机主的账号**去取消息的。也就是说，
被授权的人能读到机主账号能读到的一切 —— 包括机主的私有频道，
以及用 `t.me/对话名 5` 抓取机主和任意人 / bot 的私聊记录。

所以白名单只应该放机主自己的其他账号，或完全信任的人。
这是刻意的取舍（省掉了每人单独登录、每人单独中转频道的全部复杂度），
不是疏漏。

日后若要改成每人用自己的凭据，改动集中在 session_user()：
让它返回 uid 本身而不是机主 id，再补一套登录流程即可。
其余模块都已经通过它取凭据，不需要动。
"""
from __future__ import annotations

from enum import Enum

import db
from config import CFG


class Access(Enum):
    OK = "ok"
    NOT_ALLOWED = "no"
    BANNED = "banned"


def is_owner(user_id: int) -> bool:
    """只有机主一个特权身份，没有中间层。

    管理能力全部收在机主手里：增删用户、看全局统计、终止全部任务、
    注销凭据。授权用户只能用功能、看自己的用量、终止自己的任务。
    """
    return user_id == CFG.owner_id


async def check(user_id: int) -> Access:
    """这个人能不能用 bot。"""
    if is_owner(user_id):
        return Access.OK
    row = await db.get_user(user_id)
    if row is None:
        return Access.NOT_ALLOWED
    if row["status"] == "active":
        return Access.OK
    if row["status"] == "banned":
        return Access.BANNED
    return Access.NOT_ALLOWED


def session_user(user_id: int) -> int:
    """执行这个人的任务时，该用谁的登录凭据。

    当前一律用机主的。请求者 id 仍按原样记账，
    所以 /status 显示的是各人自己的用量。
    """
    return CFG.owner_id


async def relay_channel_for(user_id: int) -> int:
    """中转频道。凭据是谁的，频道就是谁的。"""
    row = await db.get_user(session_user(user_id))
    if row and row["relay_channel_id"]:
        return int(row["relay_channel_id"])
    return CFG.relay_channel_id


DENY_TEXT = {
    Access.NOT_ALLOWED: "无权使用。",
    Access.BANNED: "你的访问权限已被撤销。",
}
