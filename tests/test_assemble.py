"""抓取组装：把中转频道里的多条消息按类型重新组成相册。"""
import os
import sys
from datetime import datetime, timezone
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
from telethon.tl import types as T  # noqa: E402
from telethon.tl.functions.messages import SendMultiMediaRequest  # noqa: E402

import assemble  # noqa: E402

NOW = datetime.now(timezone.utc)


def _photo_msg(mid, cap="", spoiler=False):
    return T.Message(id=mid, peer_id=T.PeerChannel(1), date=NOW, message=cap,
                     media=T.MessageMediaPhoto(photo=T.Photo(
                         id=mid, access_hash=1, file_reference=b"x", date=NOW,
                         sizes=[], dc_id=1), spoiler=spoiler))


def _doc_msg(mid, attrs, cap="", mime="application/octet-stream"):
    return T.Message(id=mid, peer_id=T.PeerChannel(1), date=NOW, message=cap,
                     media=T.MessageMediaDocument(document=T.Document(
                         id=mid, access_hash=1, file_reference=b"x", date=NOW,
                         mime_type=mime, size=1, dc_id=1, attributes=attrs)))


def video(mid, cap=""):
    return _doc_msg(mid, [T.DocumentAttributeVideo(duration=1, w=1, h=1)], cap, "video/mp4")


def round_video(mid):
    return _doc_msg(mid, [T.DocumentAttributeVideo(duration=1, w=1, h=1, round_message=True)])


def gif(mid):
    return _doc_msg(mid, [T.DocumentAttributeAnimated(),
                          T.DocumentAttributeVideo(duration=1, w=1, h=1)])


def sticker(mid):
    return _doc_msg(mid, [T.DocumentAttributeSticker(alt="x", stickerset=T.InputStickerSetEmpty())])


def audio(mid):
    return _doc_msg(mid, [T.DocumentAttributeAudio(duration=1)])


def voice(mid):
    return _doc_msg(mid, [T.DocumentAttributeAudio(duration=1, voice=True)])


def file(mid):
    return _doc_msg(mid, [T.DocumentAttributeFilename(file_name="a.zip")])


# ================================================================ 分类与分组

@pytest.mark.parametrize("msg,cat", [
    (_photo_msg(1), "visual"), (video(1), "visual"),
    (file(1), "file"), (audio(1), "audio"),
    (gif(1), None), (sticker(1), None), (voice(1), None), (round_video(1), None),
    (T.Message(id=1, peer_id=T.PeerChannel(1), date=NOW, message="hi"), None),
])
def test_category(msg, cat):
    assert assemble.category(msg) == cat


def _ids(blocks):
    return [[m.id for m in b] for b in blocks]


def test_photos_and_videos_share_an_album():
    assert _ids(assemble.plan_blocks([_photo_msg(1), video(2), _photo_msg(3)])) == [[1, 2, 3]]


def test_types_grouped_by_first_appearance():
    """照片、文件、照片 -> [照片, 照片], [文件]：尽量少拆，同类内保持顺序。"""
    blocks = assemble.plan_blocks([_photo_msg(1), file(2), _photo_msg(3), file(4)])
    assert _ids(blocks) == [[1, 3], [2, 4]]


def test_unalbumable_items_stay_single():
    blocks = assemble.plan_blocks([_photo_msg(1), sticker(2), _photo_msg(3), gif(4)])
    assert _ids(blocks) == [[1, 3], [2], [4]]


def test_split_at_ten():
    blocks = assemble.plan_blocks([_photo_msg(i) for i in range(1, 13)])
    assert [len(b) for b in blocks] == [10, 2]


# ================================================================ 组装

class FakeClient:
    def __init__(self, msgs, fail_multi=False):
        self.msgs = {m.id: m for m in msgs}
        self.fail_multi = fail_multi
        self.multi = []
        self.forwarded = []
        self.deleted = []
        self._next = 1000

    async def get_messages(self, peer, ids):
        return [self.msgs.get(i) for i in ids]

    def _new(self):
        self._next += 1
        return self._next

    async def __call__(self, req):
        assert isinstance(req, SendMultiMediaRequest)
        if self.fail_multi:
            raise RuntimeError("MEDIA_INVALID")
        self.multi.append(req.multi_media)
        ups = []
        for _ in req.multi_media:
            ups.append(T.UpdateNewChannelMessage(
                message=T.Message(id=self._new(), peer_id=T.PeerChannel(1),
                                  date=NOW, message=""), pts=1, pts_count=1))
        return T.Updates(updates=ups, users=[], chats=[], date=NOW, seq=0)

    async def forward_messages(self, entity, ids, from_peer, drop_author=False):
        assert drop_author is True
        self.forwarded.append(list(ids))
        return [type("M", (), {"id": self._new()})() for _ in ids]

    async def delete_messages(self, peer, ids):
        self.deleted.extend(ids)


