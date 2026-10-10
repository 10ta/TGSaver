"""/setting：开关的存取、按钮、以及两个来源名开关对说明文字的影响。"""
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

import json  # noqa: E402

import pytest  # noqa: E402

import assemble  # noqa: E402
import db  # noqa: E402
import prefs  # noqa: E402
import tweet  # noqa: E402
from streamer import WebMedia  # noqa: E402
from tweet import Tweet  # noqa: E402


@pytest.fixture
async def fresh(tmp_path):
    from config import CFG
    object.__setattr__(CFG, "db_path", tmp_path / "s.db")
    await db.init()
    yield
    await db.close()


@pytest.fixture(autouse=True)
def _no_signature(monkeypatch):
    monkeypatch.setattr(tweet, "SIGN_TEXT", "")
    monkeypatch.setattr(tweet, "SIGN_URL", "")


# ================================================================ 存取

def test_defaults(monkeypatch):
    monkeypatch.setattr(tweet, "TWEET_MODE", "auto")
    p = prefs.defaults()
    assert p == prefs.Prefs(keep_spoiler=False, tw_name=True, tg_source=False,
                            pack_delete=False, tweet_mode="auto")


def test_tweet_mode_default_follows_env(monkeypatch):
    monkeypatch.setattr(tweet, "TWEET_MODE", "media")
    assert prefs.defaults().tweet_mode == "media"
    monkeypatch.setattr(tweet, "TWEET_MODE", "garbage")
    assert prefs.defaults().tweet_mode == "auto"


@pytest.mark.asyncio
async def test_roundtrip_stores_only_changes(fresh):
    p = await prefs.update(42, keep_spoiler=True)
    assert p.keep_spoiler and (await prefs.get(42)).keep_spoiler
    assert json.loads((await db.get_user(42))["settings"]) == {"keep_spoiler": True}
    await prefs.update(42, keep_spoiler=False)
    assert (await db.get_user(42))["settings"] is None, "改回默认就不存"


@pytest.mark.asyncio
async def test_per_user(fresh):
    await db.upsert_user(77, status="active")
    await prefs.update(77, tw_name=False)
    assert (await prefs.get(42)).tw_name and not (await prefs.get(77)).tw_name


@pytest.mark.parametrize("raw", ["{坏掉的", '{"tweet_mode": "nope", "tw_name": "yes", "old": 1}'])
def test_bad_data_falls_back(raw):
    assert prefs._decode(raw) == prefs.defaults()


@pytest.mark.asyncio
async def test_old_database_gets_column(tmp_path):
    """老库没有 settings 列，启动时自动补上。"""
    import aiosqlite
    from config import CFG
    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as c:
        await c.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, "
                        "role TEXT NOT NULL DEFAULT 'user', status TEXT NOT NULL DEFAULT 'pending', "
                        "session_enc BLOB, session_status TEXT NOT NULL DEFAULT 'none', "
                        "relay_channel_id INTEGER, added_by INTEGER, added_at INTEGER NOT NULL, "
                        "last_active INTEGER, task_count INTEGER NOT NULL DEFAULT 0, "
                        "bytes_total INTEGER NOT NULL DEFAULT 0)")
        await c.commit()
    object.__setattr__(CFG, "db_path", path)
    await db.init()
    try:
        await prefs.update(42, pack_delete=True)
        assert (await prefs.get(42)).pack_delete
    finally:
        await db.close()


# ================================================================ 按钮

def test_toggle_and_cycle():
    p = prefs.Prefs()
    assert prefs.toggle(p, "keep_spoiler") == {"keep_spoiler": True}
    assert prefs.toggle(p, "tw_name") == {"tw_name": False}
    assert prefs.toggle(p, "tweet_mode") == {"tweet_mode": "preview"}
    assert prefs.toggle(prefs.Prefs(tweet_mode="media"), "tweet_mode") == {"tweet_mode": "auto"}
    assert prefs.toggle(p, "rm -rf") == {}


def test_keyboard_labels():
    kb = prefs.keyboard(prefs.Prefs(), None)
    texts = [r[0].text for r in kb.inline_keyboard]
    assert texts == ["剧透：去掉", "推文昵称：显示", "TG 来源名：隐藏",
                     "组装后删除原消息：否", "推文形态：自动"]
    assert all(r[0].callback_data.startswith("pf:") for r in kb.inline_keyboard)
    kb = prefs.keyboard(prefs.Prefs(), "@optv4")
    assert kb.inline_keyboard[-1][0].text.startswith("fw 默认去向：@optv4")


class _Q:
    def __init__(self, data, uid=42):
        from types import SimpleNamespace
        self.data = data
        self.from_user = SimpleNamespace(id=uid)
        self.toasts = []
        self.markups = []

        async def edit(reply_markup=None):
            self.markups.append(reply_markup)
        self.message = SimpleNamespace(edit_reply_markup=edit)

    async def answer(self, text=None, **kw):
        self.toasts.append(text)


@pytest.mark.asyncio
async def test_press_toggles_and_refreshes(fresh):
    q = _Q("pf:tg_source")
    await prefs.on_press(q)
    assert (await prefs.get(42)).tg_source
    assert q.markups[0].inline_keyboard[2][0].text == "TG 来源名：显示"
    assert q.toasts == ["已保存"]


@pytest.mark.asyncio
async def test_press_clears_forward(fresh):
    await db.upsert_user(42, last_forward="@a")
    q = _Q("pf:fw_clear")
    await prefs.on_press(q)
    assert (await db.get_user(42))["last_forward"] is None
    assert all("fw" not in r[0].text for r in q.markups[0].inline_keyboard)


