"""权限判定。

>>> 日后开放多用户，只需要改这个文件。 <<<

当前 MULTI_USER=False，只有 OWNER 能用。把它改成 True 之后：
  - 审批流程自动生效（陌生人申请 -> owner 点按钮通过）
  - /adduser /ban 等命令自动对已通过的用户生效
  - 每个用户使用自己的 session 和自己的中转频道
其余模块（队列、传输、投递）全部已经按 owner_id 参数化，不需要改动。
"""
from __future__ import annotations

from enum import Enum

import db
from config import CFG

# ============================================================
#  多用户总开关。现在是 False。
# ============================================================
MULTI_USER = False


class Access(Enum):
    OK = "ok"                 # 放行
    NOT_ALLOWED = "no"        # 单用户模式下的非 owner
    PENDING = "pending"       # 已申请，等审批
    BANNED = "banned"         # 被封
    NEED_APPLY = "apply"      # 陌生人，可以走申请流程


def is_owner(user_id: int) -> bool:
    return user_id == CFG.owner_id


async def is_admin(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    if not MULTI_USER:
        return False
    row = await db.get_user(user_id)
    return bool(row and row["role"] in ("owner", "admin") and row["status"] == "active")


async def check(user_id: int) -> Access:
    """判断某个 user 能否使用 bot。"""
    if is_owner(user_id):
        return Access.OK

    if not MULTI_USER:
        return Access.NOT_ALLOWED

    row = await db.get_user(user_id)
    if row is None:
        return Access.NEED_APPLY
    if row["status"] == "active":
        return Access.OK
    if row["status"] == "banned":
        return Access.BANNED
    return Access.PENDING


async def relay_channel_for(user_id: int) -> int:
    """该用户的中转频道。单用户模式下永远是 .env 里配的那个。"""
    row = await db.get_user(user_id)
    if row and row["relay_channel_id"]:
        return int(row["relay_channel_id"])
    return CFG.relay_channel_id


DENY_TEXT = {
    Access.NOT_ALLOWED: "这个 bot 目前只对机主开放。",
    Access.BANNED: "你的访问权限已被撤销。",
    Access.PENDING: "你的申请正在等待管理员审批，请稍候。",
}
