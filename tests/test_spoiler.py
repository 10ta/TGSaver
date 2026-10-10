"""剧透总开关。

默认去掉：媒体遮罩和文字剧透都不保留。打开后原样保留；nosp 是这一次强制去掉。
处理方式是搬进中转频道之后，用服务端引用重发一遍，零流量 —— nosp 不再重新下载上传。
"""
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
from telethon.tl import functions, types  # noqa: E402

import fetcher  # noqa: E402
import spoiler  # noqa: E402
from parser import parse_link  # noqa: E402

NOW = datetime.now(timezone.utc)
CH = types.PeerChannel(555)


def _photo(mid, cover=False, text="", ents=None, gid=None):
    return types.Message(
        id=mid, peer_id=CH, date=NOW, message=text, entities=ents, grouped_id=gid,
        media=types.MessageMediaPhoto(
            photo=types.Photo(id=mid, access_hash=1, file_reference=b"", date=NOW,
                              sizes=[], dc_id=1), spoiler=cover))


def _video(mid, cover=False, gid=None):
    return types.Message(
        id=mid, peer_id=CH, date=NOW, message="", grouped_id=gid,
        media=types.MessageMediaDocument(
            document=types.Document(id=mid, access_hash=1, file_reference=b"", date=NOW,
                                    mime_type="video/mp4", size=1, dc_id=1,
                                    attributes=[]), spoiler=cover))


def _text(mid, text="hi", ents=None):
    return types.Message(id=mid, peer_id=CH, date=NOW, message=text, entities=ents)


SP = types.MessageEntitySpoiler(offset=0, length=2)
BOLD = types.MessageEntityBold(offset=0, length=2)


class FakeClient:
    """记录请求；中转频道里的消息由 store 决定。"""

    def __init__(self, store, fail=False):
        self.store = {m.id: m for m in store}
        self.requests = []
        self.deleted = []
        self.next = 900
        self.fail = fail

    async def get_messages(self, peer, ids=None):
        return [self.store.get(i) for i in ids]

    def _ups(self, n):
        ups = []
        for _ in range(n):
            self.next += 1
            ups.append(types.UpdateNewChannelMessage(
                message=types.Message(id=self.next, peer_id=CH, date=NOW, message=""),
                pts=1, pts_count=1))
        return types.Updates(updates=ups, users=[], chats=[], date=NOW, seq=0)

    async def __call__(self, req):
        self.requests.append(req)
        if self.fail and len(self.requests) > 1:
            raise RuntimeError("boom")
        if isinstance(req, functions.messages.SendMultiMediaRequest):
            return self._ups(len(req.multi_media))
        if isinstance(req, functions.messages.SendMediaRequest):
            return self._ups(1)
        raise AssertionError(type(req).__name__)

    async def send_message(self, peer, text, formatting_entities=None, link_preview=False):
        self.requests.append(("text", text, formatting_entities))
        self.next += 1
        return types.Message(id=self.next, peer_id=CH, date=NOW, message=text)

    async def forward_messages(self, peer, ids, from_peer, **kw):
        self.requests.append(("fwd", list(ids)))
        out = []
        for _ in ids:
            self.next += 1
            out.append(types.Message(id=self.next, peer_id=CH, date=NOW, message=""))
        return out

    async def delete_messages(self, peer, ids):
        self.deleted.append(list(ids))

    def of(self, cls):
        return [r for r in self.requests if isinstance(r, cls)]


# ================================================================ 判定

def test_involved():
    assert not spoiler.involved([_photo(1), _text(2, ents=[BOLD])])
    assert spoiler.involved([_photo(1, cover=True)])
    assert spoiler.involved([_text(1, ents=[SP])])


def test_wanted():
    src = [_photo(1, cover=True), _photo(2)]
    assert spoiler.wanted(src, 2, keep=False) == [False, False]
    assert spoiler.wanted(src, 2, keep=True) == [True, False]
    assert spoiler.wanted(src, 3, keep=True) == [True] * 3, "条数对不上就全部带上"


# ================================================================ 处理

@pytest.mark.asyncio
async def test_nothing_to_do_sends_nothing():
    c = FakeClient([_photo(1), _video(2)])
    assert await spoiler.apply(c, CH, [1, 2], [False, False], keep_text=False) == [1, 2]
    assert c.requests == [] and c.deleted == []


@pytest.mark.asyncio
async def test_strip_cover_single():
    c = FakeClient([_photo(1, cover=True, text="hi", ents=[BOLD])])
    ids = await spoiler.apply(c, CH, [1], [False], keep_text=False)
    req = c.of(functions.messages.SendMediaRequest)[0]
    assert isinstance(req.media, types.InputMediaPhoto) and req.media.spoiler is False
    assert req.message == "hi" and req.entities == [BOLD], "其余格式保留"
    assert ids == [901] and c.deleted == [[1]]


