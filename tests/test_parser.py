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
