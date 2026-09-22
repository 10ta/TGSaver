"""X / Twitter 帖子支持。

链接 -> FxTwitter API 取数据 -> 组装成:

    用户昵称:
    ┃ 帖子正文（引用块）
    原文链接 #用户ID        ← "原文链接" 是指向 x.com 原帖的超链接

媒体优先交给 Telegram 服务器按 URL 自己去拉（和 Telegram 渲染
fxtwitter 链接预览是同一个机制，毫秒级、本机零流量）；超出 Bot API
的 URL 大小限制时，回退到服务器中转。

数据来源是 FxEmbed 项目提供的公开 API（https://github.com/FxEmbed/FxEmbed），
v2 接口只需要帖子 id。自建了 FxEmbed 实例的话，改 FXTWITTER_API 即可。
"""
from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from streamer import WebMedia

log = logging.getLogger("tweet")

FXTWITTER_API = os.getenv("FXTWITTER_API", "https://api.fxtwitter.com").rstrip("/")
LINK_TEXT = "原文链接"

# Bot API 按 URL 发送时 Telegram 服务器愿意去拉的上限。
# 超过就只能由本机下载再上传。
URL_PHOTO_MAX = 5 * 1024 * 1024
URL_FILE_MAX = 20 * 1024 * 1024

# 相册之外的单条媒体说明上限（非 Premium）与纯文字消息上限。
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


class TweetError(RuntimeError):
    """可以直接展示给用户的失败原因。"""


# ------------------------------------------------------------------ 链接识别

