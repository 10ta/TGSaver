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
    force_reupload: bool = False         # nosp：强制重新下载上传（用于剥离剧透遮罩）
    direct_peer: Optional[str] = None   # 内部伪链接：直接按此解析对话，不加 -100

    @property
    def peer_id(self) -> int:
        """私有链接对应的完整 chat id（-100 前缀形式）。"""
        if self.channel_id is None:
            raise ValueError("公开链接没有 peer_id")
        return int(f"-100{self.channel_id}")

    @property
    def is_private(self) -> bool:
        return self.channel_id is not None

    @property
    def is_comment(self) -> bool:
        """指向频道关联讨论群里的某条评论。"""
        return self.comment_id is not None

    def __str__(self) -> str:
        if self.direct_peer:
            head = str(self.direct_peer)
        elif self.username:
            head = f"@{self.username}"
        else:
            head = f"c/{self.channel_id}"
        tail = f"#{self.msg_id}"
        if self.comment_id:
            tail += f"·评论{self.comment_id}"
        if self.force_reupload:
            tail += "·nosp"
        return head + tail


class ParseError(ValueError):
    """链接格式无法识别。"""


INTERNAL_SCHEME = "tgsaver://"

# 链接后面单独跟一个 nosp 就强制重新下载上传。
# 普通内容默认走服务端直转，那条路径零流量零磁盘，但无法剥离剧透遮罩
# —— 遮罩是原消息的属性，转发会原样带过来。想去掉就只能重传。
NOSP_TOKEN_RE = re.compile(r"(?:^|\s)nosp(?:\s|$)", re.IGNORECASE)


def wants_nosp(text: str) -> bool:
    """整条消息里是否单独出现了 nosp。"""
    return bool(text) and bool(NOSP_TOKEN_RE.search(text))


def with_nosp(url: str) -> str:
    """给链接打上 nosp 标记，使其落库后重试仍然有效。"""
    if _has_nosp_query(url):
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}nosp"


# 消息里出现 fw 就把结果转发到指定去向。去向可以省略（用上次的），
# 但不能因此把普通句子里的 "fw" 当成指令，所以只认两种写法：
#   fw + 合法去向     出现在任何位置
#   单独的 fw         在消息末尾，或后面紧跟 nosp（"fw nosp" = 用上次的去向 + 去剧透）
#
# 合法去向：@用户名、不带 @ 的用户名、数字 id、t.me 链接。
# 裸用户名按 Telegram 的规则限定为字母开头、5 到 32 位，这样 nosp 这类
# 4 个字母的关键词不会被误认成去向。
_FW_TARGET = (r"@[A-Za-z0-9_]{4,32}|-?\d{5,}"
              r"|(?:https?://)?t\.me/[A-Za-z0-9_+/]+"
              r"|[A-Za-z][A-Za-z0-9_]{4,31}")
FW_RE = re.compile(
    rf"(?:^|\s)fw(?:\s+({_FW_TARGET})(?=\s|$)|(?=\s+nosp(?:\s|$))|\s*$)",
    re.IGNORECASE,
)
# 用来发现「写了 fw 但后面的去向看不懂」，好提示用户而不是悄悄忽略
_FW_LOOSE = re.compile(r"(?:^|\s)fw\s+(\S+)", re.IGNORECASE)


def parse_forward(text: str) -> tuple[bool, Optional[str], str]:
    """从消息里剥出 fw 指令。

    返回 (是否要转发, 去向或 None, 剥掉指令后的文本)。

    必须在识别链接和抓取目标**之前**调用：`fw t.me/mychannel` 里的
    t.me/mychannel 是去向，不是「抓取该对话最近 1 条」。
    """
    m = FW_RE.search(text or "")
    if not m:
        return False, None, text or ""
    rest = (text[:m.start()] + " " + text[m.end():]).strip()
    return True, m.group(1), rest


def unrecognized_forward(text: str) -> Optional[str]:
    """写了 fw、后面也跟了东西，但那个东西不像去向。返回它，否则 None。

    只在 parse_forward 没认出指令时调用。用于提示用户，
    避免「以为转发了，其实什么都没发生」。
    """
    m = _FW_LOOSE.search(text or "")
    return m.group(1) if m else None


