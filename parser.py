"""Telegram 消息链接解析。

纯函数，无副作用，无网络。全部行为由 tests/test_parser.py 覆盖。

支持形态::

    https://t.me/name/123              公开频道/群
    https://t.me/name/45/123           公开论坛群（45 = topic id）
    https://t.me/c/1234567890/123      私有
    https://t.me/c/1234567890/45/123   私有论坛群
    https://t.me/b/botname/123         bot 频道
    tg://privatepost?channel=..&post=..
    tg://resolve?domain=..&post=..
    ?single ?thread=N ?comment=N       查询参数

关键陷阱：三段式链接中间那个数字是 topic id，不是 message id。
真正的 message id 永远是路径最后一段。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

# 在自由文本里抓出所有候选链接
LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?t\.me/[^\s<>\"']+"
    r"|tg://(?:privatepost|resolve)\?[^\s<>\"']+",
    re.IGNORECASE,
)

# t.me 上不是用户名的保留路径
_RESERVED = {"c", "b", "s", "joinchat", "addstickers", "proxy", "socks",
             "share", "iv", "login", "addtheme", "addemoji", "boost"}


@dataclass(frozen=True)
class MsgRef:
    """一条消息的定位信息。"""

    raw: str
    username: Optional[str] = None      # 公开链接的用户名
    channel_id: Optional[int] = None    # 私有链接的原始 id（未加 -100）
    msg_id: int = 0
    topic_id: Optional[int] = None      # 论坛群的话题 id
    comment_id: Optional[int] = None    # ?comment= 讨论区楼层
    single: bool = False                # ?single 表示只要这一条，不要整个相册

    @property
    def peer_id(self) -> int:
        """私有链接对应的完整 chat id（-100 前缀形式）。"""
        if self.channel_id is None:
            raise ValueError("公开链接没有 peer_id")
        return int(f"-100{self.channel_id}")

    @property
    def is_private(self) -> bool:
        return self.channel_id is not None

    def __str__(self) -> str:
        head = f"@{self.username}" if self.username else f"c/{self.channel_id}"
        return f"{head}#{self.msg_id}"


class ParseError(ValueError):
    """链接格式无法识别。"""


def find_links(text: str) -> list[str]:
    """从任意文本中抓出全部候选链接，按出现顺序，去重。"""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in LINK_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?)]}\u3002\uff0c")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _qs_int(qs: dict[str, list[str]], key: str) -> Optional[int]:
    v = qs.get(key, [None])[0]
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def parse_link(url: str) -> MsgRef:
    """把一条链接解析成 MsgRef，失败抛 ParseError。"""
    raw = url.strip()
    if not raw:
        raise ParseError("空链接")

    low = raw.lower()
    if low.startswith("tg://"):
        return _parse_tg_scheme(raw)

    if not low.startswith(("http://", "https://")):
        raw_url = "https://" + raw
    else:
        raw_url = raw

    p = urlparse(raw_url)
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in ("t.me", "telegram.me", "telegram.dog"):
        raise ParseError("不是 Telegram 消息链接")

    parts = [s for s in p.path.split("/") if s]
    qs = parse_qs(p.query, keep_blank_values=True)
    single = "single" in qs
    comment_id = _qs_int(qs, "comment")
    thread_q = _qs_int(qs, "thread")

    if not parts:
        raise ParseError("链接里没有消息位置")

    head = parts[0].lower()

    # --- 私有: /c/<channel_id>/[topic/]<msg_id> ---
    if head == "c":
        if len(parts) < 3:
            raise ParseError("私有链接缺少消息 id")
        try:
            channel_id = int(parts[1])
        except ValueError as e:
            raise ParseError("私有链接的 channel id 不是数字") from e
        topic_id, msg_id = _tail(parts[2:], thread_q)
        return MsgRef(raw, None, channel_id, msg_id, topic_id, comment_id, single)

    # --- bot 频道: /b/<botname>/<msg_id> ---
    if head == "b":
        if len(parts) < 3:
            raise ParseError("bot 链接缺少消息 id")
        topic_id, msg_id = _tail(parts[2:], thread_q)
        return MsgRef(raw, parts[1], None, msg_id, topic_id, comment_id, single)

    if head in _RESERVED:
        raise ParseError(f"/{head}/ 不是消息链接")

    # --- 公开: /<username>/[topic/]<msg_id> ---
    if len(parts) < 2:
        raise ParseError("链接里没有消息 id，只有频道名")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", parts[0]):
        raise ParseError("用户名格式不合法")
    topic_id, msg_id = _tail(parts[1:], thread_q)
    return MsgRef(raw, parts[0], None, msg_id, topic_id, comment_id, single)


def _tail(seg: list[str], thread_q: Optional[int]) -> tuple[Optional[int], int]:
    """解析路径尾部，返回 (topic_id, msg_id)。

    一段 -> 就是 msg_id；两段 -> 前者是 topic_id。
    """
    nums: list[int] = []
    for s in seg[:2]:
        try:
            nums.append(int(s))
        except ValueError as e:
            raise ParseError(f"路径段 {s!r} 不是数字") from e
    if not nums:
        raise ParseError("缺少消息 id")
    if len(nums) == 1:
        return thread_q, nums[0]
    return nums[0], nums[1]


def _parse_tg_scheme(raw: str) -> MsgRef:
    p = urlparse(raw)
    qs = parse_qs(p.query, keep_blank_values=True)
    host = (p.netloc or p.path.lstrip("/")).lower()
    single = "single" in qs
    comment_id = _qs_int(qs, "comment")
    topic_id = _qs_int(qs, "thread") or _qs_int(qs, "topic")
    post = _qs_int(qs, "post")
    if post is None:
        raise ParseError("tg:// 链接缺少 post 参数")

    if host == "privatepost":
        ch = _qs_int(qs, "channel")
        if ch is None:
            raise ParseError("tg://privatepost 缺少 channel 参数")
        return MsgRef(raw, None, ch, post, topic_id, comment_id, single)

    if host == "resolve":
        dom = qs.get("domain", [None])[0]
        if not dom:
            raise ParseError("tg://resolve 缺少 domain 参数")
        return MsgRef(raw, dom, None, post, topic_id, comment_id, single)

    raise ParseError("无法识别的 tg:// 链接")
