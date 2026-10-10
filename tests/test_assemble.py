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

    async def send_message(self, peer, text, formatting_entities=None, **kw):
        self.texts = getattr(self, "texts", []) + [text]
        return type("M", (), {"id": self._new()})()


def U(*ids, key="111", name="YM 闪闪", tag="#ym_ss_bot"):
    """一个来源单元：来源里的一条消息或一个相册。"""
    return assemble.Unit(key=key, name=name, tag=tag, ids=list(ids))


@pytest.mark.asyncio
async def test_compose_merges_into_one_album():
    c = FakeClient([_photo_msg(1, "一"), video(2, "二"), _photo_msg(3)])
    final, note = await assemble.compose(c, -100, [U(1), U(2), U(3)])
    assert len(c.multi) == 1 and len(c.multi[0]) == 3
    first = c.multi[0][0].message
    assert first == "YM 闪闪 :\n1 : 一\n2 : 二\n1-3 · #ym_ss_bot"
    assert [x.message for x in c.multi[0][1:]] == ["", ""], "其余项不再带说明"
    assert note == ""
    assert sorted(c.deleted) == [1, 2, 3], "中间副本要清掉"


@pytest.mark.asyncio
async def test_compose_ids_strictly_increasing():
    """copyMessages 要求 id 严格递增，否则整批复制失败、相册分组全丢。"""
    c = FakeClient([_photo_msg(1), sticker(2), _photo_msg(3), file(4), file(5)])
    final, _ = await assemble.compose(c, -100, [U(1), U(2), U(3), U(4), U(5)])
    assert final == sorted(final) and len(set(final)) == len(final)
    assert all(i > 5 for i in final), "单条也要重发，否则旧 id 会打乱顺序"


@pytest.mark.asyncio
async def test_compose_keeps_spoiler():
    """组装不改变剧透标记：nosp 和受保护内容在搬运时已经去掉了，这里原样保留。"""
    c = FakeClient([_photo_msg(1, spoiler=True), _photo_msg(2)])
    await assemble.compose(c, -100, [U(1), U(2)])
    assert [s.media.spoiler for s in c.multi[0]] == [True, False]


@pytest.mark.asyncio
async def test_compose_nothing_to_merge_reuses_originals():
    """全是贴纸这类进不了相册的，原样用，不重发也不删。"""
    c = FakeClient([sticker(1), gif(2)])
    final, _ = await assemble.compose(c, -100, [U(1), U(2)])
    assert final == [1, 2] and not c.multi and not c.forwarded and not c.deleted


@pytest.mark.asyncio
async def test_compose_falls_back_to_singles():
    c = FakeClient([_photo_msg(1), _photo_msg(2)], fail_multi=True)
    final, note = await assemble.compose(c, -100, [U(1), U(2)])
    assert c.forwarded == [[1, 2]] and len(final) == 2
    assert "未能组成相册" in note


@pytest.mark.asyncio
async def test_compose_skips_missing():
    c = FakeClient([_photo_msg(1), _photo_msg(3)])
    final, _ = await assemble.compose(c, -100, [U(1), U(2), U(3)])
    assert len(c.multi[0]) == 2


@pytest.mark.asyncio
async def test_compose_request_is_valid_tl():
    """用真实的 TL 类构造请求 —— 上次的教训，参数写错要在这里就炸。"""
    c = FakeClient([_photo_msg(1, "a"), video(2, "b")])
    await assemble.compose(c, -100, [U(1), U(2)])
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

    async def fake_compose(client, peer, units, **kw):
        composed.append([i for u in units for i in u.ids])
        return compose_result
    monkeypatch.setattr(assemble, "compose", fake_compose)

    class C:
        async def forward_messages(self, *a, **kw):
            C.fw = (a, kw)
            return [object()]
        async def get_entity(self, x):
            return type("E", (), {"broadcast": True, "first_name": "YM 闪闪",
                                  "username": "ym_ss_bot"})()
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


