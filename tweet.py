"""X / Twitter 帖子支持。

链接 -> FxTwitter API 取数据 -> 组装成:

    用户昵称 #用户ID :
    ┃ 帖子正文（引用块）
    原文链接                ← 指向 x.com 原帖的超链接

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
LINK_TEXT = "原帖链接"

# 呈现方式：
#   auto     先问 Telegram 预览里有没有媒体：齐全就发预览，缺了就发原始媒体（默认）
#   preview  强制预览：一条文字消息 + fxtwitter 大图预览。最快、零流量、无大小限制，
#            但多图是 fxtwitter 合成的拼图，长视频可能根本不出现
#   media    强制发送原始媒体（URL 直发，超限回退中转）。可单独保存每张图
TWEET_MODE = os.getenv("TWEET_MODE", "auto").strip().lower()

# auto 模式下等 Telegram 生成预览的最长秒数。多图时 fxtwitter 要现场拼图，
# 可能要十几秒；等不到就当预览失败，改发原始媒体。
PREVIEW_TIMEOUT = float(os.getenv("TWEET_PREVIEW_TIMEOUT", "20"))
FX_HOST = os.getenv("FXTWITTER_HOST", "fxtwitter.com").strip()

# 末尾 "via 署名" 那一行，比如你自己频道的链接。两项都留空则不显示；
# 只填文字不填链接则显示为纯文字。写在 .env 里而不是代码里，
# 这样公开仓库被别人 clone 时不会带上你的频道。
SIGN_TEXT = os.getenv("TWEET_SIGNATURE_TEXT", "").strip()
SIGN_URL = os.getenv("TWEET_SIGNATURE_URL", "").strip()

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
        """「原帖链接」指向的原帖，统一用 x.com。"""
        user = self.screen_name or "i"
        return f"https://x.com/{user}/status/{self.id}"

    @property
    def preview_url(self) -> str:
        """交给 Telegram 生成预览的地址。fxtwitter 专门为预览优化过。"""
        user = self.screen_name or "i"
        return f"https://{FX_HOST}/{user}/status/{self.id}"


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
        <blockquote><b>昵称</b> : 正文</blockquote>
        <a href="原帖">原帖链接</a> · #ID

        via <a href="署名链接">署名</a>

    没有正文时引用块里只有「昵称 :」。
    超链接的 URL 不计入 Telegram 的长度限制，只有显示文字算。
    """
    name = tw.name.strip() or tw.screen_name
    tag = id_tag(tw)
    text = tw.text.strip()

    head_plain = f"{name} :" + (" " if text else "")
    last_plain = LINK_TEXT + (f" · {tag}" if tag else "")
    sign_plain = f"\n\nvia {SIGN_TEXT}" if SIGN_TEXT else ""
    overhead = (utf16_len(head_plain) + 1 + utf16_len(last_plain)
                + utf16_len(sign_plain))
    budget = max(limit - overhead, 0)

    truncated = False
    if text and utf16_len(text) > budget:
        truncated = True
        text = _cut_utf16(text, max(budget - 1, 0)).rstrip() + "…"

    quote = f"<b>{html.escape(name, quote=False)}</b> :"
    if text:
        quote += " " + html.escape(text, quote=False)
    parts = [f"<blockquote>{quote}</blockquote>"]

    last = f'<a href="{html.escape(tw.url, quote=True)}">{LINK_TEXT}</a>'
    if tag:
        last += " · " + html.escape(tag, quote=False)
    parts.append(last)
    if SIGN_TEXT:
        sign = html.escape(SIGN_TEXT, quote=False)
        if SIGN_URL:
            sign = f'<a href="{html.escape(SIGN_URL, quote=True)}">{sign}</a>'
        parts += ["", f"via {sign}"]
    return "\n".join(parts), truncated


