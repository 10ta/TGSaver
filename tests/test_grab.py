"""裸文本抓取目标的识别。

这个解析器直接挂在「任意文本」上，所以最大的风险不是漏判而是误判：
把普通聊天内容当成抓取指令，会让 bot 去解析一个不存在的对话，
然后回一句莫名其妙的错误。下面的用例里反例比正例多，就是这个原因。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "h")
os.environ.setdefault("BOT_TOKEN", "t")
os.environ.setdefault("OWNER_ID", "42")
os.environ.setdefault("RELAY_CHANNEL_ID", "-1001111111111")
os.environ.setdefault("SECRET_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402

from bot import GRAB_MAX, parse_grab_target as P  # noqa: E402


# ------------------------------------------------- 应该识别

@pytest.mark.parametrize("text,want", [
    # 链接形式（主推：不会触发 Telegram 的 inline 查询拦截）
    ("t.me/some_bot",              ("some_bot", 1)),
    ("t.me/some_bot 5",            ("some_bot", 5)),
    ("https://t.me/some_bot 3",    ("some_bot", 3)),
    ("http://t.me/some_bot",       ("some_bot", 1)),
    ("https://www.t.me/some_bot",  ("some_bot", 1)),
    ("t.me/some_bot/ 2",           ("some_bot", 2)),
    ("  t.me/some_bot 5  ",        ("some_bot", 5)),
    # 私有频道：t.me/c/ 里的数字要补回 -100 前缀
    ("t.me/c/1234567890 2",        ("-1001234567890", 2)),
    ("t.me/c/1234567890",          ("-1001234567890", 1)),
    # 点号简写
    (".some_bot",                  ("some_bot", 1)),
    (".some_bot 5",                ("some_bot", 5)),
    (">some_bot 5",                ("some_bot", 5)),
    (".@some_bot 5",               ("some_bot", 5)),
    # @ 形式仍保留（bot 不支持 inline 时可用）
    ("@some_bot",                  ("some_bot", 1)),
    ("@some_bot 5",                ("some_bot", 5)),
    ("  @some_bot 5  ",            ("some_bot", 5)),
    # 数字 id
    ("123456789",                  ("123456789", 1)),
    ("123456789 3",                ("123456789", 3)),
    ("-1001234567890 2",           ("-1001234567890", 2)),
])
def test_recognized(text, want):
    assert P(text) == want


# --------------------------------- 绝不能吞掉真正的消息链接

@pytest.mark.parametrize("text", [
    "https://t.me/durov/1",
    "t.me/durov/1",
    "t.me/durov/1 nosp",
    "https://t.me/c/1234567890/123",
    "https://t.me/c/1234567890/45/123",
    "https://t.me/chan/181?comment=4832",
    "https://t.me/b/botname/77",
    "https://t.me/joinchat/AAAA",
    "https://t.me/durov/1 https://t.me/durov/2",
])
def test_message_links_not_swallowed(text):
    """带消息 id 的链接必须交给正常流程，不能被当成抓取目标。"""
    assert P(text) is None, f"{text!r} 是消息链接，不该走抓取"


def test_count_clamped_to_max():
    assert P("@bot_name 999")[1] == GRAB_MAX
    assert P("@bot_name 0")[1] == 1


# ------------------------------------------------- 绝不能误判

@pytest.mark.parametrize("text", [
    "hello",                      # 普通单词，长度正好像用户名
    "thanks",
    "在吗",
    "ok 5",
    "test 3",
    "123",                        # 太短，更像随口一个数字
    "2024",
    "12345",                      # 5 位，仍不够长
    "@ab",                        # 用户名太短
    "@some_bot 5 extra",          # 多余内容
    "@some_bot@other",
    "帮我看看 @some_bot",          # 夹在句子里
    "@some_bot 你好",
    ".ab",                        # 点号后用户名太短
    "",
    "   ",
    "/start",
    "-",
    "abc def",
])
def test_not_recognized(text):
    assert P(text) is None, f"{text!r} 不该被当成抓取目标"


def test_bare_word_requires_marker():
    """裸用户名只认以 bot 结尾的：Telegram 规定 bot 用户名必须以 bot 结尾，
    所以 "hello" 这种普通词不会被误判。其他对话仍需 t.me/、@ 或点号。"""
    assert P("some_bot") == ("some_bot", 1)
    assert P("SomeBot 3") == ("SomeBot", 3)
    assert P("some_user") is None
    assert P("hello") is None
    assert P("@some_user") == ("some_user", 1)
    assert P(".some_user") == ("some_user", 1)
    assert P("t.me/some_user") == ("some_user", 1)


def test_numeric_needs_six_digits():
    assert P("12345") is None
    assert P("123456") == ("123456", 1)


# ================================================================ 多对话、多数字、区间

from bot import parse_grab_command as C  # noqa: E402


def _s(specs):
    return [(x.target, x.count, x.positions) for x in specs]


@pytest.mark.parametrize("text,want", [
    # 单个对话 + 单个数字：原有逻辑不变
    ("t.me/ym_ss_bot 2",          [("ym_ss_bot", 2, None)]),
    ("ym_ss_bot 2",               [("ym_ss_bot", 2, None)]),
    ("ym_ss_bot",                 [("ym_ss_bot", 1, None)]),
    # 多个数字、区间：按位置
    ("t.me/a_bot 1 3 5",          [("a_bot", None, [1, 3, 5])]),
    ("t.me/a_bot 1-3 7",          [("a_bot", None, [1, 2, 3, 7])]),
    ("t.me/a_bot 1-3",            [("a_bot", None, [1, 2, 3])]),
    ("t.me/a_bot 3-1",            [("a_bot", None, [1, 2, 3])]),
    ("t.me/a_bot 2 1",            [("a_bot", None, [2, 1])]),       # 按写的顺序
    ("t.me/a_bot 1 1 2",          [("a_bot", None, [1, 2])]),       # 去重
    # 多个对话
    (".a_bot 3 .b_bot 1-2",       [("a_bot", 3, None), ("b_bot", None, [1, 2])]),
    ("t.me/a_bot 1 t.me/c/1234567890 2 5",
                                  [("a_bot", 1, None), ("-1001234567890", None, [2, 5])]),
    # 同一对话出现多次：合并，按位置
    (".ym_ss_bot 1 .ym_ss_bot 2", [("ym_ss_bot", None, [1, 2])]),
    (".x_bot .x_bot",             [("x_bot", None, [1])]),
    (".x_bot 3 .x_bot 5-6",       [("x_bot", None, [3, 5, 6])]),
])
def test_grab_command(text, want):
    assert _s(C(text)) == want


@pytest.mark.parametrize("text", [
    "hello 5", "ok 5", "1 3 5", "a_bot 1 hello", "t.me/a_bot 1-x",
    "t.me/a_bot 1 3 abc", "t.me/durov/1", "some_user 2", "",
])
def test_grab_command_rejects(text):
    """有任何一个词认不出来，整条都不算抓取指令。"""
    assert C(text) is None


def test_positions_capped():
    assert len(C("t.me/a_bot 1-999")[0].positions) == 50


def test_position_zero_dropped():
    assert C("t.me/a_bot 0 2")[0].positions == [2]


def test_numeric_id_not_confused_with_position():
    """6 位以上是对话 id，1~3 位是第几条，不会混。"""
    assert _s(C("123456789 2 3")) == [("123456789", None, [2, 3])]
    assert C("t.me/a_bot 1234")[0].ids == [1234], "4 位以上按消息 id 理解"


def test_do_grab_passes_forward_target():
    """抓取时写的 fw 必须传下去 —— 以前漏了，导致静默不转发。"""
    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text()
    body = src[src.index("async def do_grab"):src.index('@router.message(Command("grab"))')]
    calls = body.count("RUNNER.submit(")
    assert calls >= 1 and body.count("forward_to=fw_to") == calls


# ================================================================ 论坛话题与消息 id

def _p(text):
    r = C(text)
    return None if r is None else [(x.target, x.topic, x.count, x.picks) for x in r]


def test_topic_with_positions():
    assert _p("https://t.me/c/2703619907/124 1-3") == [
        ("-1002703619907", 124, None, [("pos", 1), ("pos", 2), ("pos", 3)])]


def test_topic_with_message_ids():
    assert _p("https://t.me/c/2703619907/124 4632-4634") == [
        ("-1002703619907", 124, None, [("id", 4632), ("id", 4633), ("id", 4634)])]


def test_public_topic():
    assert _p("t.me/somegroup/45 2") == [("somegroup", 45, 2, None)]


def test_bare_topic_link_stays_a_message_link():
    """单独一个话题 / 消息链接不能被吃成抓取指令，要交给普通链接流程。"""
    assert C("https://t.me/c/2703619907/124") is None
    assert C("https://t.me/durov/1") is None


def test_positions_and_ids_mixed_in_written_order():
    assert _p("t.me/c/2703619907/124 2 4635 1") == [
        ("-1002703619907", 124, None, [("pos", 2), ("id", 4635), ("pos", 1)])]


def test_id_range_capped():
    r = C("t.me/c/2703619907/124 1000-9999")[0]
    assert len(r.ids) == 100


def test_positive_long_number_after_target_is_message_id():
    """开头的长数字是对话 id；后面的正数长数字是消息 id；负数仍是对话 id。"""
    assert _p("123456789 2") == [("123456789", None, 2, None)]
    assert _p("t.me/a_bot 1 123456789") == [("a_bot", None, None,
                                             [("pos", 1), ("id", 123456789)])]
    assert [x.target for x in C("t.me/a_bot 1 -1001234567890 2")] == \
        ["a_bot", "-1001234567890"]


def test_same_topic_twice_merges():
    assert _p("t.me/c/2703619907/124 1 t.me/c/2703619907/124 2") == [
        ("-1002703619907", 124, None, [("pos", 1), ("pos", 2)])]


def test_different_topics_same_group_stay_separate():
    r = C("t.me/c/2703619907/124 1 t.me/c/2703619907/125 1")
    assert [(x.target, x.topic) for x in r] == [
        ("-1002703619907", 124), ("-1002703619907", 125)]


# ---------------------------------------------------------------- 取具体消息

from datetime import datetime, timezone  # noqa: E402

from telethon.tl import types as TT  # noqa: E402


def _msg(mid, gid=None, service=False):
    if service:
        return TT.MessageService(id=mid, peer_id=TT.PeerChannel(1), date=None,
                                 action=TT.MessageActionTopicCreate(title="t", icon_color=0))
    return TT.Message(id=mid, peer_id=TT.PeerChannel(1),
                      date=datetime.now(timezone.utc), message="",
                      media=TT.MessageMediaPhoto(photo=TT.Photo(
                          id=mid, access_hash=1, file_reference=b"", date=None,
                          sizes=[], dc_id=1)), grouped_id=gid)


class ScanClient:
    def __init__(self, recent=(), by_id=()):
        self.recent = list(recent)
        self.by_id = {m.id: m for m in by_id}
        self.iter_kw = None

    async def iter_messages(self, entity, limit=None, **kw):
        self.iter_kw = kw
        for m in self.recent:
            yield m

    async def get_messages(self, entity, ids=None):
        return [self.by_id.get(i) for i in ids]


@pytest.mark.asyncio
async def test_scan_within_topic_uses_reply_to():
    import bot
    c = ScanClient(recent=[_msg(9), _msg(8)])
    await bot._scan_items(c, object(), 2, topic=124)
    assert c.iter_kw == {"reply_to": 124}
    await bot._scan_items(c, object(), 2)
    assert c.iter_kw == {}, "没有话题时不能带 reply_to"


@pytest.mark.asyncio
async def test_resolve_picks_ids_dedupe_album():
    """id 区间正好覆盖一个相册时，相册只取一次 —— 否则会被重复搬好几次。"""
    import bot
    album = [_msg(i, gid=777) for i in range(4632, 4636)]
    c = ScanClient(by_id=album + [_msg(4636), _msg(4637)])
    sp = C("t.me/c/2703619907/124 4632-4637")[0]
    picks, missing, _ = await bot._resolve_picks(c, object(), sp)
    assert [m.id for m in picks] == [4632, 4636, 4637]
    assert missing == []


@pytest.mark.asyncio
async def test_resolve_picks_missing_and_service():
    import bot
    c = ScanClient(recent=[_msg(9)], by_id=[_msg(4632), _msg(4633, service=True)])
    sp = C("t.me/c/2703619907/124 1 2 4632 4633 4634")[0]
    picks, missing, _ = await bot._resolve_picks(c, object(), sp)
    assert [m.id for m in picks] == [9, 4632]
    assert missing == ["第 2 条", "消息 4633", "消息 4634"], "系统消息当作取不到"


@pytest.mark.asyncio
async def test_resolve_picks_keeps_written_order():
    import bot
    c = ScanClient(recent=[_msg(9), _msg(8)], by_id=[_msg(4632)])
    sp = C("t.me/c/2703619907/124 2 4632 1")[0]
    picks, _, _ = await bot._resolve_picks(c, object(), sp)
    assert [m.id for m in picks] == [8, 4632, 9]


# ---------------------------------------------------------------- 多个消息链接

def test_batch_links():
    from parser import batch_links
    text = ("https://t.me/c/2703619907/124/4638 https://t.me/c/2703619907/124/4637 "
            "tgsaver://p/111/5")
    assert batch_links(text) == ["https://t.me/c/2703619907/124/4638",
                                 "https://t.me/c/2703619907/124/4637",
                                 "tgsaver://p/111/5"]
    assert batch_links("https://x.com/a/status/12") == []
    assert len(batch_links("https://t.me/c/2703619907/124/4638")) == 1


def test_multiple_message_links_become_one_task():
    """多个消息链接和推文一样拼成一条消息，不再各发各的。"""
    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text()
    body = src[src.index("    valid = []"):]
    body = body[:body.index("\n\n\n")]
    assert body.count("RUNNER.submit(") == 1
    assert '" ".join(valid)' in body



# ---------------------------------------------------------------- 翻找统计

@pytest.mark.asyncio
async def test_scan_counts_messages_visited():
    import bot
    c = ScanClient(recent=[_msg(9), _msg(8, service=True), _msg(7)])
    r = await bot._scan_items(c, object(), 10, topic=124)
    assert r.scanned == 3 and len(r) == 2


@pytest.mark.asyncio
async def test_short_positions_explain_why():
    """话题里 1-10 只找到 2 条：要说清楚翻了多少条、其中多少带媒体。"""
    import bot
    c = ScanClient(recent=[_msg(9), _msg(8)])
    sp = C("t.me/c/2703619907/124 1-10")[0]
    picks, missing, why = await bot._resolve_picks(c, object(), sp)
    assert len(picks) == 2 and len(missing) == 8
    assert "这个话题一共只有 2 条消息" in why


@pytest.mark.asyncio
async def test_scan_note_when_limit_reached(monkeypatch):
    import bot
    monkeypatch.setattr(bot, "GRAB_SCAN", 3)
    c = ScanClient(recent=[_msg(9), _msg(8, service=True), _msg(7, service=True)])
    r = await bot._scan_items(c, object(), 10)
    assert "最近 3 条消息里只有 1 条带媒体" in bot._scan_note(r, None)
    assert "更早的没有翻" in bot._scan_note(r, None)


@pytest.mark.asyncio
async def test_no_scan_note_when_enough():
    import bot
    c = ScanClient(recent=[_msg(9), _msg(8)])
    sp = C("t.me/c/2703619907/124 1 2")[0]
    _, missing, why = await bot._resolve_picks(c, object(), sp)
    assert missing == [] and why == ""