# ================================================================ 汇总说明文字

def _plain(h):
    from telethon.extensions import html as tl_html
    return tl_html.parse(h)[0]


@pytest.fixture(autouse=True)
def _no_sign(monkeypatch):
    import tweet
    monkeypatch.setattr(tweet, "SIGN_TEXT", "")
    monkeypatch.setattr(tweet, "SIGN_URL", "")


def test_caption_same_source_one_quote():
    """同一来源合并成一个引用块，逐条列出。"""
    h, fits = assemble.build_caption(
        [("YM 闪闪", "#ym_ss_bot", 1, 4, [("1", "第一条"), ("2-4", "相册那条")])], 1024)
    assert fits
    assert _plain(h) == "YM 闪闪 :\n1 : 第一条\n2-4 : 相册那条\n1-4 · #ym_ss_bot"
    assert h.count("<blockquote>") == 1


def test_caption_multiple_sources():
    h, _ = assemble.build_caption([
        ("A", "#a_bot", 1, 2, [("1", "x"), ("2", "y")]),
        ("B", "#b_bot", 3, 3, [("3", "z")]),
    ], 1024)
    assert _plain(h) == "A :\n1 : x\n2 : y\nB :\n3 : z\n1-2 · #a_bot\n3 · #b_bot"
    assert h.count("<blockquote>") == 2


def test_caption_skips_items_without_text():
    """没有文字的那条不单独占一行，序号照样占位。"""
    h, _ = assemble.build_caption(
        [("A", "#a_bot", 1, 3, [("1", "有字"), ("2", ""), ("3", "也有")])], 1024)
    assert _plain(h) == "A :\n1 : 有字\n3 : 也有\n1-3 · #a_bot"


def test_caption_no_tag_without_username():
    h, _ = assemble.build_caption([("某人", "", 1, 1, [("1", "x")])], 1024)
    assert _plain(h) == "某人 :\n1 : x"


def test_caption_signature(monkeypatch):
    import tweet
    monkeypatch.setattr(tweet, "SIGN_TEXT", "@欧派TV")
    monkeypatch.setattr(tweet, "SIGN_URL", "https://t.me/optv4")
    h, _ = assemble.build_caption([("A", "#a_bot", 1, 1, [("1", "x")])], 1024)
    assert _plain(h).endswith("1 · #a_bot\n\nvia @欧派TV")
    assert 'href="https://t.me/optv4"' in h


def test_caption_escapes():
    h, _ = assemble.build_caption([("<b>A", "#a", 1, 1, [("1", "1 < 2 & <x>")])], 1024)
    assert "<x>" not in h and "&lt;x&gt;" in h and "&lt;b&gt;A" in h


def test_caption_fair_truncation_within_limit():
    import tweet
    h, fits = assemble.build_caption([("A", "#a", 1, 2, [
        ("1", "短句"), ("2", "字" * 3000)])], 1024)
    text = _plain(h)
    assert fits and tweet.utf16_len(text) <= 1024
    assert "1 : 短句\n" in text, "短的不该被削"
    assert "…" in text


def test_caption_fixed_part_too_long():
    many = [(f"来源{i}" * 5, f"#source_{i}", i, i, [(str(i), "x")]) for i in range(60)]
    _, fits = assemble.build_caption(many, 1024)
    assert not fits


# ---------------------------------------------------------------- 组装里的说明

@pytest.mark.asyncio
async def test_compose_caption_only_on_first_item():
    c = FakeClient([_photo_msg(1, "一"), _photo_msg(2, "二")])
    await assemble.compose(c, -100, [U(1), U(2)])
    msgs = [x.message for x in c.multi[0]]
    assert "1 : 一" in msgs[0] and "2 : 二" in msgs[0]
    assert msgs[1] == ""
    assert any(isinstance(e, T.MessageEntityBlockquote) for e in c.multi[0][0].entities)