# 认这些域名：x.com、twitter.com，以及各家修复嵌入的镜像
# （fxtwitter / vxtwitter / fixupx / fixvx，含 d. m. 这类子域名前缀）。
# 路径：/<用户>/status/<id>，或 /i/status/<id>、/i/web/status/<id>。
# 前面的否定回顾防止把 "abcx.com" 里的 "x.com" 误认出来。
TWEET_RE = re.compile(
    r"(?<![\w.-])(?:https?://)?(?:[a-z0-9-]{1,20}\.)?"
    r"(?:x|twitter|fxtwitter|vxtwitter|fixupx|fixvx)\.com/"
    r"(?:i/web|(?P<user>[A-Za-z0-9_]{1,15}))/status(?:es)?/(?P<id>\d{2,20})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TweetRef:
    user: str       # 可能是 "i"（/i/status/ 形式），此时以 API 返回的为准
    id: str

    @property
    def canonical(self) -> str:
        return f"https://x.com/{self.user}/status/{self.id}"


def find_links(text: str) -> list[str]:
    """从任意文本里找出所有推文链接，规范化成 x.com 形式，按 id 去重。"""
    out, seen = [], set()
    for m in TWEET_RE.finditer(text or ""):
        tid = m.group("id")
        if tid in seen:
            continue
        seen.add(tid)
        out.append(TweetRef(m.group("user") or "i", tid).canonical)
    return out


def parse(link: str) -> Optional[TweetRef]:
    m = TWEET_RE.search(link or "")
    if not m:
        return None
    return TweetRef(m.group("user") or "i", m.group("id"))


def is_tweet(link: str) -> bool:
    return parse(link) is not None


# ------------------------------------------------------------------ 数据模型

@dataclass
class Tweet:
    id: str
    name: str               # 昵称
    screen_name: str        # 用户 ID（@ 后面那个）
    text: str
    media: list[WebMedia] = field(default_factory=list)

    @property
    def url(self) -> str:
        """按钮指向的原帖，统一用 x.com。"""
        user = self.screen_name or "i"
        return f"https://x.com/{user}/status/{self.id}"


_TOMBSTONE = {
    "deleted": "这条帖子已被删除。",
    "suspended": "发帖账号已被冻结。",
    "private": "这是受保护账号的帖子，无法读取。",
    "blocked": "帖子不可见（作者屏蔽）。",
    "unavailable": "帖子暂时不可用。",
}


def parse_payload(data: Any) -> Tweet:
    """把 FxTwitter v2 的 JSON 变成 Tweet。纯函数，不碰网络。"""
    if not isinstance(data, dict):
        raise TweetError("FxTwitter 返回了无法识别的数据。")

    code = data.get("code")
    st = data.get("status")

    if code == 404 or (code != 200 and not st):
        raise TweetError("帖子不存在，或已被删除。")
    if code == 401:
        raise TweetError("这是受保护账号的帖子，无法读取。")
    if isinstance(st, dict) and st.get("type") == "tombstone":
        raise TweetError(_TOMBSTONE.get(st.get("reason"), "帖子不可用。"))
    if code != 200 or not isinstance(st, dict):
        raise TweetError(f"FxTwitter 返回错误 {code}：{data.get('message', '')}".rstrip("："))

    author = st.get("author") or {}
    screen = str(author.get("screen_name") or "")
    name = str(author.get("name") or screen)

    return Tweet(
        id=str(st.get("id") or ""),
        name=name,
        screen_name=screen,
        text=str(st.get("text") or ""),
        media=_parse_media(st.get("media") or {}),
    )


def _parse_media(media: dict) -> list[WebMedia]:
    """按帖子里的原始顺序取媒体。

    优先用 media.all（官方给的就是原顺序的混合列表），缺失时退回
    photos + videos。拼图（mosaic）、直播（broadcast）、外链视频跳过。
    """
    items = media.get("all")
    if not isinstance(items, list):
        items = list(media.get("photos") or []) + list(media.get("videos") or [])

    out: list[WebMedia] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        kind = it.get("type")
        url = it.get("url")
        if not url or kind not in ("photo", "video", "gif"):
            continue
        is_video_like = "duration" in it or kind == "video"
        if kind == "gif" and not is_video_like and _looks_like_image(url):
            kind = "photo"  # 极少数 gif 以静态图形式给出
        if kind == "photo":
            out.append(WebMedia("photo", _best_photo(url),
                                int(it.get("width") or 0), int(it.get("height") or 0)))
        else:
            url, size = _best_video(it)
            out.append(WebMedia(
                kind, url,
                int(it.get("width") or 0), int(it.get("height") or 0),
                float(it.get("duration") or 0), it.get("thumbnail_url"), size))
    return out


def _looks_like_image(url: str) -> bool:
    return bool(re.search(r"\.(jpe?g|png|webp)(\?|$)", url, re.IGNORECASE))


def _best_photo(url: str) -> str:
    """推特图床取 4096x4096 规格。

    不用 orig：原图偶尔超过 Telegram 照片的 10MB 或宽高之和 10000 的
    限制，会被当成无效图片拒收。4096 是推特提供的最大缩放档，
    对绝大多数图片就是原图。
    """
    if "pbs.twimg.com/media/" not in url:
        return url
    base = url.split("?", 1)[0]
    m = re.search(r"\.(jpe?g|png|webp)$", base, re.IGNORECASE)
    if m:
        fmt = m.group(1).lower().replace("jpeg", "jpg")
        base = base[: m.start()]
        return f"{base}?format={fmt}&name=4096x4096"
    return f"{base}?name=4096x4096"


def _best_video(it: dict) -> tuple[str, int]:
    """在多个码率里挑 h264 的最高码率 mp4，返回 (url, 已知大小或 0)。

    hevc / av1 部分 Telegram 客户端播不了，所以优先 h264；
    m3u8 是分片流，没法当文件传，排除。
    """
    fmts = [f for f in (it.get("formats") or [])
            if isinstance(f, dict) and f.get("url") and f.get("container") == "mp4"]
    h264 = [f for f in fmts if f.get("codec") in (None, "h264")]
    pool = h264 or fmts
    if pool:
        best = max(pool, key=lambda f: f.get("bitrate") or 0)
        return best["url"], int(best.get("size") or 0)
    return it["url"], int(it.get("filesize") or 0)


# ------------------------------------------------------------------ 抓取

_session = None


async def _api_session():
    """复用同一个连接。每次新建会话都要重做 TLS 握手，白白多上百毫秒。"""
    global _session
    if _session is None or _session.closed:
        from streamer import http_session
        _session = http_session()
    return _session


async def close() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


async def fetch(ref: TweetRef) -> Tweet:
    url = f"{FXTWITTER_API}/2/status/{ref.id}"
    try:
        http = await _api_session()
        async with http.get(url) as r:
            try:
                data = await r.json(content_type=None)
            except Exception as e:  # noqa: BLE001
                raise TweetError(
                    f"FxTwitter 返回了无法解析的内容（HTTP {r.status}）。") from e
    except TweetError:
        raise
    except Exception as e:  # noqa: BLE001
        raise TweetError(f"连接 FxTwitter 失败：{type(e).__name__}") from e
    return parse_payload(data)


# ------------------------------------------------------------------ 组装

def utf16_len(s: str) -> int:
    """Telegram 的长度限制按 UTF-16 码元计，emoji 等算 2 个。"""
    return len(s.encode("utf-16-le")) // 2


def _cut_utf16(s: str, limit: int) -> str:
    out, n = [], 0
    for ch in s:
        w = 2 if ord(ch) > 0xFFFF else 1
        if n + w > limit:
            break
        out.append(ch)
        n += w
    return "".join(out)


def to_hashtag(s: str) -> str:
    """把任意昵称变成 Telegram 认得的 hashtag。

    hashtag 只能由字母（含中日韩文字）、数字、下划线组成。昵称里的
    空格、emoji、标点都会截断 hashtag，所以统一替换成下划线再收拢。
    """
    t = re.sub(r"[^\w]+", "_", s or "", flags=re.UNICODE)
    t = re.sub(r"_+", "_", t).strip("_")
    return f"#{t}" if t else ""


def id_tag(tw: Tweet) -> str:
    return to_hashtag(tw.screen_name)


def build_html(tw: Tweet, limit: int) -> tuple[str, bool]:
    """组装消息 HTML。超长时截断正文，返回 (html, 是否截断)。

    格式：
        昵称:
        <blockquote>正文</blockquote>
        <a href="原帖">原文链接</a> #ID

    超链接的 URL 不计入 Telegram 的长度限制，只有显示文字算。
    """
    name = tw.name.strip() or tw.screen_name
    tag = id_tag(tw)
    text = tw.text.strip()

    last_plain = LINK_TEXT + (f" {tag}" if tag else "")
    overhead = utf16_len(name) + 1 + 1 + utf16_len(last_plain)   # 冒号 + 末行前换行
    if text:
        overhead += 1                                            # 正文前换行
    budget = max(limit - overhead, 0)

    truncated = False
    if text and utf16_len(text) > budget:
        truncated = True
        text = _cut_utf16(text, max(budget - 1, 0)).rstrip() + "…"

    link = f'<a href="{html.escape(tw.url, quote=True)}">{LINK_TEXT}</a>'
    last = link + (f" {html.escape(tag, quote=False)}" if tag else "")

    parts = [html.escape(name, quote=False) + ":"]
    if text:
        parts.append(f"<blockquote>{html.escape(text, quote=False)}</blockquote>")
    parts.append(last)
    return "\n".join(parts), truncated


def plan(tw: Tweet) -> dict:
    """决定呈现形态，返回可以直接落库的描述。

    text        纯文字，一条消息
    media       有媒体，说明挂在第一项上（单个媒体或相册都一样）
    media_long  说明超过 1024，媒体不带说明，后面另跟一条文字消息
    """
    if not tw.media:
        body, _ = build_html(tw, TEXT_LIMIT)
        return {"kind": "tweet", "mode": "text", "html": body, "url": tw.url,
                "caption": False}

    cap, cut = build_html(tw, CAPTION_LIMIT)
    if not cut:
        return {"kind": "tweet", "mode": "media", "html": cap, "url": tw.url,
                "caption": True}
    body, _ = build_html(tw, TEXT_LIMIT)
    return {"kind": "tweet", "mode": "media_long", "html": body, "url": tw.url,
            "caption": False}


def needs_relay(tw: Tweet) -> str | None:
    """这条推文能否直接按 URL 交给 Telegram。能就返回 None，否则返回原因。

    已知大小时提前判断，省一次必然失败的请求；大小未知就先试，
    Telegram 拒收后再回退。
    """
    if len(tw.media) > 1 and any(m.kind == "gif" for m in tw.media):
        return "动图不能放进相册"
    for m in tw.media:
        if m.kind == "photo" and m.size > URL_PHOTO_MAX:
            return "图片超过 5MB"
        if m.kind != "photo" and m.size > URL_FILE_MAX:
            return "视频超过 20MB"
    return None
