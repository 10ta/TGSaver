"""bot 侧投递：把中转频道里的消息 copy 给用户。

copyMessage / copyMessages 是服务端引用操作，不算 bot 上传，
所以完全不受 Bot API 那个 50MB 上传上限的约束。
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
    ReplyParameters,
)

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


class UrlRejected(RuntimeError):
    """Telegram 服务器拒绝按 URL 拉取媒体（太大、拉不到等）。

    只在「还没有任何东西发给用户」的阶段抛出，调用方可以放心回退到
    服务器中转，不会出现用户收到两份的情况。
    """


def _rp(reply_to):
    return (ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
            if reply_to else None)


_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def _send_text(bot: Bot, to_chat: int, html_text: str, reply_to=None) -> None:
    # 关掉链接预览：正文里的外链、以及「原文链接」本身都会生成预览卡片，
    # 把消息撑得很长
    await bot.send_message(to_chat, html_text, parse_mode="HTML",
                           reply_parameters=_rp(reply_to),
                           link_preview_options=_NO_PREVIEW)


async def _send_preview(bot: Bot, to_chat: int, extra: dict, reply_to=None) -> None:
    """一条文字消息 + fxtwitter 链接预览，大图显示在正文上方。

    预览地址不必出现在正文里：「原帖链接」指向 x.com，
    预览单独用 fxtwitter，两者互不影响。
    """
    await bot.send_message(
        to_chat, extra["html"], parse_mode="HTML",
        reply_parameters=_rp(reply_to),
        # is_disabled 必须显式给 False：不给的话 aiogram 会去取 bot 全局默认值，
        # 哪天全局默认改成禁用预览，这里就会悄悄失效
        link_preview_options=LinkPreviewOptions(
            is_disabled=False, url=extra["preview_url"],
            prefer_large_media=True, show_above_text=True),
    )


async def send_tweet_direct(bot: Bot, tw, extra: dict, to_chat: int,
                            reply_to: int | None) -> int:
    """快速路径：把媒体 URL 直接交给 Telegram，由它的服务器去拉。

    和 Telegram 渲染 fxtwitter 链接预览是同一个机制——媒体不经过本机，
    毫秒级完成。Bot API 对 URL 有大小限制（照片 5MB、其他 20MB），
    超了会报 BadRequest，这里转成 UrlRejected 让调用方回退。
    """
    mode = extra["mode"]
    if mode == "text":
        await _send_text(bot, to_chat, extra["html"], reply_to)
        return 1
    if mode == "preview":
        await _send_preview(bot, to_chat, extra, reply_to)
        return 1

    cap = extra["html"] if extra.get("caption") else None
    kw = {"caption": cap, "parse_mode": "HTML"} if cap else {}
    rp = _rp(reply_to)
    media = tw.media

    try:
        if len(media) == 1:
            m = media[0]
            if m.kind == "photo":
                await bot.send_photo(to_chat, m.url, reply_parameters=rp, **kw)
            elif m.kind == "gif":
                await bot.send_animation(
                    to_chat, m.url, width=m.width or None, height=m.height or None,
                    duration=int(m.duration) or None, reply_parameters=rp, **kw)
            else:
                await bot.send_video(
                    to_chat, m.url, width=m.width or None, height=m.height or None,
                    duration=int(m.duration) or None, supports_streaming=True,
                    reply_parameters=rp, **kw)
        else:
            group = []
            for i, m in enumerate(media):
                ikw = kw if i == 0 else {}
                if m.kind == "photo":
                    group.append(InputMediaPhoto(media=m.url, **ikw))
                else:
                    group.append(InputMediaVideo(
                        media=m.url, width=m.width or None, height=m.height or None,
                        duration=int(m.duration) or None, supports_streaming=True,
                        **ikw))
            await bot.send_media_group(to_chat, group, reply_parameters=rp)
    except TelegramBadRequest as e:
        raise UrlRejected(str(e)) from e

    if mode == "media_long":
        # 媒体已经送达，这之后的失败不能再回退，否则用户会收到两份
        await _send_text(bot, to_chat, extra["html"])
        return len(media) + 1
    return len(media)


async def deliver_tweet(
    bot: Bot,
    relay_channel: int | None,
    relay_ids: list[int] | None,
    extra: dict,
    to_chat: int,
    reply_to: int | None,
) -> int:
    """慢速路径的投递：媒体已经由本机搬进中转频道，从那里复制过来。

    说明文字在中转时就挂在第一项上了，copy 会原样带过来。
    """
    mode = extra.get("mode")
    if mode == "text":
        await _send_text(bot, to_chat, extra["html"], reply_to)
        return 1
    if mode == "preview":
        await _send_preview(bot, to_chat, extra, reply_to)
        return 1

    if not relay_ids:
        raise DeliverError("中转频道里没有拿到媒体")

    album = len(relay_ids) > 1 and not extra.get("split")
    n = await deliver(bot, relay_channel, relay_ids, to_chat, reply_to,
                      is_album=album)
    if mode == "media_long":
        await _send_text(bot, to_chat, extra["html"])
        n += 1
    return n
