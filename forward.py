"""转发到指定去向。

用 user 账号的 forward_messages(drop_author=True) —— 也就是客户端上
「hide sender name」那个选项。这是服务端操作：不花流量、不带「转发自」
抬头、也不会露出 bot（转到频道显示频道自己的署名）。

消息先由 user 账号生成在中转频道里，再从那里转出去，所以中转频道那份
自然留作归档。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

log = logging.getLogger("forward")


class ForwardError(RuntimeError):
    """可以直接展示给用户的失败原因。"""


_TME = re.compile(r"^(?:https?://)?(?:www\.)?t\.me/(.+)$", re.IGNORECASE)


def normalize(spec: str) -> str:
    """把用户写的去向规整成一种形式：@用户名 或 数字 id。"""
    spec = (spec or "").strip()
    if not spec:
        raise ForwardError("没有指定转发去向。")

    m = _TME.match(spec)
    if m:
        path = m.group(1).strip("/")
        if path.startswith("+") or path.lower().startswith("joinchat/"):
            raise ForwardError(
                "邀请链接不能作为转发去向，请用 @用户名 或数字 id。")
        if path.lower().startswith("c/"):
            digits = path.split("/")[1] if "/" in path else ""
            if not digits.isdigit():
                raise ForwardError("这个 t.me/c/ 链接里没有频道 id。")
            return f"-100{digits}"
        spec = "@" + path.split("/")[0]

    if spec.startswith("@"):
        if not re.fullmatch(r"@[A-Za-z0-9_]{4,32}", spec):
            raise ForwardError(f"用户名格式不对：{spec}")
        return spec
    if re.fullmatch(r"-?\d{5,}", spec):
        return spec
    raise ForwardError(
        f"看不懂的去向：{spec}\n请用 @用户名、数字 id 或 t.me 链接。")


async def resolve(client: Any, target: str) -> Any:
    """解析去向并确认能往那儿发。失败时抛出能直接看懂的原因。"""
    try:
        entity = await client.get_entity(
            int(target) if target.lstrip("-").isdigit() else target)
    except Exception as e:  # noqa: BLE001
        raise ForwardError(
            f"找不到 {target}。\n"
            f"请确认你的账号已加入它，用户名或 id 没写错。"
            f"（{type(e).__name__}）") from e

    broadcast = bool(getattr(entity, "broadcast", False))
    try:
        perms = await client.get_permissions(entity, "me")
    except Exception as e:  # noqa: BLE001
        log.debug("权限查询失败，交给发送时判断: %s", e)
        return entity

    if perms is None:
        return entity
    if getattr(perms, "is_creator", False):
        return entity
    allowed = (getattr(perms, "post_messages", None) if broadcast
               else getattr(perms, "send_messages", None))
    if allowed is False:
        raise ForwardError(
            f"你的账号在 {target} 没有发布权限。"
            + ("频道需要管理员并勾选 Post Messages。" if broadcast else ""))
    return entity


async def send(client: Any, target: str, from_peer: int,
               message_ids: list[int]) -> int:
    """把中转频道里的若干条消息转到去向，隐藏来源。"""
    if not message_ids:
        raise ForwardError("没有可转发的消息。")
    entity = await resolve(client, target)
    try:
        sent = await client.forward_messages(
            entity, message_ids, from_peer=from_peer, drop_author=True)
    except Exception as e:  # noqa: BLE001
        raise ForwardError(f"转发到 {target} 失败：{type(e).__name__}: {e}") from e
    n = len(sent if isinstance(sent, list) else [sent])
    log.info("已转发 %d 条到 %s（隐藏来源）", n, target)
    return n