def plan(tw: Tweet, mode: str | None = None) -> dict:
    """决定呈现形态，返回可以直接落库的描述。

    text        纯文字推文，一条消息，不带预览（预览只会把正文再显示一遍）
    preview     一条消息 + 置顶大图预览
    media       媒体带说明（单个或相册）
    media_long  同上但说明超过 1024，媒体后面另跟一条文字

    mode 不给时按 TWEET_MODE。auto 在这里先按 preview 规划，
    由调度器在发送前检查预览，不行再用 mode="media" 重新规划。
    """
    mode = (mode or TWEET_MODE)
    base = {"kind": "tweet", "url": tw.url, "preview_url": tw.preview_url}

    if not tw.media:
        body, _ = build_html(tw, TEXT_LIMIT)
        return {**base, "mode": "text", "html": body, "caption": False}

    if mode != "media":
        body, _ = build_html(tw, TEXT_LIMIT)
        return {**base, "mode": "preview", "html": body, "caption": False}

    cap, cut = build_html(tw, CAPTION_LIMIT)
    if not cut:
        return {**base, "mode": "media", "html": cap, "caption": True}
    body, _ = build_html(tw, TEXT_LIMIT)
    return {**base, "mode": "media_long", "html": body, "caption": False}


def needs_relay(tw: Tweet, extra: dict | None = None) -> str | None:
    """这条推文能否直接由 bot 发出。能就返回 None，否则返回原因。

    文字和预览两种形态只发一条文字消息，永远不需要中转。
    媒体形态下，已知大小时提前判断，省一次必然失败的请求；
    大小未知就先试，Telegram 拒收后再回退。
    """
    if extra and extra.get("mode") in ("text", "preview"):
        return None
    if len(tw.media) > 1 and any(m.kind == "gif" for m in tw.media):
        return "动图不能放进相册"
    for m in tw.media:
        if m.kind == "photo" and m.size > URL_PHOTO_MAX:
            return "图片超过 5MB"
        if m.kind != "photo" and m.size > URL_FILE_MAX:
            return "视频超过 20MB"
    return None


# ------------------------------------------------------------------ 预览检查

def prejudge_preview(tw: Tweet) -> str | None:
    """不用问 Telegram 就能断定预览装不下的情况，返回原因；否则 None。

    一个链接预览只能挂一个媒体对象。多张图 fxtwitter 会拼成一张图，
    这没问题；但多个视频、或图和视频混排时，预览必然丢东西。
    """
    videos = sum(1 for m in tw.media if m.kind in ("video", "gif"))
    photos = sum(1 for m in tw.media if m.kind == "photo")
    if videos > 1:
        return "多个视频，预览只能显示一个"
    if videos and photos:
        return "图片和视频混排，预览装不下"
    return None


def _is_video_doc(doc: Any) -> bool:
    if doc is None:
        return False
    mime = (getattr(doc, "mime_type", "") or "").lower()
    if mime.startswith("video/"):
        return True
    from telethon.tl.types import DocumentAttributeVideo
    return any(isinstance(a, DocumentAttributeVideo)
               for a in (getattr(doc, "attributes", None) or []))


def judge_preview(result: Any, tw: Tweet) -> str:
    """判断 Telegram 给出的预览是否带齐了媒体。

    返回 ok / pending / missing。

    视频只认 document（Telegram 自己存了一份文件）。embed_url 是内嵌播放器，
    每次播放都实时去源站拉，原帖一删就放不了，不算数。
    """
    from telethon.tl.types import (MessageMediaWebPage, WebPage,
                                   WebPagePending)
    # 新协议层外面包了一层 messages.WebPagePreview，旧的直接是 MessageMedia
    media = getattr(result, "media", result)
    if not isinstance(media, MessageMediaWebPage):
        return "missing"
    wp = media.webpage
    if isinstance(wp, WebPagePending):
        return "pending"
    if not isinstance(wp, WebPage):
        return "missing"

    has_video = _is_video_doc(wp.document)
    has_image = wp.photo is not None or has_video or (
        wp.document is not None
        and (getattr(wp.document, "mime_type", "") or "").startswith("image/"))

    if any(m.kind in ("video", "gif") for m in tw.media):
        return "ok" if has_video else "missing"
    return "ok" if has_image else "missing"


