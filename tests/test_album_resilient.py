"""相册尽量成组，保不住就不 fw。

线上又遇到一次「相册整组发送失败，已逐条转存」：单条都发出去了，fw 也跟着逐条
转到了公开频道。这里验证三件事：
1. 事先能知道的（服务器认定是动图、贴纸、文件……）按相册规则分开，不算失败
2. 事先不知道、被拒了的，对半拆开重试：其余项大多仍然成组，而不是全部散开
3. 分组没保住时，本人照常收到，但不转发到公开去向
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "h")
os.environ.setdefault("BOT_TOKEN", "t")
os.environ.setdefault("OWNER_ID", "42")
os.environ.setdefault("RELAY_CHANNEL_ID", "-1001111111111")
os.environ.setdefault("SECRET_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402
from telethon.tl import functions, types  # noqa: E402

import streamer  # noqa: E402
from streamer import MediaSpec  # noqa: E402

NOW = datetime.now(timezone.utc)


def _spec(name, kind="video"):
    if kind == "photo":
        return MediaSpec(size=10, file_name=name, mime_type="image/jpeg", attributes=[],
                         is_photo=True, force_document=False, caption="", entities=[])
    attrs = [types.DocumentAttributeVideo(duration=1, w=1, h=1),
             types.DocumentAttributeFilename(file_name=name)]
    return MediaSpec(size=10, file_name=name, mime_type="video/mp4", attributes=attrs,
                     is_photo=False, force_document=False, caption="", entities=[])


class Server:
    """模拟 Telegram：

    server_attrs  某个文件上传后服务器认定的属性（可以和我们给的不一样）
    reject        这些文件进相册就整组拒收，单发没问题（原因未知的那一类）
    """

    def __init__(self, server_attrs=None, reject=()):
        self.server_attrs = server_attrs or {}
        self.reject = set(reject)
        self.by_id = {}
        self.next_doc = 1
        self.next_msg = 1000
        self.multi_ok = []
        self.multi_rejected = []
        self.singles = []
        self.deleted = []

    def _ups(self, n):
        ups = []
        for _ in range(n):
            self.next_msg += 1
            ups.append(types.UpdateNewChannelMessage(
                message=types.Message(id=self.next_msg, peer_id=types.PeerChannel(1),
                                      date=NOW, message=""), pts=1, pts_count=1))
        return types.Updates(updates=ups, users=[], chats=[], date=NOW, seq=0)

    def _name(self, media):
        return self.by_id[media.id.id]

    async def __call__(self, req):
        if isinstance(req, functions.messages.UploadMediaRequest):
            did = self.next_doc
            self.next_doc += 1
            if isinstance(req.media, types.InputMediaUploadedPhoto):
                self.by_id[did] = req.media.file      # 句柄就是文件名
                return types.MessageMediaPhoto(photo=types.Photo(
                    id=did, access_hash=1, file_reference=b"", date=NOW, sizes=[], dc_id=1))
            name = next(a.file_name for a in req.media.attributes
                        if isinstance(a, types.DocumentAttributeFilename))
            self.by_id[did] = name
            attrs = self.server_attrs.get(name, req.media.attributes)
            return types.MessageMediaDocument(document=types.Document(
                id=did, access_hash=1, file_reference=b"", date=NOW,
                mime_type="video/mp4", size=1, dc_id=1, attributes=attrs))
        if isinstance(req, functions.messages.SendMultiMediaRequest):
            names = [self._name(sm.media) for sm in req.multi_media]
            animated = any(
                any(isinstance(a, types.DocumentAttributeAnimated)
                    for a in self.server_attrs.get(n, [])) for n in names)
            if animated or self.reject & set(names):
                self.multi_rejected.append(names)
                raise RuntimeError("MEDIA_INVALID")
            self.multi_ok.append(names)
            return self._ups(len(names))
        if isinstance(req, functions.messages.SendMediaRequest):
            self.singles.append(self._name(req.media))
            return self._ups(1)
        raise AssertionError(type(req).__name__)

    async def send_file(self, *a, **kw):
        raise AssertionError("不该重新上传")

    async def upload_file(self, f, **kw):
        return f


def _run(server, names, kinds=None):
    kinds = kinds or ["video"] * len(names)
    specs = [_spec(n, k) for n, k in zip(names, kinds)]
    return streamer.send_handles_as_album(server, -100, list(names), specs)


# ================================================================ 分段

@pytest.mark.parametrize("kinds,runs", [
    (["visual", "visual"], [[0, 1]]),
    (["visual", None, "visual"], [[0], [1], [2]]),
    (["visual", "visual", "file", "file", "visual"], [[0, 1], [2, 3], [4]]),
    ([None, None], [[0], [1]]),
    (["visual"] * 12, [list(range(10)), [10, 11]]),
])
def test_kind_runs(kinds, runs):
    assert streamer.kind_runs(kinds) == runs


def test_attrs_kind():
    V = types.DocumentAttributeVideo
    assert streamer.attrs_kind([V(duration=1, w=1, h=1)]) == "visual"
    assert streamer.attrs_kind([V(duration=1, w=1, h=1), types.DocumentAttributeAnimated()]) is None
    assert streamer.attrs_kind([V(duration=1, w=1, h=1, round_message=True)]) is None
    assert streamer.attrs_kind([types.DocumentAttributeAudio(duration=1, voice=True)]) is None
    assert streamer.attrs_kind([types.DocumentAttributeAudio(duration=1)]) == "audio"
    assert streamer.attrs_kind([types.DocumentAttributeFilename(file_name="a.zip")]) == "file"


# ================================================================ 事先知道的

@pytest.mark.asyncio
async def test_server_says_animated_split_beforehand_without_failure():
    """我们当它是视频，服务器认定是动图：按服务器的为准事先分开，一次都不被拒。"""
    srv = Server(server_attrs={"c.mp4": [
        types.DocumentAttributeVideo(duration=1, w=1, h=1, nosound=True),
        types.DocumentAttributeAnimated()]})
    ids, note = await _run(srv, ["a.mp4", "b.mp4", "c.mp4", "d.mp4", "e.mp4"])
    assert note == "", "事先知道的分开不算失败，fw 照常"
    assert srv.multi_rejected == []
    assert srv.multi_ok == [["a.mp4", "b.mp4"], ["d.mp4", "e.mp4"]]
    assert srv.singles == ["c.mp4"]
    assert len(ids) == 5 and ids == sorted(ids)


@pytest.mark.asyncio
async def test_server_attrs_missing_falls_back_to_ours():
    srv = Server(server_attrs={"a.mp4": [], "b.mp4": []})
    ids, note = await _run(srv, ["a.mp4", "b.mp4"])
    assert note == "" and srv.multi_ok == [["a.mp4", "b.mp4"]]


# ================================================================ 事先不知道的

@pytest.mark.asyncio
async def test_unknown_reject_keeps_most_grouped():
    names = [f"{c}.mp4" for c in "abcdefghij"]
    srv = Server(reject={"d.mp4"})
    ids, note = await _run(srv, names)
    assert note == streamer.SPLIT_NOTE
    assert "d.mp4" in srv.singles and len(srv.singles) <= 3
    assert sum(len(g) for g in srv.multi_ok) >= 7, "其余大多仍然成组"
    assert len(ids) == 10 and ids == sorted(ids), "顺序不乱"


@pytest.mark.asyncio
async def test_failure_logs_group_members(caplog):
    srv = Server(reject={"b.mp4"})
    await _run(srv, ["a.mp4", "b.mp4"])
    assert any("b.mp4(video/mp4" in r.getMessage() for r in caplog.records), \
        "日志里要能看到被拒的组里有哪些文件"


@pytest.mark.asyncio
async def test_no_reupload_when_splitting():
    srv = Server(reject={"a.mp4", "b.mp4", "c.mp4"})
    ids, _ = await _run(srv, ["a.mp4", "b.mp4", "c.mp4"])
    assert srv.singles == ["a.mp4", "b.mp4", "c.mp4"] and len(ids) == 3


# ================================================================ 不 fw

def _runner():
    import taskqueue
    r = taskqueue.Runner.__new__(taskqueue.Runner)
    r.bot = None
    r.said = []
    r.forwarded = []

    async def say(job, text):
        r.said.append(text)
    r._say = say

    async def fwd(job):
        r.forwarded.append(job.task_id)
    r._forward_from_relay = fwd

    async def drop(job):
        pass
    r._drop_status = drop
    return r


@pytest.fixture
def fake_db(monkeypatch):
    import db
    import sender

    async def nop(*a, **kw):
        return None

    async def row(*a, **kw):
        return {"bytes_moved": 0}

    async def deliver(*a, **kw):
        return 1

    import acl

    async def relay_ch(uid):
        return -100

    monkeypatch.setattr(acl, "relay_channel_for", relay_ch)
    monkeypatch.setattr(db, "update_task", nop)
    monkeypatch.setattr(db, "bump_usage", nop)
    monkeypatch.setattr(db, "get_task", row)
    monkeypatch.setattr(sender, "deliver", deliver)
    monkeypatch.setattr(sender, "deliver_tweet", deliver)


@pytest.mark.asyncio
async def test_split_result_not_forwarded(fake_db):
    import fetcher
    import taskqueue
    r = _runner()
    job = taskqueue.Job(1, 42, "https://t.me/durov/1", 9, 5, forward_to="@pub")
    await r._finish(job, fetcher.Relayed([1, 2], False, "B", 0,
                                         streamer.SPLIT_NOTE, split=True))
    assert r.forwarded == [], "分组没保住就不转发到公开去向"
    assert job.extra == {"split": True}, "记进 extra，重启后重投递也知道"
    assert "@pub" in r.said[-1] and "没有转发" in r.said[-1]


@pytest.mark.asyncio
async def test_intact_result_forwarded(fake_db):
    import fetcher
    import taskqueue
    r = _runner()
    job = taskqueue.Job(1, 42, "https://t.me/durov/1", 9, 5, forward_to="@pub")
    await r._finish(job, fetcher.Relayed([1, 2], True, "B", 0, ""))
    assert r.forwarded == [1]


@pytest.mark.asyncio
async def test_other_notes_do_not_block_forward(fake_db):
    """投票、按钮之类的说明和分组无关，照常转发。"""
    import fetcher
    import taskqueue
    r = _runner()
    job = taskqueue.Job(1, 42, "https://t.me/durov/1", 9, 5, forward_to="@pub")
    await r._finish(job, fetcher.Relayed([1], False, "A", 0, "投票会被复制成一个全新的投票"))
    assert r.forwarded == [1]


@pytest.mark.asyncio
async def test_redeliver_after_restart_still_no_forward(fake_db):
    import taskqueue
    r = _runner()
    job = taskqueue.Job(1, 42, "https://t.me/durov/1", 9, 5, forward_to="@pub",
                        relay_chat_id=-100, relay_ids=[1, 2], extra={"split": True})
    await r._deliver(job)
    assert r.forwarded == []


@pytest.mark.asyncio
async def test_forward_tweet_skips_when_relay_copy_split(monkeypatch):
    """快通道直发后 fw：Telegram 拉不动、本机中转又没能成组 —— 不转发。"""
    import acl
    import taskqueue
    from tweet import Tweet
    from streamer import WebMedia

    r = _runner()
    sent = []

    async def ext(*a, **kw):
        raise RuntimeError("拉不动")

    async def relay(*a, **kw):
        return [1, 2], 0, streamer.SPLIT_NOTE

    async def really_forward(job, peer, ids):
        sent.append(ids)

    class Pool:
        async def acquire(self, uid):
            return object()

        def using(self, uid):
            class C:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, *a):
                    return False
            return C()

    async def relay_ch(uid):
        return -100

    monkeypatch.setattr(streamer, "send_external_media", ext)
    monkeypatch.setattr(streamer, "relay_web_media", relay)
    monkeypatch.setattr(taskqueue, "POOL", Pool())
    monkeypatch.setattr(acl, "relay_channel_for", relay_ch)
    r._forward = really_forward

    job = taskqueue.Job(1, 42, "x", 9, 5, forward_to="@pub")
    job.tweet = Tweet("1", "J", "j", "", [WebMedia("photo", "https://a"),
                                          WebMedia("video", "https://b")])
    job.extra = {"mode": "media", "html": "<b>J</b>", "caption": True}
    await r._forward_tweet(job)
    assert sent == [] and "没有转发" in r.said[-1]


# ================================================================ URL 直发半途失败

@pytest.mark.asyncio
async def test_external_partial_send_cleaned_up():
    """前一段相册已经发出、后一段被拒：撤掉已发的，回退时整批重做不留半份。"""
    from streamer import WebMedia

    class C:
        def __init__(self):
            self.n = 0
            self.deleted = []

        async def send_file(self, peer, file, **kw):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("拒收")
            if isinstance(file, list):
                return [SimpleNamespace(id=100 + i) for i in range(len(file))]
            return SimpleNamespace(id=200)

        async def delete_messages(self, peer, ids):
            self.deleted.append(list(ids))

    c = C()
    with pytest.raises(RuntimeError):
        await streamer.send_external_media(
            c, [WebMedia("photo", "https://a"), WebMedia("photo", "https://b"),
                WebMedia("gif", "https://g")], -100)
    assert c.deleted == [[100, 101]]