@pytest.mark.asyncio
async def test_compose_merges_into_one_album():
    c = FakeClient([_photo_msg(1, "一"), video(2, "二"), _photo_msg(3)])
    final, note = await assemble.compose(c, -100, [1, 2, 3])
    assert len(c.multi) == 1 and len(c.multi[0]) == 3
    assert [s.message for s in c.multi[0]] == ["一", "二", ""], "各自的说明留在各自那一项上"
    assert note == ""
    assert sorted(c.deleted) == [1, 2, 3], "中间副本要清掉"


@pytest.mark.asyncio
async def test_compose_ids_strictly_increasing():
    """copyMessages 要求 id 严格递增，否则整批复制失败、相册分组全丢。"""
    c = FakeClient([_photo_msg(1), sticker(2), _photo_msg(3), file(4), file(5)])
    final, _ = await assemble.compose(c, -100, [1, 2, 3, 4, 5])
    assert final == sorted(final) and len(set(final)) == len(final)
    assert all(i > 5 for i in final), "单条也要重发，否则旧 id 会打乱顺序"


@pytest.mark.asyncio
async def test_compose_keeps_spoiler():
    """组装不改变剧透标记：nosp 和受保护内容在搬运时已经去掉了，这里原样保留。"""
    c = FakeClient([_photo_msg(1, spoiler=True), _photo_msg(2)])
    await assemble.compose(c, -100, [1, 2])
    assert [s.media.spoiler for s in c.multi[0]] == [True, False]


@pytest.mark.asyncio
async def test_compose_nothing_to_merge_reuses_originals():
    """全是贴纸这类进不了相册的，原样用，不重发也不删。"""
    c = FakeClient([sticker(1), gif(2)])
    final, _ = await assemble.compose(c, -100, [1, 2])
    assert final == [1, 2] and not c.multi and not c.forwarded and not c.deleted


@pytest.mark.asyncio
async def test_compose_falls_back_to_singles():
    c = FakeClient([_photo_msg(1), _photo_msg(2)], fail_multi=True)
    final, note = await assemble.compose(c, -100, [1, 2])
    assert c.forwarded == [[1, 2]] and len(final) == 2
    assert "未能组成相册" in note


@pytest.mark.asyncio
async def test_compose_skips_missing():
    c = FakeClient([_photo_msg(1), _photo_msg(3)])
    final, _ = await assemble.compose(c, -100, [1, 2, 3])
    assert len(c.multi[0]) == 2


@pytest.mark.asyncio
async def test_compose_request_is_valid_tl():
    """用真实的 TL 类构造请求 —— 上次的教训，参数写错要在这里就炸。"""
    c = FakeClient([_photo_msg(1, "a"), video(2, "b")])
    await assemble.compose(c, -100, [1, 2])
    for s in c.multi[0]:
        assert isinstance(s, T.InputSingleMedia)
        assert isinstance(s.media, (T.InputMediaPhoto, T.InputMediaDocument))


# ================================================================ 调度

import json  # noqa: E402

import db  # noqa: E402


@pytest.fixture
async def qdb(tmp_path):
    from config import CFG
    object.__setattr__(CFG, "db_path", tmp_path / "q.db")
    await db.init()
    yield db
    await db.close()


class Bot:
    def __init__(self):
        self.calls = []

    async def copy_messages(self, chat_id, from_chat_id, message_ids, **kw):
        self.calls.append(("copies", list(message_ids)))
        return list(message_ids)

    async def copy_message(self, chat_id, from_chat_id, message_id, **kw):
        self.calls.append(("copy", message_id))

    async def send_message(self, *a, **kw):
        self.calls.append(("text",))


def _runner():
    import asyncio
    import taskqueue
    r = taskqueue.Runner.__new__(taskqueue.Runner)
    r.bot = Bot()
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


def _setup(monkeypatch, relay_results, compose_result=([901, 902], "")):
    """relay_results: 链接 -> message_ids 或异常。"""
    import fetcher
    import taskqueue
    relayed, composed = [], []

    async def fake_relay(client, ref, relay_ch, **kw):
        key = f"tgsaver://p/{ref.direct_peer}/{ref.msg_id}"
        relayed.append(key)
        r = relay_results[key]
        if isinstance(r, Exception):
            raise r
        return fetcher.Relayed(r, len(r) > 1, "A", 0)
    monkeypatch.setattr(fetcher, "relay", fake_relay)

    async def fake_compose(client, peer, ids):
        composed.append(list(ids))
        return compose_result
    monkeypatch.setattr(assemble, "compose", fake_compose)

    class C:
        async def forward_messages(self, *a, **kw):
            C.fw = (a, kw)
            return [object()]
        async def get_entity(self, x):
            return type("E", (), {"broadcast": True})()
        async def get_permissions(self, *a):
            return None

    async def acquire(uid):
        return C()
    monkeypatch.setattr(taskqueue.POOL, "acquire", acquire)
    return relayed, composed, C


