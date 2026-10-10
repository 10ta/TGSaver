"""收集箱：转发来的媒体攒着，/pack 组装成相册发回。

全程只有 bot：用 file_id 重发是服务端引用，零流量。这里用假 bot 记录调用，
验证分组、说明汇总、剧透开关、来源名开关、投递分块、删除原消息、/pack 的流程。
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
from aiogram.types import (  # noqa: E402
    Animation, Audio, Chat, Document, InputMediaDocument, InputMediaPhoto,
    InputMediaVideo, Message, MessageEntity, MessageOriginChannel,
    MessageOriginHiddenUser, MessageOriginUser, PhotoSize, Sticker, User, Video,
)

import db  # noqa: E402
import pack  # noqa: E402
import tweet  # noqa: E402
from pack import Item  # noqa: E402

NOW = datetime.now(timezone.utc)
ME = Chat(id=42, type="private")
CHAN = Chat(id=-1009, type="channel", title="某频道", username="some_chan")


def _msg(mid, kind="photo", caption=None, group=None, origin=None,
         spoiler=False, entities=None):
    kw = {}
    if kind == "photo":
        kw["photo"] = [PhotoSize(file_id=f"small{mid}", file_unique_id="s", width=1, height=1),
                       PhotoSize(file_id=f"p{mid}", file_unique_id="b", width=9, height=9)]
    elif kind == "video":
        kw["video"] = Video(file_id=f"v{mid}", file_unique_id="u", width=1, height=1, duration=1)
    elif kind == "animation":
        kw["animation"] = Animation(file_id=f"g{mid}", file_unique_id="u", width=1,
                                    height=1, duration=1)
        kw["document"] = Document(file_id=f"g{mid}", file_unique_id="u")
    elif kind == "document":
        kw["document"] = Document(file_id=f"d{mid}", file_unique_id="u")
    elif kind == "audio":
        kw["audio"] = Audio(file_id=f"a{mid}", file_unique_id="u", duration=1)
    elif kind == "sticker":
        kw["sticker"] = Sticker(file_id=f"s{mid}", file_unique_id="u", type="regular",
                                width=1, height=1, is_animated=False, is_video=False)
    return Message(message_id=mid, date=NOW, chat=ME, caption=caption,
                   caption_entities=entities, media_group_id=group,
                   forward_origin=origin, has_media_spoiler=spoiler or None, **kw)


def _chan_origin():
    return MessageOriginChannel(type="channel", date=NOW, chat=CHAN, message_id=1)


class FakeBot:
    def __init__(self, fail_group=False):
        self.calls = []
        self.next = 1000
        self.fail_group = fail_group

    def _id(self):
        self.next += 1
        return SimpleNamespace(message_id=self.next)

    async def send_media_group(self, chat, media, **kw):
        if self.fail_group:
            raise RuntimeError("MEDIA_INVALID")
        self.calls.append(("group", chat, media))
        return [self._id() for _ in media]

    def __getattr__(self, name):
        if not name.startswith("send_"):
            raise AttributeError(name)

        async def send(chat, *a, **kw):
            self.calls.append((name[5:], chat, a, kw))
            return self._id()
        return send

    async def copy_messages(self, chat_id, from_chat_id, message_ids, **kw):
        self.calls.append(("copies", chat_id, list(message_ids)))
        return list(message_ids)

    async def delete_messages(self, chat, ids):
        self.calls.append(("delete", chat, list(ids)))
        return True

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


@pytest.fixture(autouse=True)
def _no_signature(monkeypatch):
    monkeypatch.setattr(tweet, "SIGN_TEXT", "")
    monkeypatch.setattr(tweet, "SIGN_URL", "")
    pack._inbox.clear()
    pack._warned.clear()


# ================================================================ 认出媒体

@pytest.mark.parametrize("kind,file_id", [
    ("photo", "p1"),            # 照片取最大的那个尺寸
    ("video", "v1"),
    ("animation", "g1"),        # 动图同时带 document，必须认成动图
    ("document", "d1"),
    ("audio", "a1"),
    ("sticker", "s1"),
])
def test_from_message_kinds(kind, file_id):
    it = pack.from_message(_msg(1, kind))
    assert (it.kind, it.file_id) == (kind, file_id)


def test_from_message_text_only_is_none():
    assert pack.from_message(Message(message_id=1, date=NOW, chat=ME, text="hi")) is None


def test_origin_channel_user_hidden_self():
    assert pack.origin_of(_msg(1, origin=_chan_origin())) == \
        ("c-1009", "某频道", "#some_chan")
    u = User(id=7, is_bot=False, first_name="小", last_name="明", username="xm")
    assert pack.origin_of(_msg(1, origin=MessageOriginUser(
        type="user", date=NOW, sender_user=u))) == ("u7", "小 明", "#xm")
    assert pack.origin_of(_msg(1, origin=MessageOriginHiddenUser(
        type="hidden_user", date=NOW, sender_user_name="匿名"))) == ("h:匿名", "匿名", "")
    assert pack.origin_of(_msg(1)) == ("self", "", "")


def test_spoiler_and_caption_captured():
    ents = [MessageEntity(type="spoiler", offset=0, length=2)]
    it = pack.from_message(_msg(3, caption="秘密", spoiler=True, entities=ents, group="g"))
    assert it.spoiler and it.caption == "秘密" and it.group == "g"
    assert it.entities[0].type == "spoiler"


# ================================================================ 收集箱

def test_take_only_before_command_and_sorted():
    for mid in (5, 3, 9, 4):
        pack.add(42, Item(mid, "photo", f"p{mid}"))
    got = pack.take(42, before=6)
    assert [x.msg_id for x in got] == [3, 4, 5], "从旧到新"
    assert [x.msg_id for x in pack.take(42)] == [9], "命令之后才到的留给下一次"
    assert pack.count(42) == 0


def test_users_have_separate_boxes():
    pack.add(42, Item(1, "photo", "a"))
    pack.add(77, Item(2, "photo", "b"))
    assert pack.clear(42) == 1 and pack.count(77) == 1


def test_inbox_limit_and_single_warning(monkeypatch):
    monkeypatch.setattr(pack, "INBOX_MAX", 2)
    assert pack.add(42, Item(1, "photo", "a")) and pack.add(42, Item(2, "photo", "b"))
    assert not pack.add(42, Item(3, "photo", "c"))
    assert pack.first_overflow(42) and not pack.first_overflow(42), "满了只提醒一次"
    pack.clear(42)
    assert pack.first_overflow(42), "清空后重新计"


def test_restore_after_failure():
    pack.add(42, Item(9, "photo", "late"))
    got = pack.take(42, before=5)
    pack.restore(42, [Item(1, "photo", "a"), Item(2, "photo", "b")])
    assert got == [] and [x.msg_id for x in pack.take(42)] == [1, 2, 9]


# ================================================================ 单元与分组

def test_units_merge_original_albums():
    items = [Item(1, "photo", "a", "第一组", group="g1"),
             Item(2, "photo", "b", group="g1"),
             Item(3, "photo", "c", "单张"),
             Item(4, "video", "d", group="g2"),
             Item(5, "video", "e", "第二组", group="g2")]
    units, owner, texts = pack.units_of(items)
    assert owner == [0, 0, 1, 2, 2]
    assert texts == ["第一组", "单张", "第二组"]


@pytest.mark.asyncio
async def test_twelve_photos_two_albums():
    bot = FakeBot()
    items = [Item(i, "photo", f"p{i}") for i in range(1, 13)]
    packed = await pack.build(bot, -100, items)
    assert [len(c[2]) for c in bot.of("group")] == [10, 2]
    assert [len(b) for b in packed.blocks] == [10, 2]
    assert packed.note == ""


@pytest.mark.asyncio
async def test_mixed_kinds_follow_album_rules():
    """图片视频一组，文件一组，动图和贴纸单独发。"""
    bot = FakeBot()
    items = [Item(1, "photo", "p1"), Item(2, "animation", "g2"),
             Item(3, "video", "v3"), Item(4, "document", "d4"),
             Item(5, "document", "d5"), Item(6, "sticker", "s6")]
    await pack.build(bot, -100, items)
    groups = [c[2] for c in bot.of("group")]
    assert [type(x) for x in groups[0]] == [InputMediaPhoto, InputMediaVideo]
    assert [type(x) for x in groups[1]] == [InputMediaDocument, InputMediaDocument]
    assert [c[2][0] for c in bot.of("animation")] == ["g2"]
    assert [c[2][0] for c in bot.of("sticker")] == ["s6"]
    sent_ids = [x.media for g in groups for x in g]
    assert sent_ids == ["p1", "v3", "d4", "d5"], "用 file_id，不传字节"


@pytest.mark.asyncio
async def test_caption_summary_on_first_only_without_source_by_default():
    bot = FakeBot()
    items = [Item(1, "photo", "a", "第一张", src_key="c1", src_name="某频道", src_tag="#ch"),
             Item(2, "photo", "b"),
             Item(3, "photo", "c", "第三张", src_key="c1", src_name="某频道", src_tag="#ch")]
    await pack.build(bot, -100, items)
    media = bot.of("group")[0][2]
    assert media[0].caption == "<blockquote>1 : 第一张\n3 : 第三张</blockquote>"
    assert media[1].caption is None and media[2].caption is None
    assert "某频道" not in media[0].caption and "#ch" not in media[0].caption, \
        "TG 来源名默认不写"


@pytest.mark.asyncio
async def test_caption_with_source_when_enabled():
    bot = FakeBot()
    items = [Item(1, "photo", "a", "你好", src_key="c1", src_name="某频道", src_tag="#ch"),
             Item(2, "photo", "b", src_key="c1", src_name="某频道", src_tag="#ch")]
    await pack.build(bot, -100, items, show_source=True)
    cap = bot.of("group")[0][2][0].caption
    assert cap == "<blockquote><b>某频道</b> :\n1 : 你好</blockquote>\n1-2 · #ch"


@pytest.mark.asyncio
async def test_no_text_no_caption():
    bot = FakeBot()
    await pack.build(bot, -100, [Item(1, "photo", "a"), Item(2, "photo", "b")])
    assert all(x.caption is None for x in bot.of("group")[0][2])


@pytest.mark.asyncio
async def test_spoiler_dropped_by_default_kept_when_enabled():
    items = [Item(1, "photo", "a", spoiler=True), Item(2, "video", "b", spoiler=True),
             Item(3, "animation", "g", spoiler=True)]
    bot = FakeBot()
    await pack.build(bot, -100, items)
    assert not any(x.has_spoiler for x in bot.of("group")[0][2])
    assert bot.of("animation")[0][3]["has_spoiler"] is False

    bot = FakeBot()
    await pack.build(bot, -100, items, keep_spoiler=True)
    assert all(x.has_spoiler for x in bot.of("group")[0][2])
    assert bot.of("animation")[0][3]["has_spoiler"] is True


@pytest.mark.asyncio
async def test_single_keeps_own_caption_strips_spoiler_entity():
    ents = [MessageEntity(type="spoiler", offset=0, length=2),
            MessageEntity(type="bold", offset=3, length=2)]
    bot = FakeBot()
    await pack.build(bot, -100, [Item(1, "animation", "g", "秘密 加粗", ents)])
    kw = bot.of("animation")[0][3]
    assert kw["caption"] == "秘密 加粗" and kw["parse_mode"] is None
    assert [e.type for e in kw["caption_entities"]] == ["bold"]

    bot = FakeBot()
    await pack.build(bot, -100, [Item(1, "animation", "g", "秘密 加粗", ents)],
                     keep_spoiler=True)
    assert [e.type for e in bot.of("animation")[0][3]["caption_entities"]] == \
        ["spoiler", "bold"]


@pytest.mark.asyncio
async def test_group_failure_falls_back_to_singles():
    bot = FakeBot(fail_group=True)
    packed = await pack.build(bot, -100, [Item(1, "photo", "a"), Item(2, "photo", "b")])
    assert [c[2][0] for c in bot.of("photo")] == ["a", "b"]
    assert packed.note and len(packed.ids) == 2


@pytest.mark.asyncio
async def test_overlong_caption_sent_as_separate_text(monkeypatch):
    monkeypatch.setattr(tweet, "CAPTION_LIMIT", 10)
    bot = FakeBot()
    items = [Item(i, "photo", f"p{i}", "字" * 5) for i in range(1, 4)]
    packed = await pack.build(bot, -100, items)
    assert all(x.caption is None for x in bot.of("group")[0][2])
    assert len(bot.of("message")) == 1, "汇总另发一条"
    assert len(packed.blocks[0]) == 4


# ================================================================ 投递

@pytest.mark.asyncio
async def test_deliver_chunks_without_splitting_blocks():
    bot = FakeBot()
    blocks = [list(range(k * 10, k * 10 + 10)) for k in range(10)] + [[500, 501]]
    n = await pack.deliver(bot, -100, 42, pack.Packed(blocks))
    copies = [c[2] for c in bot.of("copies")]
    assert [len(c) for c in copies] == [100, 2] and n == 102


@pytest.mark.asyncio
async def test_delete_originals_in_batches():
    bot = FakeBot()
    await pack.delete_originals(bot, 42, list(range(250)))
    assert [len(c[2]) for c in bot.of("delete")] == [100, 100, 50]


# ================================================================ /pack 流程

@pytest.fixture
async def fresh(tmp_path):
    from config import CFG
    object.__setattr__(CFG, "db_path", tmp_path / "p.db")
    await db.init()
    yield
    await db.close()


class FakeMsg:
    def __init__(self, bot, mid=100, uid=42):
        self.bot = bot
        self.message_id = mid
        self.from_user = SimpleNamespace(id=uid)
        self.chat = SimpleNamespace(id=uid)
        self.replies = []

    async def reply(self, text, **kw):
        self.replies.append(text)

    async def answer(self, text, **kw):
        self.replies.append(text)


@pytest.fixture
def botmod(monkeypatch):
    import bot as botmod
    monkeypatch.setattr(pack, "SETTLE", 0)
    return botmod


@pytest.mark.asyncio
async def test_cmd_pack_delivers_and_keeps_originals_by_default(fresh, botmod):
    for mid in (1, 2, 3):
        pack.add(42, Item(mid, "photo", f"p{mid}"))
    bot = FakeBot()
    m = FakeMsg(bot)
    await botmod.cmd_pack(m, SimpleNamespace(args=None))
    assert len(bot.of("group")[0][2]) == 3
    assert bot.of("copies")[0][1] == 42
    assert not bot.of("delete"), "默认不删原消息"
    assert m.replies == [], "成功时不打扰"
    assert pack.count(42) == 0


@pytest.mark.asyncio
async def test_cmd_pack_deletes_when_enabled(fresh, botmod):
    import prefs
    await prefs.update(42, pack_delete=True)
    pack.add(42, Item(1, "photo", "a"))
    pack.add(42, Item(2, "photo", "b"))
    bot = FakeBot()
    await botmod.cmd_pack(FakeMsg(bot, mid=100), SimpleNamespace(args=None))
    assert bot.of("delete")[0][2] == [1, 2, 100], "原消息和命令本身一起删"


@pytest.mark.asyncio
async def test_cmd_pack_empty(fresh, botmod):
    m = FakeMsg(FakeBot())
    await botmod.cmd_pack(m, SimpleNamespace(args=None))
    assert "空" in m.replies[0]


@pytest.mark.asyncio
async def test_cmd_pack_failure_restores_inbox(fresh, botmod, monkeypatch):
    pack.add(42, Item(1, "photo", "a"))

    async def boom(*a, **kw):
        raise RuntimeError("网络断了")
    monkeypatch.setattr(pack, "build", boom)
    m = FakeMsg(FakeBot())
    await botmod.cmd_pack(m, SimpleNamespace(args=None))
    assert "组装失败" in m.replies[0] and pack.count(42) == 1


@pytest.mark.asyncio
async def test_cmd_pack_bad_args(fresh, botmod):
    pack.add(42, Item(1, "photo", "a"))
    m = FakeMsg(FakeBot())
    await botmod.cmd_pack(m, SimpleNamespace(args="随便写点"))
    assert "用法" in m.replies[0] and pack.count(42) == 1


@pytest.mark.asyncio
async def test_cmd_pack_fw_forwards_relay_copies(fresh, botmod, monkeypatch):
    await db.upsert_user(42, last_forward="@mychan")
    pack.add(42, Item(1, "photo", "a"))
    pack.add(42, Item(2, "photo", "b"))
    sent = []

    async def fake_send(client, target, peer, ids):
        sent.append((target, list(ids)))
        return len(ids)

    class Pool:
        async def acquire(self, uid):
            return object()

        def using(self, uid):
            return _Null()

    class _Null:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(botmod.forward, "send", fake_send)
    monkeypatch.setattr(botmod, "POOL", Pool())
    bot = FakeBot()
    await botmod.cmd_pack(FakeMsg(bot), SimpleNamespace(args="fw"))
    assert bot.of("copies"), "本人那份照常收到"
    assert sent == [("@mychan", [1001, 1002])]


@pytest.mark.asyncio
async def test_cmd_clear(fresh, botmod):
    pack.add(42, Item(1, "photo", "a"))
    m = FakeMsg(FakeBot())
    await botmod.cmd_clear(m)
    assert m.replies == ["已清空 1 个。"] and pack.count(42) == 0