@pytest.mark.asyncio
async def test_press_by_stranger_rejected(fresh):
    q = _Q("pf:keep_spoiler", uid=999)
    await prefs.on_press(q)
    assert q.toasts == ["无权使用。"] and not (await prefs.get(999)).keep_spoiler


# ================================================================ 推文昵称开关

def _tw(text="正文", media=None, name="花无尘", sn="HuaWu"):
    return Tweet("1", name, sn, text, media or [])


def test_tweet_name_shown_by_default():
    h, _ = tweet.build_html(_tw(), 1024)
    assert h.startswith("<blockquote><b>花无尘</b> : 正文</blockquote>")


def test_tweet_name_hidden():
    h, _ = tweet.build_html(_tw(), 1024, show_name=False)
    assert h.startswith("<blockquote>正文</blockquote>\n<a href=")
    assert "花无尘" not in h and "#HuaWu" in h, "#ID 还在，只去掉昵称"


def test_tweet_name_hidden_no_text_no_quote():
    h, _ = tweet.build_html(_tw(text=""), 1024, show_name=False)
    assert "<blockquote>" not in h and h.startswith("<a href=")


def test_tweet_name_hidden_truncation_budget():
    """去掉昵称后省下的长度要还给正文。"""
    long = "字" * 2000
    with_name, _ = tweet.build_html(_tw(text=long), 200)
    without, _ = tweet.build_html(_tw(text=long), 200, show_name=False)
    assert without.count("字") > with_name.count("字")
    plain = without.replace("<blockquote>", "").replace("</blockquote>", "")
    assert tweet.utf16_len(plain.split("<a href")[0]) <= 200


def test_plan_passes_show_name():
    t = _tw(media=[WebMedia("photo", "https://a")])
    assert "花无尘" not in tweet.plan(t, mode="media", show_name=False)["html"]
    assert "花无尘" in tweet.plan(t, mode="media")["html"]


def test_batch_name_hidden_keeps_labels():
    a = _tw("甲", [WebMedia("photo", "https://a")], name="A", sn="a")
    b = _tw("", [WebMedia("photo", "https://b"), WebMedia("photo", "https://c")],
            name="B", sn="b")
    c = _tw("纯文字", [], name="C", sn="c")
    extra, _ = tweet.plan_batch([a, b, c], show_name=False)
    h = extra["html"]
    assert "<blockquote>1 : 甲</blockquote>" in h
    assert "<blockquote>2-3 :</blockquote>" in h
    assert "<blockquote>纯文字</blockquote>" in h
    assert "<b>" not in h


@pytest.mark.asyncio
async def test_runner_uses_prefs_for_tweet(monkeypatch):
    """调度器按发起人的设置规划推文：昵称开关、推文形态。"""
    import taskqueue
    seen = {}

    def fake_plan(tw, mode=None, show_name=True):
        seen.update(mode=mode, show_name=show_name)
        return {"kind": "tweet", "mode": "text", "html": "x", "caption": False,
                "url": tw.url, "preview_url": tw.preview_url}

    async def fake_fetch(ref):
        return _tw()

    async def nop(*a, **kw):
        return None

    monkeypatch.setattr(tweet, "plan", fake_plan)
    monkeypatch.setattr(tweet, "fetch", fake_fetch)
    r = taskqueue.Runner.__new__(taskqueue.Runner)
    r._send_tweet_media = nop
    job = taskqueue.Job(1, 42, "https://x.com/a/status/1", 9, 5)
    job.prefs = prefs.Prefs(tw_name=False, tweet_mode="media")
    await r._run_tweet(job, "fast")
    assert seen == {"mode": "media", "show_name": False}


# ================================================================ TG 来源名开关

SRC = [("某频道", "#chan", 1, 3, [("1", "第一条"), ("2-3", "")])]


def test_caption_with_source():
    h, ok = assemble.build_caption(SRC, 1024)
    assert ok and h == "<blockquote><b>某频道</b> :\n1 : 第一条</blockquote>\n1-3 · #chan"


def test_caption_without_source():
    h, ok = assemble.build_caption(SRC, 1024, show_source=False)
    assert ok and h == "<blockquote>1 : 第一条</blockquote>"


def test_caption_without_source_drops_empty_blocks():
    src = SRC + [("别的群", "#other", 4, 5, [("4-5", "")])]
    h, _ = assemble.build_caption(src, 1024, show_source=False)
    assert h == "<blockquote>1 : 第一条</blockquote>"


def test_caption_nothing_at_all_with_signature(monkeypatch):
    monkeypatch.setattr(tweet, "SIGN_TEXT", "@欧派TV")
    h, _ = assemble.build_caption([("x", "#x", 1, 2, [("1-2", "")])], 1024,
                                  show_source=False)
    assert h == "via @欧派TV", "只剩署名时前面不空两行"


def test_caption_empty_name_never_shown():
    h, _ = assemble.build_caption([("", "", 1, 2, [("1", "hi")])], 1024)
    assert h == "<blockquote>1 : hi</blockquote>"


def test_caption_without_source_budget_goes_to_text():
    long = "字" * 3000
    src = [("某个很长很长的频道名字", "#a_very_long_channel_tag", 1, 2, [("1-2", long)])]
    a, _ = assemble.build_caption(src, 300)
    b, _ = assemble.build_caption(src, 300, show_source=False)
    assert b.count("字") > a.count("字")