# 预览生成中时的轮询间隔（秒），逐渐拉长。大多数预览几秒内就好，快的依然快；
# 慢的少问几次。20 秒内最多 6 次请求，固定 1.5 秒间隔的话是十几次。
PREVIEW_BACKOFF = (1, 2, 3, 5, 8)

# 预览查询被限流后，到这个时刻之前不再查询，直接按预览发送。
_probe_blocked_until = 0.0


class ProbeUnavailable(RuntimeError):
    """当前不宜查询预览（限流冷却中）。调用方按「检查出错」处理：直接发预览。"""


async def probe_preview(client: Any, tw: Tweet, timeout: float = None,
                        on_pending: Any = None,
                        backoff: tuple = None) -> str:
    """发送之前问 Telegram：这个链接会生成什么样的预览。

    和 Telegram 客户端输入链接时显示预览草稿是同一个接口。附带的好处是
    Telegram 会把结果缓存下来，bot 紧接着发送时直接用缓存。

    返回 ok / missing。一直是 pending 直到超时，也算 missing。

    对 user 账号尽量克制：
      - 轮询间隔按 PREVIEW_BACKOFF 逐渐拉长
      - 单次请求 flood_sleep_threshold=0：被限流时立刻报错，而不是让
        Telethon 默默睡最多 60 秒再重试（那样 20 秒超时就形同虚设）。
        只对这一种请求这么设，上传照旧允许短暂等待——否则大文件
        传到一半碰上几秒限流，整个任务会重下重传
      - 被限流后进入冷却期，期间不再查询
    """
    import asyncio
    import time
    from telethon.errors import FloodWaitError
    from telethon.tl.functions.messages import GetWebPagePreviewRequest

    global _probe_blocked_until
    now = time.monotonic()
    if now < _probe_blocked_until:
        raise ProbeUnavailable(
            f"预览查询限流冷却中，还剩 {int(_probe_blocked_until - now)} 秒")

    timeout = PREVIEW_TIMEOUT if timeout is None else timeout
    steps = list(backoff or PREVIEW_BACKOFF)
    deadline = now + timeout
    notified = False
    i = 0
    while True:
        try:
            res = await client(GetWebPagePreviewRequest(message=tw.preview_url),
                               flood_sleep_threshold=0)
        except FloodWaitError as e:
            _probe_blocked_until = time.monotonic() + e.seconds
            log.warning("预览查询被限流 %s 秒，期间推文直接按预览发送", e.seconds)
            raise

        verdict = judge_preview(res, tw)
        if verdict != "pending":
            return verdict
        if not notified and on_pending is not None:
            notified = True
            r = on_pending()
            if hasattr(r, "__await__"):
                await r
        left = deadline - time.monotonic()
        if left <= 0:
            log.info("预览等待超时（%ss）：%s", timeout, tw.preview_url)
            return "missing"
        await asyncio.sleep(min(steps[min(i, len(steps) - 1)], left))
        i += 1


# ------------------------------------------------------------------ 大小预查

async def fill_sizes(tw: Tweet, timeout: float = 5.0) -> None:
    """对大小未知的媒体发 HEAD 请求，补上 Content-Length。

    有了大小，needs_relay 就能提前判断是否超过 Bot API 的 URL 上限，
    省掉一次「发给 Telegram -> 被拒」。HEAD 打的是推特的 CDN，
    不是 Telegram，对你的账号没有任何影响。

    失败（超时、不给长度）就保持未知，照旧先试后回退。
    """
    import asyncio

    todo = [m for m in tw.media if not m.size]
    if not todo:
        return
    http = await _api_session()

    async def one(m: WebMedia) -> None:
        try:
            async with asyncio.timeout(timeout):
                async with http.head(m.url, allow_redirects=True) as r:
                    if r.status == 200 and r.content_length:
                        m.size = int(r.content_length)
        except Exception as e:  # noqa: BLE001
            log.debug("HEAD 取大小失败 %s: %s", m.url, e)

    await asyncio.gather(*(one(m) for m in todo))
