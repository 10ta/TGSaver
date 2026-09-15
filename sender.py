"""bot 侧投递：把中转频道里的消息 copy 给用户。

copyMessage / copyMessages 是服务端引用操作，不算 bot 上传，
所以完全不受 Bot API 那个 50MB 上传上限的约束。
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import ReplyParameters

log = logging.getLogger("send")


class DeliverError(RuntimeError):
    pass


async def deliver(
    bot: Bot,
    relay_channel: int,
    relay_ids: list[int],
    to_chat: int,
    reply_to: int | None,
    is_album: bool,
) -> int:
    """把中转频道里的若干条消息复制给用户，返回复制成功的条数。"""
    if not relay_ids:
        raise DeliverError("中转频道里没有拿到消息")

    # 相册用 copyMessages（复数）才能保住分组。
    # 注意该接口不支持 reply_parameters，这是 Bot API 的限制。
    if is_album and len(relay_ids) > 1:
        try:
            res = await bot.copy_messages(
                chat_id=to_chat,
                from_chat_id=relay_channel,
                message_ids=relay_ids,
            )
            return len(res)
        except TelegramRetryAfter:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("相册整组复制失败，退化为逐条: %s", e)

    n = 0
    for i, mid in enumerate(relay_ids):
        rp = ReplyParameters(message_id=reply_to) if (reply_to and i == 0) else None
        await bot.copy_message(
            chat_id=to_chat,
            from_chat_id=relay_channel,
            message_id=mid,
            reply_parameters=rp,
        )
        n += 1
    return n
