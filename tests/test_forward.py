"""fw 转发。

用 user 账号的 forward_messages(drop_author=True)，也就是客户端上的
「hide sender name」。重点验证：指令解析不误伤普通文本、去向规整、
权限校验、以及带 fw 时结果只到去向、不经过 bot。
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

import db  # noqa: E402
import forward  # noqa: E402
from forward import ForwardError  # noqa: E402
from parser import parse_forward  # noqa: E402


# ================================================================ 指令解析

@pytest.mark.parametrize("text,target,rest", [
    ("https://x.com/a/status/12 fw @mychan", "@mychan", "https://x.com/a/status/12"),
    ("https://x.com/a/status/12 FW @MyChan", "@MyChan", "https://x.com/a/status/12"),
    ("fw @mychan", "@mychan", ""),
    ("fw -1001234567890", "-1001234567890", ""),
    ("fw t.me/mychan", "t.me/mychan", ""),
    ("fw https://t.me/mychan", "https://t.me/mychan", ""),
    ("link fw", None, "link"),
    ("fw", None, ""),
])
def test_parsed(text, target, rest):
    has, got, left = parse_forward(text)
    assert has and got == target and left == rest


@pytest.mark.parametrize("text", [
    "no fw here x",             # 普通句子里的 fw
    "看 fw 这个词",
    "fw @ab",                   # 用户名太短，不是合法去向
    "forward @mychan",
    "https://x.com/a/status/12",
    "",
])
def test_not_a_directive(text):
    has, _, left = parse_forward(text)
    assert not has and left == text


def test_stripped_before_link_parsing():
    """fw 后面的 t.me 是去向，不能被当成「抓取该对话」。"""
    from bot import parse_grab_target
    has, target, rest = parse_forward("t.me/chan/123 fw t.me/mychan")
    assert has and target == "t.me/mychan"
    assert parse_grab_target(rest) is None, "剥掉 fw 后不该再被当成抓取指令"


def test_works_with_nosp():
    from parser import wants_nosp
    has, target, rest = parse_forward("https://t.me/c/1234567890/5 nosp fw @mychan")
    assert has and target == "@mychan"
    assert wants_nosp(rest)


# ================================================================ 去向规整

@pytest.mark.parametrize("spec,want", [
    ("@mychan", "@mychan"),
    ("t.me/mychan", "@mychan"),
    ("https://t.me/mychan", "@mychan"),
    ("https://www.t.me/mychan/", "@mychan"),
    ("-1001234567890", "-1001234567890"),
    ("123456789", "123456789"),
    ("t.me/c/1234567890", "-1001234567890"),
])
def test_normalize(spec, want):
    assert forward.normalize(spec) == want


@pytest.mark.parametrize("spec,frag", [
    ("", "没有指定"),
    ("t.me/+AbCdEf", "邀请链接"),
    ("t.me/joinchat/AbCdEf", "邀请链接"),
    ("@ab", "格式不对"),
    ("随便写的", "看不懂"),
    ("123", "看不懂"),
])
def test_normalize_rejects(spec, frag):
    with pytest.raises(ForwardError, match=frag):
        forward.normalize(spec)


# ================================================================ 权限校验

class Perms:
    def __init__(self, creator=False, post=None, send=None):
        self.is_creator = creator
        self.post_messages = post
        self.send_messages = send


class FakeClient:
    def __init__(self, entity=None, perms=None, entity_error=None):
        self.entity = entity if entity is not None else type("E", (), {"broadcast": False})()
        self.perms = perms
        self.entity_error = entity_error
        self.forwarded = []

    async def get_entity(self, x):
        if self.entity_error:
            raise self.entity_error
        return self.entity

    async def get_permissions(self, entity, who):
        if isinstance(self.perms, Exception):
            raise self.perms
        return self.perms

    async def forward_messages(self, entity, messages, from_peer=None,
                               drop_author=False):
        self.forwarded.append((entity, list(messages), from_peer, drop_author))
        return [object() for _ in messages]


def _chan(broadcast=True):
    return type("E", (), {"broadcast": broadcast})()


@pytest.mark.asyncio
async def test_resolve_unknown_target():
    c = FakeClient(entity_error=ValueError("no such"))
    with pytest.raises(ForwardError, match="找不到"):
        await forward.resolve(c, "@mychan")


@pytest.mark.asyncio
async def test_resolve_channel_without_post_rights():
    c = FakeClient(_chan(), Perms(post=False))
    with pytest.raises(ForwardError, match="Post Messages"):
        await forward.resolve(c, "@mychan")


@pytest.mark.asyncio
async def test_resolve_group_muted():
    c = FakeClient(_chan(broadcast=False), Perms(send=False))
    with pytest.raises(ForwardError, match="没有发布权限"):
        await forward.resolve(c, "@mygroup")


@pytest.mark.asyncio
async def test_resolve_creator_always_ok():
    c = FakeClient(_chan(), Perms(creator=True, post=False))
    assert await forward.resolve(c, "@mychan") is c.entity


@pytest.mark.asyncio
async def test_resolve_tolerates_permission_query_failure():
    """权限查不到就放行，交给真正发送时判断，不要平白挡住。"""
    c = FakeClient(_chan(), RuntimeError("boom"))
    assert await forward.resolve(c, "@mychan") is c.entity


# ================================================================ 转发

@pytest.mark.asyncio
async def test_send_hides_author():
    c = FakeClient(_chan(), Perms(post=True))
    n = await forward.send(c, "@mychan", -100999, [11, 12, 13])
    assert n == 3
    entity, ids, from_peer, drop = c.forwarded[0]
    assert ids == [11, 12, 13] and from_peer == -100999
    assert drop is True, "必须隐藏来源，否则会露出中转频道"


@pytest.mark.asyncio
async def test_send_without_messages():
    with pytest.raises(ForwardError, match="没有可转发"):
        await forward.send(FakeClient(), "@mychan", -100, [])


@pytest.mark.asyncio
async def test_send_reports_failure_reason():
    class Boom(FakeClient):
        async def forward_messages(self, *a, **kw):
            raise RuntimeError("CHAT_WRITE_FORBIDDEN")

    with pytest.raises(ForwardError, match="CHAT_WRITE_FORBIDDEN"):
        await forward.send(Boom(_chan(), Perms(post=True)), "@c", -100, [1])


def test_forward_error_is_fatal():
    """去向不对重试多少次都一样，必须当场判死。"""
    import taskqueue
    assert issubclass(ForwardError, taskqueue.FATAL)


# ================================================================ 落库

def _set_db_path(p):
    from config import CFG
    object.__setattr__(CFG, "db_path", p)


@pytest.fixture
async def fresh(tmp_path):
    _set_db_path(tmp_path / "t.db")
    await db.init()
    yield
    await db.close()


@pytest.mark.asyncio
async def test_last_forward_per_user(fresh):
    await db.upsert_user(42, last_forward="@a")
    await db.upsert_user(777, status="active", last_forward="@b")
    assert (await db.get_user(42))["last_forward"] == "@a"
    assert (await db.get_user(777))["last_forward"] == "@b", "各人记各人的"


@pytest.mark.asyncio
async def test_task_carries_forward_target(fresh):
    tid = await db.add_task(42, "l", 1, 1, forward_to="@mychan")
    assert (await db.get_task(tid))["forward_to"] == "@mychan"


@pytest.mark.asyncio
async def test_migration_adds_columns(tmp_path):
    """老库没有这两列，升级时要补上且不丢数据。"""
    import aiosqlite
    p = tmp_path / "old.db"
    async with aiosqlite.connect(p) as c:
        await c.execute("""CREATE TABLE users (
            user_id INTEGER PRIMARY KEY, username TEXT,
            role TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'active',
            session_enc BLOB, session_status TEXT NOT NULL DEFAULT 'none',
            relay_channel_id INTEGER, added_by INTEGER,
            added_at INTEGER NOT NULL, last_active INTEGER,
            task_count INTEGER NOT NULL DEFAULT 0,
            bytes_total INTEGER NOT NULL DEFAULT 0)""")
        await c.execute("INSERT INTO users (user_id, added_at) VALUES (42, 0)")
        await c.commit()
    _set_db_path(p)
    await db.init()
    row = await db.get_user(42)
    assert row["last_forward"] is None and row["user_id"] == 42
    await db.close()


# ================================================================ 调度

class FakeBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("text", text, kw))

    async def send_photo(self, chat_id, photo, **kw):
        self.calls.append(("photo", photo, kw))

    async def send_media_group(self, chat_id, media, **kw):
        self.calls.append(("group", media, kw))

    async def copy_message(self, chat_id, from_chat_id, message_id, **kw):
        self.calls.append(("copy", message_id, kw))

    async def copy_messages(self, chat_id, from_chat_id, message_ids, **kw):
        self.calls.append(("copies", list(message_ids), kw))
        return list(message_ids)


class RelayClient(FakeClient):
    """记录 user 账号往中转频道发了什么、又转发了什么。"""

    def __init__(self):
        super().__init__(_chan(), Perms(post=True))
        self.sent = []
        self._next = 500

    async def send_file(self, peer, file, caption=None, **kw):
        self._next += 1
        self.sent.append(("file", file, caption))
        return type("M", (), {"id": self._next})()

    async def send_message(self, peer, text, **kw):
        self._next += 1
        self.sent.append(("text", text, kw))
        return type("M", (), {"id": self._next})()


def _runner(bot=None):
    import asyncio
    import taskqueue
    r = taskqueue.Runner.__new__(taskqueue.Runner)
    r.bot = bot or FakeBot()
    r.fast, r.slow = asyncio.Queue(), asyncio.Queue()
    r._active = {}
    r.said = []

    async def say(job, text):
        r.said.append(text)
    r._say = say

    async def drop(job):
        pass
    r._drop_status = drop
    return r


@pytest.mark.asyncio
async def test_fw_is_additive_not_a_replacement(fresh, monkeypatch):
    """fw 是附加步骤：本人该收到的一份不能少，之后再额外转一份。"""
    import taskqueue
    client = RelayClient()

    async def acquire(uid):
        return client
    monkeypatch.setattr(taskqueue.POOL, "acquire", acquire)

    r = _runner()
    tid = await db.add_task(42, "https://t.me/chan/12", 9, 5, forward_to="@mychan")
    job = taskqueue.Job(tid, 42, "https://t.me/chan/12", 9, 5,
                        forward_to="@mychan", relay_chat_id=-100999,
                        relay_ids=[11, 12], relay_is_album=True)
    await r._deliver(job)

    assert [c[0] for c in r.bot.calls] == ["copies"], "本人那一份不能少"
    entity, ids, from_peer, drop = client.forwarded[0]
    assert ids == [11, 12] and from_peer == -100999 and drop is True
    assert (await db.get_task(tid))["state"] == "done"


@pytest.mark.asyncio
async def test_without_fw_still_delivers_to_user(fresh):
    import taskqueue
    r = _runner()
    tid = await db.add_task(42, "https://t.me/chan/12", 9, 5)
    job = taskqueue.Job(tid, 42, "https://t.me/chan/12", 9, 5,
                        relay_chat_id=-100999, relay_ids=[11], relay_is_album=False)
    await r._deliver(job)
    assert [c[0] for c in r.bot.calls] == ["copy"]


@pytest.mark.asyncio
async def test_fw_tweet_built_by_user_account(fresh, monkeypatch):
    """快通道下 bot 已经发给用户了，中转频道没有副本，
    所以要用 user 账号另做一份再转 —— bot 发的东西转不走。"""
    import taskqueue
    import tweet
    from streamer import WebMedia

    client = RelayClient()

    async def acquire(uid):
        return client
    monkeypatch.setattr(taskqueue.POOL, "acquire", acquire)

    r = _runner()
    tw = tweet.Tweet("12", "J", "j", "hi", [WebMedia("photo", "https://a.jpg")])
    tid = await db.add_task(42, "https://x.com/j/status/12", 9, 5,
                            forward_to="@mychan")
    job = taskqueue.Job(tid, 42, "https://x.com/j/status/12", 9, 5,
                        tweet=tw, forward_to="@mychan")
    job.extra = tweet.plan(tw, mode="media")

    await r._forward_tweet(job)

    assert client.sent and client.sent[0][0] == "file"
    assert client.sent[0][1] == ["https://a.jpg"] or \
        client.sent[0][1] == "https://a.jpg", "URL 交给 Telegram 自己拉"
    assert client.forwarded[0][3] is True, "转发时隐藏来源"


@pytest.mark.asyncio
async def test_fw_preview_built_by_user_account(fresh, monkeypatch):
    """预览形态同理：bot 发的预览转不走，副本要 user 账号自己发。"""
    import taskqueue
    import tweet
    from streamer import WebMedia

    calls = []

    async def fake_send_text(client, peer, html_text, preview_url=None):
        calls.append((peer, preview_url))
        return [777]
    import streamer
    monkeypatch.setattr(streamer, "send_text_message", fake_send_text)

    client = RelayClient()

    async def acquire(uid):
        return client
    monkeypatch.setattr(taskqueue.POOL, "acquire", acquire)

    r = _runner()
    tw = tweet.Tweet("12", "J", "j", "hi", [WebMedia("photo", "https://a.jpg")])
    tid = await db.add_task(42, "https://x.com/j/status/12", 9, 5,
                            forward_to="@mychan")
    job = taskqueue.Job(tid, 42, "https://x.com/j/status/12", 9, 5,
                        tweet=tw, forward_to="@mychan")
    job.extra = tweet.plan(tw, mode="preview")

    await r._forward_tweet(job)

    assert calls and calls[0][1] == tw.preview_url, "预览地址要带上"
    assert client.forwarded[0][1] == [777]


@pytest.mark.asyncio
async def test_fw_falls_back_to_relay_when_telegram_cannot_fetch(fresh, monkeypatch):
    """Telegram 拉不动就本机下载上传，仍然由 user 账号发，仍然转发。"""
    import taskqueue
    import tweet
    import streamer
    from streamer import WebMedia

    class Refuse(RelayClient):
        async def send_file(self, *a, **kw):
            raise RuntimeError("WEBPAGE_CURL_FAILED")

    client = Refuse()

    async def acquire(uid):
        return client
    monkeypatch.setattr(taskqueue.POOL, "acquire", acquire)

    async def fake_relay(cl, items, peer, caption_html=None, **kw):
        return [901], 12345, ""
    monkeypatch.setattr(streamer, "relay_web_media", fake_relay)

    r = _runner()
    tw = tweet.Tweet("12", "J", "j", "hi", [WebMedia("video", "https://big.mp4")])
    tid = await db.add_task(42, "https://x.com/j/status/12", 9, 5,
                            forward_to="@mychan")
    job = taskqueue.Job(tid, 42, "https://x.com/j/status/12", 9, 5,
                        tweet=tw, forward_to="@mychan")
    job.extra = tweet.plan(tw, mode="media")

    await r._forward_tweet(job)
    assert client.forwarded[0][1] == [901]


@pytest.mark.asyncio
async def test_fw_target_persisted_on_task(fresh):
    """去向跟着任务走，重启后恢复出来还在。"""
    import taskqueue
    tid = await db.add_task(42, "l", 1, 1, forward_to="@mychan")
    await db.update_task(tid, state="relayed", relay_ids='[1]',
                         relay_chat_id=-100)
    rows = await db.pending_tasks()
    job = taskqueue.Job(
        task_id=rows[0]["id"], owner_id=42, link="l",
        request_chat_id=1, request_msg_id=1,
        forward_to=rows[0]["forward_to"])
    assert job.forward_to == "@mychan"


@pytest.mark.asyncio
async def test_fw_failure_does_not_lose_delivered_copy(fresh, monkeypatch):
    """转发失败只提示，不能让整个任务失败 —— 内容已经在用户手里了。"""
    import taskqueue

    class Boom(RelayClient):
        async def forward_messages(self, *a, **kw):
            raise RuntimeError("CHAT_WRITE_FORBIDDEN")

    async def acquire(uid):
        return Boom()
    monkeypatch.setattr(taskqueue.POOL, "acquire", acquire)

    r = _runner()
    tid = await db.add_task(42, "l", 9, 5, forward_to="@mychan")
    job = taskqueue.Job(tid, 42, "l", 9, 5, forward_to="@mychan",
                        relay_chat_id=-100999, relay_ids=[11])
    await r._deliver(job)

    assert [c[0] for c in r.bot.calls] == ["copy"], "本人那一份照常送达"
    assert any("转发失败" in x for x in r.said)
    assert (await db.get_task(tid))["state"] == "done"


@pytest.mark.asyncio
async def test_fw_without_relay_copy_reports_clearly(fresh):
    import taskqueue
    r = _runner()
    tid = await db.add_task(42, "l", 9, 5, forward_to="@mychan")
    job = taskqueue.Job(tid, 42, "l", 9, 5, forward_to="@mychan")
    await r._forward_from_relay(job)
    assert any("没有可转发" in x for x in r.said)


def test_credential_check_looks_at_session_owner():
    """凭据属于机主。查发起人自己的话，授权用户永远是「未登录」。"""
    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text()
    block = src[src.index("async def _resolve_forward"):src.index("async def _allowed")]
    import re
    assert "db.get_user(acl.session_user(uid))" in block
    # owner_row["session_status"] 是对的，裸 row["session_status"] 不是
    assert not re.search(r'(?<!owner_)row\["session_status"\]', block), \
        "不能查发起人自己的登录状态"
