import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from parser import ParseError, find_links, parse_link  # noqa: E402


def test_public_simple():
    r = parse_link("https://t.me/durov/123")
    assert r.username == "durov"
    assert r.msg_id == 123
    assert r.topic_id is None
    assert not r.is_private


def test_public_forum_topic():
    """三段式公开链接：中间是 topic，不是 msg_id。"""
    r = parse_link("https://t.me/somegroup/45/123")
    assert r.username == "somegroup"
    assert r.topic_id == 45
    assert r.msg_id == 123


def test_private():
    r = parse_link("https://t.me/c/1234567890/123")
    assert r.channel_id == 1234567890
    assert r.peer_id == -1001234567890
    assert r.msg_id == 123
    assert r.is_private


def test_private_forum_topic():
    r = parse_link("https://t.me/c/1234567890/45/123")
    assert r.topic_id == 45
    assert r.msg_id == 123
    assert r.peer_id == -1001234567890


def test_bot_channel():
    r = parse_link("https://t.me/b/mybotname/77")
    assert r.username == "mybotname"
    assert r.msg_id == 77


def test_single_flag():
    assert parse_link("https://t.me/durov/123?single").single
    assert not parse_link("https://t.me/durov/123").single


def test_comment_and_thread():
    r = parse_link("https://t.me/chan/10?comment=55")
    assert r.comment_id == 55
    r2 = parse_link("https://t.me/chan/10?thread=9")
    assert r2.topic_id == 9


def test_no_scheme():
    assert parse_link("t.me/durov/123").msg_id == 123


def test_mirror_hosts():
    assert parse_link("https://telegram.me/durov/1").msg_id == 1
    assert parse_link("https://telegram.dog/durov/2").msg_id == 2


def test_www_prefix():
    assert parse_link("https://www.t.me/durov/5").msg_id == 5


def test_tg_privatepost():
    r = parse_link("tg://privatepost?channel=1234567890&post=42")
    assert r.peer_id == -1001234567890
    assert r.msg_id == 42


def test_tg_resolve():
    r = parse_link("tg://resolve?domain=durov&post=9")
    assert r.username == "durov"
    assert r.msg_id == 9


@pytest.mark.parametrize("bad", [
    "https://example.com/a/1",
    "https://t.me/durov",              # 只有频道名
    "https://t.me/c/1234567890",       # 私有但没 msg id
    "https://t.me/joinchat/AAAA",      # 邀请链接
    "https://t.me/s/durov/1",          # 预览页
    "https://t.me/addstickers/xyz",
    "tg://resolve?domain=durov",       # 缺 post
    "",
])
def test_rejects(bad):
    with pytest.raises(ParseError):
        parse_link(bad)


def test_find_links_multiple():
    text = ("看这条 https://t.me/a/1 还有 https://t.me/c/222/3 "
            "以及 tg://privatepost?channel=999&post=8")
    got = find_links(text)
    assert len(got) == 3


def test_find_links_dedup_and_punct():
    text = "https://t.me/a/1。 https://t.me/a/1"
    got = find_links(text)
    assert got == ["https://t.me/a/1"]


def test_find_links_empty():
    assert find_links("没有链接的一句话") == []
    assert find_links("") == []


# ------------------------------------------------- 内部伪链接 (/grab)

def test_internal_username():
    from parser import make_internal
    r = parse_link(make_internal("some_bot", 4832))
    assert r.direct_peer == "some_bot"
    assert r.msg_id == 4832
    assert not r.is_private
    assert r.channel_id is None


def test_internal_numeric_peer_keeps_sign():
    """私聊 peer 是用户 id，绝不能像 t.me/c/ 那样加 -100 前缀。"""
    from parser import make_internal
    r = parse_link(make_internal(-1001234567890, 7))
    assert r.direct_peer == "-1001234567890"
    r2 = parse_link(make_internal(123456789, 7))
    assert r2.direct_peer == "123456789"
    assert r2.channel_id is None


def test_internal_strips_at():
    assert parse_link("tgsaver://p/@bot/5").direct_peer == "bot"


@pytest.mark.parametrize("bad", [
    "tgsaver://p/onlypeer",
    "tgsaver://p//5",
    "tgsaver://x/bot/5",
    "tgsaver://p/bot/notanumber",
])
def test_internal_rejects(bad):
    with pytest.raises(ParseError):
        parse_link(bad)


def test_internal_not_picked_up_by_find_links():
    """内部链接是程序自己生成的，不该从用户文本里被误抓。"""
    assert find_links("tgsaver://p/bot/5") == []


# ------------------------------------------------- 评论链接

def test_comment_link_fields():
    """两个数字属于两套编号：181 是频道帖子，4832 是讨论群里的评论。"""
    r = parse_link("https://t.me/FCbzmg/181?single&comment=4832")
    assert r.username == "FCbzmg"
    assert r.msg_id == 181
    assert r.comment_id == 4832
    assert r.is_comment
    assert r.single


def test_plain_link_is_not_comment():
    assert not parse_link("https://t.me/FCbzmg/181").is_comment


def test_comment_repr_shows_both():
    r = parse_link("https://t.me/chan/181?comment=4832")
    assert "181" in str(r) and "4832" in str(r)


# ------------------------------------------------- nosp 标记

def test_nosp_query():
    from parser import parse_link as pl
    assert pl("https://t.me/durov/1?nosp").force_reupload
    assert not pl("https://t.me/durov/1").force_reupload


def test_nosp_combines_with_single():
    r = parse_link("https://t.me/durov/1?single&nosp")
    assert r.force_reupload and r.single


def test_nosp_on_private_and_comment():
    assert parse_link("https://t.me/c/1234567890/5?nosp").force_reupload
    r = parse_link("https://t.me/chan/181?comment=4832&nosp")
    assert r.force_reupload and r.comment_id == 4832


def test_nosp_on_tg_scheme():
    assert parse_link("tg://resolve?domain=durov&post=9&nosp").force_reupload


def test_nosp_on_internal_link():
    assert parse_link("tgsaver://p/bot/5?nosp").force_reupload
    assert not parse_link("tgsaver://p/bot/5").force_reupload


def test_with_nosp_builds_correct_query():
    from parser import with_nosp
    assert with_nosp("https://t.me/durov/1") == "https://t.me/durov/1?nosp"
    assert with_nosp("https://t.me/durov/1?single") == \
        "https://t.me/durov/1?single&nosp"


def test_with_nosp_is_idempotent():
    from parser import with_nosp
    once = with_nosp("https://t.me/durov/1")
    assert with_nosp(once) == once


@pytest.mark.parametrize("text,want", [
    ("https://t.me/durov/1 nosp", True),
    ("nosp https://t.me/durov/1", True),
    ("https://t.me/durov/1\nnosp", True),
    ("https://t.me/durov/1", False),
    ("nospam 这个词不算", False),
    ("nosping", False),
    ("anosp", False),
])
def test_wants_nosp(text, want):
    from parser import wants_nosp
    assert wants_nosp(text) is want


def test_nosp_survives_roundtrip():
    """标记必须写进链接本身，否则任务落库重试后就丢了。"""
    from parser import with_nosp
    link = with_nosp("https://t.me/durov/1")
    assert parse_link(link).force_reupload, "重新解析后标记应仍在"