@pytest.mark.asyncio
async def test_strip_keeps_album_together_and_order():
    c = FakeClient([_photo(1, cover=True, gid=7), _video(2, gid=7), _text(3, "尾巴")])
    ids = await spoiler.apply(c, CH, [1, 2, 3], [False] * 3, keep_text=False)
    multi = c.of(functions.messages.SendMultiMediaRequest)
    assert len(multi) == 1 and len(multi[0].multi_media) == 2, "相册整组重发"
    assert not any(x.media.spoiler for x in multi[0].multi_media)
    assert ids == sorted(ids) and len(ids) == 3, "id 递增，copyMessages 才不出错"
    assert c.deleted == [[1, 2, 3]]


@pytest.mark.asyncio
async def test_strip_text_spoiler_entity():
    c = FakeClient([_text(1, "秘密内容", [SP, BOLD])])
    await spoiler.apply(c, CH, [1], [False], keep_text=False)
    assert c.requests[0] == ("text", "秘密内容", [BOLD])


@pytest.mark.asyncio
async def test_keep_restores_cover_lost_by_reupload():
    """受保护内容重传后遮罩丢了；保留模式下要补回来。"""
    c = FakeClient([_photo(1), _photo(2)])
    await spoiler.apply(c, CH, [1, 2], [True, False], keep_text=True)
    sent = [r for r in c.requests if isinstance(r, functions.messages.SendMediaRequest)]
    assert [r.media.spoiler for r in sent] == [True, False]


@pytest.mark.asyncio
async def test_keep_leaves_text_spoiler():
    c = FakeClient([_text(1, "秘密", [SP])])
    assert await spoiler.apply(c, CH, [1], [False], keep_text=True) == [1]
    assert c.requests == []


@pytest.mark.asyncio
async def test_failure_rolls_back_and_returns_original():
    c = FakeClient([_photo(1, cover=True), _photo(2, cover=True)], fail=True)
    ids = await spoiler.apply(c, CH, [1, 2], [False, False], keep_text=False)
    assert ids == [1, 2]
    assert c.deleted == [[901]], "已重发的那条撤掉，原件不动"


# ================================================================ 接进 relay

class RelayClient(FakeClient):
    """路径 A：forward_messages 进中转频道，中转里的副本保留来源的遮罩。"""

    def __init__(self, src):
        super().__init__([])
        self.src = src

    async def forward_messages(self, peer, ids, from_peer=None, **kw):
        out = []
        for m in self.src:
            self.next += 1
            copy = types.Message(id=self.next, peer_id=CH, date=NOW, message=m.message,
                                 entities=m.entities, media=m.media)
            self.store[copy.id] = copy
            out.append(copy)
        return out


async def _relay(src, link, keep):
    c = RelayClient(src)
    res = await fetcher.relay(c, parse_link(link), -100, entity=object(),
                              msg=src[0], keep_spoiler=keep)
    return c, res


@pytest.mark.asyncio
async def test_relay_strips_by_default():
    c, res = await _relay([_photo(10, cover=True)], "https://t.me/durov/10", keep=False)
    req = c.of(functions.messages.SendMediaRequest)[0]
    assert req.media.spoiler is False and res.message_ids == [902]


@pytest.mark.asyncio
async def test_relay_keeps_when_enabled():
    c, res = await _relay([_photo(10, cover=True)], "https://t.me/durov/10", keep=True)
    assert not c.of(functions.messages.SendMediaRequest), "直转已经带着遮罩，不用动"
    assert res.message_ids == [901]


@pytest.mark.asyncio
async def test_relay_nosp_overrides_keep():
    c, res = await _relay([_photo(10, cover=True)], "https://t.me/durov/10?nosp", keep=True)
    assert c.of(functions.messages.SendMediaRequest)[0].media.spoiler is False


@pytest.mark.asyncio
async def test_relay_no_spoiler_no_extra_requests():
    c, res = await _relay([_photo(10)], "https://t.me/durov/10", keep=False)
    assert c.requests == [] and res.path == "A"


@pytest.mark.asyncio
async def test_nosp_no_longer_forces_slow_lane(monkeypatch):
    """nosp 以前要重新下载上传，现在零流量：探测时不再当成「重活」。"""
    async def fake_locate(client, ref):
        return object(), _photo(10, cover=True)
    monkeypatch.setattr(fetcher, "locate", fake_locate)
    _, _, heavy = await fetcher.probe(None, parse_link("https://t.me/durov/10?nosp"))
    assert heavy is False
