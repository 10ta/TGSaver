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
    assert C("t.me/a_bot 1234") is None, "4 位数既不是 id 也不是位置"


def test_do_grab_passes_forward_target():
    """抓取时写的 fw 必须传下去 —— 以前漏了，导致静默不转发。"""
    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text()
    body = src[src.index("async def do_grab"):src.index('@router.message(Command("grab"))')]
    calls = body.count("RUNNER.submit(")
    assert calls >= 1 and body.count("forward_to=fw_to") == calls