@pytest.mark.asyncio
async def test_compose_source_album_counts_as_one_entry():
    """来源里的一个 3 图相册是一条，序号写成区间，文字只列一次。"""
    c = FakeClient([_photo_msg(1, "单图"), _photo_msg(2, "相册说明"),
                    _photo_msg(3), _photo_msg(4)])
    await assemble.compose(c, -100, [U(1), U(2, 3, 4)])
    assert c.multi[0][0].message == \
        "YM 闪闪 :\n1 : 单图\n2-4 : 相册说明\n1-4 · #ym_ss_bot"


@pytest.mark.asyncio
async def test_compose_groups_by_source():
    c = FakeClient([_photo_msg(1, "a1"), _photo_msg(2, "a2"), _photo_msg(3, "b1")])
    await assemble.compose(c, -100, [
        U(1, key="a", name="A", tag="#a_bot"), U(2, key="a", name="A", tag="#a_bot"),
        U(3, key="b", name="B", tag="#b_bot")])
    assert c.multi[0][0].message == \
        "A :\n1 : a1\n2 : a2\nB :\n3 : b1\n1-2 · #a_bot\n3 · #b_bot"


@pytest.mark.asyncio
async def test_compose_numbers_restart_per_album():
    """按类型拆成几个相册时，各自汇总、序号各自从 1 开始。"""
    c = FakeClient([_photo_msg(1, "图"), file(2), file(3), _photo_msg(4, "图2")])
    for i, cap in ((2, "文件甲"), (3, "文件乙")):
        c.msgs[i].message = cap
    await assemble.compose(c, -100, [U(1), U(2), U(3), U(4)])
    assert len(c.multi) == 2
    assert c.multi[0][0].message.startswith("YM 闪闪 :\n1 : 图\n2 : 图2")
    assert c.multi[1][0].message.startswith("YM 闪闪 :\n1 : 文件甲\n2 : 文件乙")


@pytest.mark.asyncio
async def test_compose_single_keeps_its_own_caption():
    """贴纸、动图这类单独发的，原样转发，带的是它自己的说明。"""
    c = FakeClient([_photo_msg(1, "a"), _photo_msg(2, "b"), gif(3)])
    await assemble.compose(c, -100, [U(1), U(2), U(3)])
    assert c.forwarded == [[3]], "单条走原样转发，保留原说明"


@pytest.mark.asyncio
async def test_compose_long_caption_sent_separately(monkeypatch):
    """固定部分本身超 1024：相册不带说明，汇总另发一条，且 id 仍然递增。"""
    import tweet
    msgs = [_photo_msg(i, "x") for i in range(1, 11)]
    c = FakeClient(msgs)
    units = [U(i, key=str(i), name=f"很长的来源名称第{i}号" * 12, tag=f"#source_number_{i}")
             for i in range(1, 11)]
    h, fits = assemble.build_caption(
        [(u.name, u.tag, i, i, [(str(i), "x")]) for i, u in enumerate(units, 1)], 1024)
    assert not fits, "测试数据要真的超长"

    final, _ = await assemble.compose(c, -100, units)
    assert all(x.message == "" for x in c.multi[0])
    assert len(c.texts) == 1 and "很长的来源名称第1号" in c.texts[0]
    assert tweet.utf16_len(c.texts[0]) <= 4096
    assert final == sorted(final) and len(final) == 11


# ---------------------------------------------------------------- 来源信息

@pytest.mark.asyncio
async def test_grab_units_names_and_tags():
    import taskqueue

    class C:
        async def get_entity(self, peer):
            if peer == 111:
                return T.User(id=111, first_name="YM", last_name="闪闪",
                              username="ym_ss_bot", bot=True)
            raise ValueError("不认识")

    links = ["tgsaver://p/111/5", "tgsaver://p/111/9", "tgsaver://p/222/3",
             "tgsaver://p/333/1"]
    done = {links[0]: [11], links[1]: [12, 13], links[2]: [14], links[3]: []}
    units = await taskqueue.Runner._grab_units(C(), links, done)
    assert [(u.key, u.name, u.tag, u.ids) for u in units] == [
        ("111", "YM 闪闪", "#ym_ss_bot", [11]),
        ("111", "YM 闪闪", "#ym_ss_bot", [12, 13]),
        ("222", "222", "", [14]),      # 查不到：用 id 当名称，不带标签
    ], "取不到的那条不进组装"