L1, L2, L3 = "tgsaver://p/111/5", "tgsaver://p/111/9", "tgsaver://p/222/3"


@pytest.mark.asyncio
async def test_batch_relays_composes_and_delivers(qdb, monkeypatch):
    import taskqueue
    relayed, composed, _ = _setup(monkeypatch, {L1: [11], L2: [12, 13], L3: [14]})
    r = _runner()
    link = f"{L1} {L2} {L3}"
    tid = await qdb.add_task(42, link, 9, 5)
    await r._run(taskqueue.Job(tid, 42, link, 9, 5), "slow")

    assert relayed == [L1, L2, L3]
    assert composed == [[11, 12, 13, 14]], "按写的顺序交给组装"
    assert r.bot.calls == [("copies", [901, 902])], "组装结果一次复制给用户"
    assert (await qdb.get_task(tid))["state"] == "done"


@pytest.mark.asyncio
async def test_batch_skips_missing_and_notes(qdb, monkeypatch):
    import fetcher
    import taskqueue
    _setup(monkeypatch, {L1: [11], L2: fetcher.FetchError("已删除"), L3: [14]})
    r = _runner()
    link = f"{L1} {L2} {L3}"
    tid = await qdb.add_task(42, link, 9, 5)
    await r._run(taskqueue.Job(tid, 42, link, 9, 5), "slow")
    assert any("其中 1 条取不到" in x for x in r.said)
    assert (await qdb.get_task(tid))["state"] == "done"


@pytest.mark.asyncio
async def test_batch_retry_does_not_relay_again(qdb, monkeypatch):
    """之前搬好的（可能是受保护的大文件）重试时绝不能重传。"""
    import taskqueue
    relayed, composed, _ = _setup(monkeypatch, {L1: [11], L2: [12], L3: [14]})
    r = _runner()
    link = f"{L1} {L2} {L3}"
    tid = await qdb.add_task(42, link, 9, 5)
    job = taskqueue.Job(tid, 42, link, 9, 5)
    job.extra = {"kind": "grab_batch", "done": {L1: [11], L2: [12]}, "bytes": 0}
    await r._run(job, "slow")
    assert relayed == [L3], "只搬还没搬的"
    assert composed == [[11, 12, 14]]


@pytest.mark.asyncio
async def test_batch_progress_persisted_each_step(qdb, monkeypatch):
    import taskqueue
    _setup(monkeypatch, {L1: [11], L2: RuntimeError("网络断了"), L3: [14]})
    r = _runner()
    link = f"{L1} {L2} {L3}"
    tid = await qdb.add_task(42, link, 9, 5)
    await r._run(taskqueue.Job(tid, 42, link, 9, 5), "slow")
    saved = json.loads((await qdb.get_task(tid))["extra"])
    assert saved["done"] == {L1: [11]}, "搬好的那条已落库，重试时可跳过"


@pytest.mark.asyncio
async def test_batch_all_missing_fails(qdb, monkeypatch):
    import fetcher
    import taskqueue
    _setup(monkeypatch, {L1: fetcher.FetchError("x"), L2: fetcher.FetchError("x")})
    r = _runner()
    link = f"{L1} {L2}"
    tid = await qdb.add_task(42, link, 9, 5)
    await r._run(taskqueue.Job(tid, 42, link, 9, 5), "slow")
    row = await qdb.get_task(tid)
    assert row["state"] == "failed" and "全部取不到" in row["error"]


@pytest.mark.asyncio
async def test_batch_with_fw_delivers_then_forwards(qdb, monkeypatch):
    import taskqueue
    _, _, C = _setup(monkeypatch, {L1: [11], L2: [12]})
    r = _runner()
    link = f"{L1} {L2}"
    tid = await qdb.add_task(42, link, 9, 5, forward_to="@optv4")
    await r._run(taskqueue.Job(tid, 42, link, 9, 5, forward_to="@optv4"), "slow")
    assert r.bot.calls == [("copies", [901, 902])], "本人那份照常"
    args, kw = C.fw
    assert list(args[1]) == [901, 902] and kw["drop_author"] is True