def _has_nosp_query(url: str) -> bool:
    q = url.split("?", 1)[1] if "?" in url else ""
    return any(p.split("=", 1)[0].lower() == "nosp" for p in q.split("&") if p)


def make_internal(peer: str | int, msg_id: int) -> str:
    """生成内部伪链接。

    私聊（含与 bot 的对话）没有 t.me 链接，Telegram 只为公开频道和
    超级群生成。/grab 用这个形式把私聊消息喂进同一套队列，
    下游的传输、投递逻辑完全复用，不需要任何分支。
    """
    return f"{INTERNAL_SCHEME}p/{peer}/{msg_id}"


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
    if low.startswith(INTERNAL_SCHEME):
        return _parse_internal(raw)
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
    nosp = "nosp" in qs
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
        return MsgRef(raw, None, channel_id, msg_id, topic_id, comment_id,
                      single, nosp)

    # --- bot 频道: /b/<botname>/<msg_id> ---
    if head == "b":
        if len(parts) < 3:
            raise ParseError("bot 链接缺少消息 id")
        topic_id, msg_id = _tail(parts[2:], thread_q)
        return MsgRef(raw, parts[1], None, msg_id, topic_id, comment_id,
                      single, nosp)

    if head in _RESERVED:
        raise ParseError(f"/{head}/ 不是消息链接")

    # --- 公开: /<username>/[topic/]<msg_id> ---
    if len(parts) < 2:
        raise ParseError("链接里没有消息 id，只有频道名")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", parts[0]):
        raise ParseError("用户名格式不合法")
    topic_id, msg_id = _tail(parts[1:], thread_q)
    return MsgRef(raw, parts[0], None, msg_id, topic_id, comment_id,
                  single, nosp)


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


def _parse_internal(raw: str) -> MsgRef:
    """tgsaver://p/<peer>/<msg_id>

    peer 可以是 @username、纯用户名、或数字 id（含负号）。
    与 t.me/c/ 不同，这里的数字**不加** -100 前缀——私聊和 bot 对话
    的 peer 是用户 id，加前缀会指向一个不存在的频道。
    """
    body = raw[len(INTERNAL_SCHEME):]
    nosp = _has_nosp_query(body)
    body = body.split("?", 1)[0]
    parts = [s for s in body.split("/") if s]
    if len(parts) != 3 or parts[0] != "p":
        raise ParseError("内部链接格式应为 tgsaver://p/<peer>/<msg_id>")
    peer = parts[1].lstrip("@")
    if not peer:
        raise ParseError("内部链接缺少对话标识")
    try:
        msg_id = int(parts[2])
    except ValueError as e:
        raise ParseError("内部链接的消息 id 不是数字") from e
    return MsgRef(raw, msg_id=msg_id, force_reupload=nosp, direct_peer=peer)


def _parse_tg_scheme(raw: str) -> MsgRef:
    p = urlparse(raw)
    qs = parse_qs(p.query, keep_blank_values=True)
    host = (p.netloc or p.path.lstrip("/")).lower()
    single = "single" in qs
    nosp = "nosp" in qs
    comment_id = _qs_int(qs, "comment")
    topic_id = _qs_int(qs, "thread") or _qs_int(qs, "topic")
    post = _qs_int(qs, "post")
    if post is None:
        raise ParseError("tg:// 链接缺少 post 参数")

    if host == "privatepost":
        ch = _qs_int(qs, "channel")
        if ch is None:
            raise ParseError("tg://privatepost 缺少 channel 参数")
        return MsgRef(raw, None, ch, post, topic_id, comment_id, single, nosp)

    if host == "resolve":
        dom = qs.get("domain", [None])[0]
        if not dom:
            raise ParseError("tg://resolve 缺少 domain 参数")
        return MsgRef(raw, dom, None, post, topic_id, comment_id, single, nosp)

    raise ParseError("无法识别的 tg:// 链接")