# ================================================================ 多个 t.me 链接组装

@pytest.mark.asyncio
async def test_multiple_tme_links_routed_to_batch(qdb):
    r = _runner()
    await r.submit(42, "https://t.me/c/2703619907/124/4638 "
                       "https://t.me/c/2703619907/124/4637", 9, 5)
    assert r.slow.qsize() == 1 and r.fast.empty()


@pytest.mark.asyncio
async def test_single_tme_link_unchanged(qdb):
    r = _runner()
    await r.submit(42, "https://t.me/c/2703619907/124/4638", 9, 5)
    assert r.fast.qsize() == 1 and r.slow.empty()


@pytest.mark.asyncio
async def test_tme_batch_relays_each_link(qdb, monkeypatch):
    import taskqueue
    A = "https://t.me/c/2703619907/124/4638"
    B = "https://t.me/c/2703619907/124/4637"

    import fetcher
    relayed = []

    async def fake_relay(client, ref, relay_ch, **kw):
        relayed.append(ref.msg_id)
        return fetcher.Relayed([100 + ref.msg_id % 10], False, "A", 0)
    monkeypatch.setattr(fetcher, "relay", fake_relay)

    async def fake_resolve(client, ref):
        return T.Channel(id=2703619907, title="某群", photo=T.ChatPhotoEmpty(),
                         date=NOW, megagroup=True)
    monkeypatch.setattr(fetcher, "resolve_entity", fake_resolve)

    got_units = []

    async def fake_compose(client, peer, units, **kw):
        got_units.extend(units)
        return [901, 902], ""
    monkeypatch.setattr(assemble, "compose", fake_compose)

    async def acquire(uid):
        return object()
    monkeypatch.setattr(taskqueue.POOL, "acquire", acquire)

    r = _runner()
    link = f"{A} {B}"
    tid = await qdb.add_task(42, link, 9, 5)
    await r._run(taskqueue.Job(tid, 42, link, 9, 5), "slow")

    assert relayed == [4637, 4638], "同一对话里从旧到新"
    assert [(u.key, u.name, u.tag) for u in got_units] == [
        ("-1002703619907", "某群", "")] * 2, "私有群没有用户名，就不带标签"
    assert r.bot.calls == [("copies", [901, 902])]


@pytest.mark.asyncio
async def test_topic_link_hint():
    """把话题链接当消息链接发，提示正确写法而不是只说「系统消息」。"""
    import fetcher
    from parser import parse_link

    class C:
        async def get_messages(self, entity, ids=None):
            return T.MessageService(id=124, peer_id=T.PeerChannel(1), date=None,
                                    action=T.MessageActionTopicCreate(title="t", icon_color=0))

    ref = parse_link("https://t.me/c/2703619907/124")
    with pytest.raises(fetcher.FetchError) as ei:
        await fetcher.load_message(C(), object(), ref)
    msg = str(ei.value)
    assert "论坛话题" in msg and "https://t.me/c/2703619907/124 1-5" in msg


@pytest.mark.asyncio
async def test_other_service_message_unchanged():
    import fetcher
    from parser import parse_link

    class C:
        async def get_messages(self, entity, ids=None):
            return T.MessageService(id=5, peer_id=T.PeerChannel(1), date=None,
                                    action=T.MessageActionPinMessage())

    with pytest.raises(fetcher.FetchError, match="系统消息"):
        await fetcher.load_message(C(), object(), parse_link("https://t.me/c/2703619907/5"))
