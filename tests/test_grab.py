"""抓取写法的识别。只有一条规则：数字永远是「第几条」。

这个解析器挂在「任意文本」上，所以最大的风险是误判：把普通聊天内容
当成抓取指令，或者把普通消息链接吃掉。下面反例和正例一样多。
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

from bot import GrabSpec, old_syntax_hint, parse_grab_command as C  # noqa: E402


def _p(text):
    r = C(text)
    return None if r is None else [(x.target, x.topic, x.picks) for x in r]


def _pos(*ns):
    return [("pos", n) for n in ns]


def _ids(*ns):
    return [("id", n) for n in ns]


# ================================================================ 数字永远是第几条

@pytest.mark.parametrize("text,want", [
    ("t.me/ym_ss_bot",             [("ym_ss_bot", None, _pos(1))]),   # 不写数字 = 第 1 条
    ("t.me/ym_ss_bot 5",           [("ym_ss_bot", None, _pos(5))]),   # 单个数字也是第几条
    ("t.me/ym_ss_bot 1-5",         [("ym_ss_bot", None, _pos(1, 2, 3, 4, 5))]),
    ("t.me/ym_ss_bot 1 3 7-9",     [("ym_ss_bot", None, _pos(1, 3, 7, 8, 9))]),
    ("t.me/ym_ss_bot 2 1",         [("ym_ss_bot", None, _pos(2, 1))]),  # 按写的顺序
    ("t.me/ym_ss_bot 3-1",         [("ym_ss_bot", None, _pos(1, 2, 3))]),  # 区间从小到大
    ("t.me/ym_ss_bot 1 1 2",       [("ym_ss_bot", None, _pos(1, 2))]),  # 去重
    ("https://t.me/ym_ss_bot 2",   [("ym_ss_bot", None, _pos(2))]),
    ("t.me/ym_ss_bot/ 2",          [("ym_ss_bot", None, _pos(2))]),
])
def test_numbers_are_positions(text, want):
    assert _p(text) == want


def test_single_number_no_longer_means_latest_n():
    """统一前「bot 5」是最近 5 条，「bot 5 6」是第 5、6 条 —— 同一个 5 两种意思。"""
    assert C("t.me/a_bot 5")[0].positions == [5]
    assert C("t.me/a_bot 5 6")[0].positions == [5, 6]


# ================================================================ 目标只有三种写法

@pytest.mark.parametrize("text,target", [
    ("t.me/some_bot 1", "some_bot"),
    (".some_bot 1", "some_bot"),
    ("some_bot 1", "some_bot"),
    ("SomeBot 1", "SomeBot"),
    ("t.me/some_user 1", "some_user"),
    (".some_user 1", "some_user"),
    ("t.me/c/1234567890 1", "-1001234567890"),
    ("-1001234567890 1", "-1001234567890"),
])
def test_target_forms(text, target):
    assert C(text)[0].target == target


@pytest.mark.parametrize("text", [
    "@some_bot 1",          # 会被 Telegram 拦成 inline 查询，不再支持
    ">some_bot 1",          # 多余的同义写法，去掉
    "123456789 1",          # 纯正数对话 id 会和其他数字混淆，去掉
    "some_user 1",          # 不带前缀只认以 bot 结尾的
])
def test_removed_target_forms(text):
    assert C(text) is None


# ================================================================ 多个对话

def test_multiple_dialogs():
    assert _p(".a_bot 1-2 .b_bot 3") == [("a_bot", None, _pos(1, 2)),
                                         ("b_bot", None, _pos(3))]


def test_same_dialog_twice_merges():
    """统一后这不再是特例：数字都是第几条，合起来就行。"""
    assert _p(".x_bot 1 .x_bot 2") == [("x_bot", None, _pos(1, 2))]
    assert _p(".x_bot .x_bot") == [("x_bot", None, _pos(1))]


# ================================================================ 论坛话题与消息 id

def test_topic_positions():
    assert _p("https://t.me/c/2703619907/124 1-3") == [
        ("-1002703619907", 124, _pos(1, 2, 3))]
    assert _p("t.me/somegroup/45 2") == [("somegroup", 45, _pos(2))]


def test_message_ids_live_in_the_link():
    """消息 id 写进链接路径里，和普通消息链接一个形状，不再靠位数区分。"""
    assert _p("t.me/c/2703619907/124/4632-4634") == [
        ("-1002703619907", 124, _ids(4632, 4633, 4634))]
    assert _p("t.me/c/2703619907/4632-4633") == [
        ("-1002703619907", None, _ids(4632, 4633))]
    assert _p("t.me/somegroup/45/100-101") == [("somegroup", 45, _ids(100, 101))]


def test_small_message_ids_work_now():
    """以前 id 小于 1000 没法用（会被当成第几条），现在写在链接里就没问题。"""
    assert _p("t.me/c/2703619907/5-7") == [("-1002703619907", None, _ids(5, 6, 7))]


def test_id_range_and_positions_combined():
    r = C("t.me/c/2703619907/124 1-2 t.me/c/2703619907/124/4632-4633")
    assert len(r) == 1 and r[0].picks == _pos(1, 2) + _ids(4632, 4633)


def test_nothing_may_follow_an_id_range():
    """id 区间后面再跟数字，意思说不清，不认。"""
    assert C("t.me/c/2703619907/124/4632-4634 1") is None


def test_id_range_capped():
    assert len(C("t.me/c/2703619907/124/1000-9999")[0].ids) == 100


def test_positions_capped():
    assert len(C("t.me/a_bot 1-999")[0].positions) == 50


# ================================================================ 绝不能误判

@pytest.mark.parametrize("text", [
    "hello", "hello 5", "ok 5", "在吗", "1 3 5", "2024",
    "a_bot 1 hello", "t.me/a_bot 1-x", "t.me/a_bot 1234",
    "@ab", ".ab", "帮我看看 t.me/a_bot", "", "   ",
])
def test_not_a_grab_command(text):
    assert C(text) is None


@pytest.mark.parametrize("text", [
    "https://t.me/durov/1",
    "https://t.me/c/1234567890/123",
    "https://t.me/c/2703619907/124",          # 单独的话题链接 = 那条消息
    "https://t.me/c/1234567890/45/123",
    "https://t.me/chan/181?comment=4832",
    "https://t.me/durov/1 https://t.me/durov/2",
    "https://t.me/joinchat/AAAA",
])
def test_message_links_not_swallowed(text):
    assert C(text) is None


# ================================================================ 旧写法提示

@pytest.mark.parametrize("text,frag", [
    ("t.me/c/2703619907/124 4632-4638", "t.me/c/2703619907/124/4632-4638"),
    ("https://t.me/c/2703619907/124 4632", "https://t.me/c/2703619907/124/4632"),
    ("@ym_ss_bot 5", "t.me/ym_ss_bot"),
    (">ym_ss_bot 5", ".ym_ss_bot"),
    ("123456789 3", "t.me/c/123456789"),
])
def test_old_syntax_gets_a_hint(text, frag):
    assert frag in old_syntax_hint(text)


@pytest.mark.parametrize("text", ["hello 2024", "2024", "在吗", "", "t.me/a_bot 1",
                                  "https://x.com/a/status/12 2024"])
def test_no_hint_for_normal_text(text):
    assert old_syntax_hint(text) is None


def test_do_grab_passes_forward_target():
    """抓取时写的 fw 必须传下去。"""
    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text()
    body = src[src.index("async def do_grab"):src.index('@router.message(Command("grab"))')]
    calls = body.count("RUNNER.submit(")
    assert calls >= 1 and body.count("forward_to=fw_to") == calls


def test_do_grab_has_no_separate_delivery_branch():
    """统一后只有一种交付：一条原样发，多条拼相册，都是一个任务。"""
    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text()
    body = src[src.index("async def do_grab"):src.index('@router.message(Command("grab"))')]
    assert body.count("RUNNER.submit(") == 1
    assert "is_legacy" not in src and "GRAB_MAX" not in src


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
    sp = C("t.me/c/2703619907/124/4632-4637")[0]
    picks, missing, _ = await bot._resolve_picks(c, object(), sp)
    assert [m.id for m in picks] == [4632, 4636, 4637]
    assert missing == []


@pytest.mark.asyncio
async def test_resolve_picks_missing_and_service():
    import bot
    c = ScanClient(recent=[_msg(9)], by_id=[_msg(4632), _msg(4633, service=True)])
    sp = GrabSpec("-1002703619907", [("pos", 1), ("pos", 2), ("id", 4632), ("id", 4633), ("id", 4634)], 124)
    picks, missing, _ = await bot._resolve_picks(c, object(), sp)
    assert [m.id for m in picks] == [9, 4632]
    assert missing == ["第 2 条", "消息 4633", "消息 4634"], "系统消息当作取不到"


@pytest.mark.asyncio
async def test_resolve_picks_keeps_written_order():
    import bot
    c = ScanClient(recent=[_msg(9), _msg(8)], by_id=[_msg(4632)])
    sp = GrabSpec("-1002703619907", [("pos", 2), ("id", 4632), ("pos", 1)], 124)
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
