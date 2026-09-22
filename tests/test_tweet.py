"""推文支持的测试。

网络全部替换成假对象：FxTwitter 的响应按官方 v2 schema 构造，
媒体下载用假的 HTTP 会话，Telegram 上传用假客户端。
"""
import io
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
from telethon.tl.types import (  # noqa: E402
    DocumentAttributeAnimated,
    DocumentAttributeVideo,
    MessageEntityBlockquote,
)

import streamer  # noqa: E402
import tweet  # noqa: E402
from tweet import Tweet, TweetError  # noqa: E402
from streamer import WebMedia  # noqa: E402


# ================================================================ 链接识别

@pytest.mark.parametrize("text,want", [
    ("https://x.com/jack/status/20", "https://x.com/jack/status/20"),
    ("https://twitter.com/jack/status/20", "https://x.com/jack/status/20"),
    ("x.com/jack/status/20", "https://x.com/jack/status/20"),
    ("https://mobile.twitter.com/jack/status/20", "https://x.com/jack/status/20"),
    ("https://www.twitter.com/jack/status/20", "https://x.com/jack/status/20"),
    ("https://fxtwitter.com/jack/status/20", "https://x.com/jack/status/20"),
    ("https://vxtwitter.com/jack/status/20", "https://x.com/jack/status/20"),
    ("https://fixupx.com/jack/status/20", "https://x.com/jack/status/20"),
    ("https://fixvx.com/jack/status/20", "https://x.com/jack/status/20"),
    ("https://d.fxtwitter.com/jack/status/20", "https://x.com/jack/status/20"),
    ("https://x.com/jack/status/20/photo/1", "https://x.com/jack/status/20"),
    ("https://x.com/jack/status/20?s=46&t=abc", "https://x.com/jack/status/20"),
    ("https://twitter.com/jack/statuses/20", "https://x.com/jack/status/20"),
    ("https://x.com/i/status/20", "https://x.com/i/status/20"),
    ("https://x.com/i/web/status/20", "https://x.com/i/status/20"),
    ("看看这个 https://x.com/jack/status/20 很有意思", "https://x.com/jack/status/20"),
    ("https://X.COM/Jack/status/20", "https://x.com/Jack/status/20"),
])
def test_recognized(text, want):
    assert tweet.find_links(text) == [want]


@pytest.mark.parametrize("text", [
    "https://x.com/jack",                       # 主页，不是帖子
    "https://x.com/jack/likes",
    "https://abcx.com/jack/status/20",          # 域名里恰好含 x.com
    "https://notwitter.com/jack/status/20",
    "https://x.community/jack/status/20",
    "https://t.me/durov/1",
    "https://x.com/jack/status/",
    "https://x.com/jack/status/abc",
    "",
])
def test_not_recognized(text):
    assert tweet.find_links(text) == []


def test_dedupe_by_id():
    text = "https://x.com/a/status/20 https://twitter.com/a/status/20 fxtwitter.com/a/status/20"
    assert tweet.find_links(text) == ["https://x.com/a/status/20"]


def test_multiple_distinct():
    text = "https://x.com/a/status/20 和 https://x.com/b/status/21"
    assert len(tweet.find_links(text)) == 2


def test_tme_links_not_confused():
    """推文识别不能吃掉 t.me 链接，反之亦然。"""
    from parser import find_links as tme
    text = "https://x.com/a/status/20 https://t.me/durov/1"
    assert tweet.find_links(text) == ["https://x.com/a/status/20"]
    assert tme(text) == ["https://t.me/durov/1"]


def test_is_tweet():
    assert tweet.is_tweet("https://x.com/a/status/20")
    assert not tweet.is_tweet("https://t.me/durov/1")
    assert not tweet.is_tweet("tgsaver://p/bot/5")


# ================================================================ API 解析

def _payload(**over):
    st = {
        "type": "status", "id": "1234567890",
        "url": "https://x.com/jack/status/1234567890",
        "text": "hello world",
        "author": {"type": "profile", "name": "Jack Dorsey", "screen_name": "jack"},
        "media": {"all": []},
    }
    st.update(over)
    return {"code": 200, "status": st}


def test_parse_basic():
    t = tweet.parse_payload(_payload())
    assert t.name == "Jack Dorsey"
    assert t.screen_name == "jack"
    assert t.text == "hello world"
    assert t.media == []
    assert t.url == "https://x.com/jack/status/1234567890"


def test_button_url_uses_real_screen_name():
    """链接是 /i/status/ 形式时，按钮也要指向带真实用户名的原帖。"""
    t = tweet.parse_payload(_payload())
    assert "/jack/" in t.url and "/i/" not in t.url


def test_parse_media_order_preserved():
    p = _payload(media={"all": [
        {"type": "video", "url": "https://video.twimg.com/v1.mp4", "width": 1280,
         "height": 720, "duration": 12.5, "thumbnail_url": "https://pbs.twimg.com/t.jpg",
         "formats": []},
        {"type": "photo", "url": "https://pbs.twimg.com/media/AAA.jpg", "width": 800, "height": 600},
    ]})
    t = tweet.parse_payload(p)
    assert [m.kind for m in t.media] == ["video", "photo"]
    assert t.media[0].duration == 12.5
    assert t.media[0].thumb_url == "https://pbs.twimg.com/t.jpg"


def test_fallback_to_photos_and_videos():
    p = _payload(media={
        "photos": [{"type": "photo", "url": "https://pbs.twimg.com/media/A.jpg",
                    "width": 1, "height": 1}],
        "videos": [{"type": "video", "url": "https://v/1.mp4", "width": 1,
                    "height": 1, "duration": 1, "formats": []}],
    })
    assert [m.kind for m in tweet.parse_payload(p).media] == ["photo", "video"]


def test_skip_mosaic_and_unknown():
    p = _payload(media={"all": [
        {"type": "mosaic_photo", "url": "https://mosaic"},
        {"type": "photo", "url": "https://pbs.twimg.com/media/A.jpg", "width": 1, "height": 1},
        {"type": "photo"},                       # 没 url
        "garbage",
    ]})
    assert len(tweet.parse_payload(p).media) == 1


def test_gif_kept_as_animation():
    p = _payload(media={"all": [
        {"type": "gif", "url": "https://video.twimg.com/tweet_video/X.mp4",
         "width": 480, "height": 270, "duration": 3, "formats": []}]})
    assert tweet.parse_payload(p).media[0].kind == "gif"


def test_best_video_prefers_h264_highest_bitrate():
    it = {"url": "https://fallback.mp4", "formats": [
        {"container": "mp4", "codec": "h264", "bitrate": 832000, "url": "https://low.mp4"},
        {"container": "mp4", "codec": "h264", "bitrate": 2176000, "url": "https://high.mp4"},
        {"container": "mp4", "codec": "hevc", "bitrate": 9000000, "url": "https://hevc.mp4"},
        {"container": "m3u8", "bitrate": 99999999, "url": "https://stream.m3u8"},
    ]}
    assert tweet._best_video(it)[0] == "https://high.mp4"


def test_best_video_falls_back_to_url():
    assert tweet._best_video({"url": "https://u.mp4", "formats": []})[0] == "https://u.mp4"
    assert tweet._best_video({"url": "https://u.mp4", "formats": [
        {"container": "m3u8", "url": "https://x.m3u8"}]})[0] == "https://u.mp4"
    assert tweet._best_video({"url": "https://u.mp4", "filesize": 42,
                              "formats": []}) == ("https://u.mp4", 42)


def test_best_photo_size():
    assert tweet._best_photo("https://pbs.twimg.com/media/AbC.jpg") == \
        "https://pbs.twimg.com/media/AbC?format=jpg&name=4096x4096"
    assert tweet._best_photo("https://pbs.twimg.com/media/AbC.png?name=small") == \
        "https://pbs.twimg.com/media/AbC?format=png&name=4096x4096"
    assert tweet._best_photo("https://other.cdn/x.jpg") == "https://other.cdn/x.jpg"


@pytest.mark.parametrize("reason,frag", [
    ("deleted", "删除"), ("suspended", "冻结"), ("private", "受保护"),
])
def test_tombstone(reason, frag):
    p = {"code": 200, "status": {"type": "tombstone", "provider": "twitter",
                                 "reason": reason, "message": "x"}}
    with pytest.raises(TweetError) as ei:
        tweet.parse_payload(p)
    assert frag in str(ei.value)


def test_404_and_401():
    with pytest.raises(TweetError, match="不存在"):
        tweet.parse_payload({"code": 404, "status": None})
    with pytest.raises(TweetError, match="受保护"):
        tweet.parse_payload({"code": 401, "status": {"type": "status"}})


def test_garbage_payload():
    with pytest.raises(TweetError):
        tweet.parse_payload("not json")
    with pytest.raises(TweetError):
        tweet.parse_payload({"code": 500, "message": "boom"})


def test_tweet_error_is_fatal():
    """这类错误重试没有意义，必须在 FATAL 里。"""
    import taskqueue
    assert issubclass(TweetError, taskqueue.FATAL)


# ================================================================ hashtag

@pytest.mark.parametrize("name,want", [
    ("Jack Dorsey", "#Jack_Dorsey"),
    ("小明🌸", "#小明"),
    ("a.b-c!!d", "#a_b_c_d"),
    ("__x__", "#x"),
    ("🌸🌸", ""),
    ("", ""),
    ("Russell3402", "#Russell3402"),
])
def test_to_hashtag(name, want):
    assert tweet.to_hashtag(name) == want


def test_only_id_is_tagged():
    """昵称不再做成 hashtag，只有 ID 有。"""
    t = Tweet("1", "Russell", "Russell3402", "hi")
    html, _ = tweet.build_html(t, 4096)
    assert "#Russell3402" in html
    assert "#Russell " not in html and not html.startswith("#")


# ================================================================ 组装

def test_build_format_matches_spec():
    t = Tweet("1", "Russell", "Russell3402", "已经到了看到价格就知道在卖什么的程度😂")
    html, cut = tweet.build_html(t, 4096)
    assert not cut
    assert html == (
        "Russell:\n"
        "<blockquote>已经到了看到价格就知道在卖什么的程度😂</blockquote>\n"
        '<a href="https://x.com/Russell3402/status/1">原文链接</a> #Russell3402')


def test_link_precedes_id_and_points_to_x():
    from telethon.extensions import html as tl_html
    from telethon.tl.types import MessageEntityTextUrl
    t = Tweet("1", "杰克", "jack", "hi")
    text, ents = tl_html.parse(tweet.build_html(t, 4096)[0])
    assert text.splitlines()[-1] == "原文链接 #jack"
    links = [e for e in ents if isinstance(e, MessageEntityTextUrl)]
    assert len(links) == 1 and links[0].url == "https://x.com/jack/status/1"


def test_nickname_line_has_colon_then_quote():
    from telethon.extensions import html as tl_html
    t = Tweet("1", "杰克", "jack", "line1\nline2")
    text, ents = tl_html.parse(tweet.build_html(t, 4096)[0])
    lines = text.splitlines()
    assert lines[0] == "杰克:"
    bq = [e for e in ents if isinstance(e, MessageEntityBlockquote)][0]
    quoted = text.encode("utf-16-le")[bq.offset * 2:(bq.offset + bq.length) * 2]
    assert quoted.decode("utf-16-le") == "line1\nline2"
    assert "原文链接" not in quoted.decode("utf-16-le"), "链接和 tag 必须在引用块之外"


def test_build_escapes_html():
    t = Tweet("1", "A<b>", "a", "1 < 2 & <script>")
    html, _ = tweet.build_html(t, 4096)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert html.startswith("A&lt;b&gt;:")


def test_build_without_text():
    t = Tweet("1", "杰克", "jack", "")
    html, _ = tweet.build_html(t, 4096)
    assert "blockquote" not in html
    assert html.splitlines()[0] == "杰克:"


def test_falls_back_to_screen_name_when_no_nickname():
    t = Tweet("1", "", "jack", "hi")
    assert tweet.build_html(t, 4096)[0].startswith("jack:")


def test_link_url_not_counted_in_length():
    """超链接的 URL 不计入长度，只有「原文链接」四个字算。"""
    from telethon.extensions import html as tl_html
    t = Tweet("1", "J", "j", "字" * 2000)
    html, cut = tweet.build_html(t, 1024)
    assert cut
    text, _ = tl_html.parse(html)
    n = tweet.utf16_len(text)
    assert n <= 1024
    assert n >= 1020, f"预算算得太保守，只用了 {n}"


def test_truncation_counts_emoji_as_two():
    from telethon.extensions import html as tl_html
    t = Tweet("1", "Jack", "jack", "😀" * 1000)
    html, cut = tweet.build_html(t, 1024)
    assert cut
    assert tweet.utf16_len(tl_html.parse(html)[0]) <= 1024


def test_no_truncation_when_fits():
    assert not tweet.build_html(Tweet("1", "J", "j", "short"), 1024)[1]


# ================================================================ 形态规划

def _m(k="photo", size=0):
    return WebMedia(k, "https://u", size=size)


def test_plan_text_only():
    p = tweet.plan(Tweet("1", "J", "j", "hi"))
    assert p["mode"] == "text" and p["kind"] == "tweet"


def test_plan_single_media_has_caption():
    p = tweet.plan(Tweet("1", "J", "j", "hi", [_m()]))
    assert p["mode"] == "media" and p["caption"] is True


def test_plan_album_also_has_caption():
    """去掉按钮后，相册的说明可以直接挂在第一项上。"""
    p = tweet.plan(Tweet("1", "J", "j", "hi", [_m(), _m("video")]))
    assert p["mode"] == "media" and p["caption"] is True


def test_plan_long_text():
    p = tweet.plan(Tweet("1", "J", "j", "x" * 2000, [_m(), _m()]))
    assert p["mode"] == "media_long" and p["caption"] is False
    assert "…" not in p["html"]


def test_plan_is_json_serializable():
    import json
    p = tweet.plan(Tweet("1", "J", "j", "hi <&>", [_m(), _m()]))
    assert json.loads(json.dumps(p)) == p


@pytest.mark.parametrize("media,expect_relay", [
    ([], False),
    ([_m("photo")], False),
    ([_m("photo", 6 * 1024 * 1024)], True),        # 超过 URL 照片上限
    ([_m("video", 19 * 1024 * 1024)], False),
    ([_m("video", 21 * 1024 * 1024)], True),       # 超过 URL 文件上限
    ([_m("video", 0)], False),                     # 大小未知就先试
    ([_m("gif")], False),
    ([_m("gif"), _m("photo")], True),              # 动图进不了相册
])
def test_needs_relay(media, expect_relay):
    assert (tweet.needs_relay(Tweet("1", "J", "j", "", media)) is not None) is expect_relay


def test_video_size_captured_from_best_format():
    p = _payload(media={"all": [{"type": "video", "url": "https://f.mp4",
        "width": 1, "height": 1, "duration": 1, "formats": [
            {"container": "mp4", "codec": "h264", "bitrate": 1, "url": "https://a", "size": 111},
            {"container": "mp4", "codec": "h264", "bitrate": 9, "url": "https://b", "size": 999},
        ]}]})
    m = tweet.parse_payload(p).media[0]
    assert (m.url, m.size) == ("https://b", 999)


# ================================================================ 投递

class FakeBot:
    def __init__(self, reject=False):
        self.calls = []
        self.reject = reject

    def _maybe_reject(self):
        if self.reject:
            from aiogram.exceptions import TelegramBadRequest
            raise TelegramBadRequest(method=None, message="failed to get HTTP URL content")

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("text", text, kw))

    async def send_photo(self, chat_id, photo, **kw):
        self._maybe_reject()
        self.calls.append(("photo", photo, kw))

    async def send_video(self, chat_id, video, **kw):
        self._maybe_reject()
        self.calls.append(("video", video, kw))

    async def send_animation(self, chat_id, animation, **kw):
        self._maybe_reject()
        self.calls.append(("animation", animation, kw))

    async def send_media_group(self, chat_id, media, **kw):
        self._maybe_reject()
        self.calls.append(("group", media, kw))

    async def copy_message(self, chat_id, from_chat_id, message_id, **kw):
        self.calls.append(("copy", message_id, kw))

    async def copy_messages(self, chat_id, from_chat_id, message_ids, **kw):
        self.calls.append(("copies", list(message_ids), kw))
        return list(message_ids)


def _tw(media, text="hi"):
    t = Tweet("1", "J", "j", text, media)
    return t, tweet.plan(t)


def _no_buttons(bot):
    return all("reply_markup" not in c[2] for c in bot.calls)


@pytest.mark.asyncio
async def test_direct_text():
    import sender
    bot = FakeBot()
    t, extra = _tw([])
    await sender.send_tweet_direct(bot, t, extra, 9, 5)
    assert [c[0] for c in bot.calls] == ["text"]
    assert bot.calls[0][2]["link_preview_options"].is_disabled
    assert _no_buttons(bot)


@pytest.mark.asyncio
async def test_direct_single_photo_by_url():
    """媒体交给 Telegram 按 URL 拉，不经过本机。"""
    import sender
    bot = FakeBot()
    t, extra = _tw([WebMedia("photo", "https://pbs.twimg.com/media/A?name=4096x4096")])
    await sender.send_tweet_direct(bot, t, extra, 9, 5)
    op, url, kw = bot.calls[0]
    assert op == "photo" and url.startswith("https://pbs.twimg.com/")
    assert "<blockquote>" in kw["caption"] and kw["parse_mode"] == "HTML"
    assert kw["reply_parameters"].message_id == 5
    assert len(bot.calls) == 1 and _no_buttons(bot)


@pytest.mark.asyncio
async def test_direct_video_carries_dimensions():
    import sender
    bot = FakeBot()
    t, extra = _tw([WebMedia("video", "https://v.mp4", 1280, 720, 12.7)])
    await sender.send_tweet_direct(bot, t, extra, 9, 5)
    kw = bot.calls[0][2]
    assert (kw["width"], kw["height"], kw["duration"]) == (1280, 720, 12)
    assert kw["supports_streaming"]


@pytest.mark.asyncio
async def test_direct_gif_as_animation():
    import sender
    bot = FakeBot()
    t, extra = _tw([WebMedia("gif", "https://g.mp4", 480, 270, 3)])
    await sender.send_tweet_direct(bot, t, extra, 9, 5)
    assert bot.calls[0][0] == "animation"


@pytest.mark.asyncio
async def test_direct_album_caption_on_first_only():
    import sender
    bot = FakeBot()
    t, extra = _tw([WebMedia("photo", "https://a"), WebMedia("video", "https://b"),
                    WebMedia("photo", "https://c")])
    await sender.send_tweet_direct(bot, t, extra, 9, 5)
    assert [c[0] for c in bot.calls] == ["group"], "说明挂在相册上，不该再跟文字消息"
    group = bot.calls[0][1]
    assert "<blockquote>" in group[0].caption
    assert all(g.caption is None for g in group[1:])


@pytest.mark.asyncio
async def test_direct_long_text_follows_media():
    import sender
    bot = FakeBot()
    t, extra = _tw([WebMedia("photo", "https://a"), WebMedia("photo", "https://b")],
                   text="x" * 2000)
    await sender.send_tweet_direct(bot, t, extra, 9, 5)
    assert [c[0] for c in bot.calls] == ["group", "text"]
    assert getattr(bot.calls[0][1][0], "caption", None) is None


@pytest.mark.asyncio
async def test_direct_rejection_raises_url_rejected():
    import sender
    bot = FakeBot(reject=True)
    t, extra = _tw([WebMedia("video", "https://big.mp4")])
    with pytest.raises(sender.UrlRejected):
        await sender.send_tweet_direct(bot, t, extra, 9, 5)
    assert bot.calls == [], "被拒时用户什么都不该收到，才能安全回退"


@pytest.mark.asyncio
async def test_relay_delivery_album_uses_copy_messages():
    import sender
    bot = FakeBot()
    _, extra = _tw([_m(), _m()])
    await sender.deliver_tweet(bot, -100, [1, 2], extra, 9, 5)
    assert [c[0] for c in bot.calls] == ["copies"]


@pytest.mark.asyncio
async def test_relay_delivery_split_copies_individually():
    import sender
    bot = FakeBot()
    _, extra = _tw([_m(), _m()])
    extra["split"] = True
    await sender.deliver_tweet(bot, -100, [1, 2], extra, 9, 5)
    assert [c[0] for c in bot.calls] == ["copy", "copy"]


@pytest.mark.asyncio
async def test_relay_delivery_long_appends_text():
    import sender
    bot = FakeBot()
    _, extra = _tw([_m()], text="x" * 2000)
    await sender.deliver_tweet(bot, -100, [7], extra, 9, 5)
    assert [c[0] for c in bot.calls] == ["copy", "text"]


# ================================================================ 媒体搬运

class FakeContent:
    def __init__(self, data):
        self.data = data

    async def iter_chunked(self, n):
        for i in range(0, len(self.data), n):
            yield self.data[i:i + n]


class FakeResp:
    def __init__(self, data, status=200, with_length=True):
        self.status = status
        self.content_length = len(data) if with_length else None
        self.content = FakeContent(data)
        self._data = data

    async def read(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


class FakeHttp:
    def __init__(self, table, with_length=True):
        self.table = table
        self.with_length = with_length
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        data, status = self.table.get(url, (b"", 404))
        return FakeResp(data, status, self.with_length)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


class FakeTg:
    def __init__(self):
        self.uploaded = []
        self.sent = []
        self.multi = []

    async def upload_file(self, f, file_size=None, file_name=None,
                          progress_callback=None, **kw):
        if hasattr(f, "read"):
            data = await f.read(file_size)
            self.uploaded.append((file_name, len(data)))
        else:
            self.uploaded.append((file_name, Path(f).stat().st_size))
        return f"h:{file_name}"

    async def send_file(self, relay, file=None, **kw):
        self.sent.append((file, kw))
        return type("S", (), {"id": 100 + len(self.sent)})()

    async def __call__(self, req):
        from telethon.tl.types import (Document, MessageMediaDocument,
                                       MessageMediaPhoto, Photo,
                                       UpdateNewChannelMessage)
        name = type(req).__name__
        if name == "UploadMediaRequest":
            if type(req.media).__name__ == "InputMediaUploadedPhoto":
                return MessageMediaPhoto(photo=Photo(
                    id=1, access_hash=1, file_reference=b"", date=None,
                    sizes=[], dc_id=1))
            return MessageMediaDocument(document=Document(
                id=1, access_hash=1, file_reference=b"", date=None,
                mime_type="video/mp4", size=1, dc_id=1, attributes=[]))
        if name == "SendMultiMediaRequest":
            self.multi.append(req.multi_media)

            class U(UpdateNewChannelMessage):
                def __init__(self, i):
                    self.message = type("M", (), {"id": i})()
                    self.pts = self.pts_count = 0
            return type("R", (), {"updates": [U(900 + i) for i in range(len(req.multi_media))]})()
        raise AssertionError(name)


@pytest.fixture
def cfg(tmp_path):
    from config import CFG
    object.__setattr__(CFG, "max_upload_size", 2 * 1024 ** 3)
    object.__setattr__(CFG, "tmp_dir", tmp_path)
    return CFG


def _use_http(monkeypatch, http):
    monkeypatch.setattr(streamer, "http_session", lambda: http)


@pytest.mark.asyncio
async def test_single_photo_with_caption(monkeypatch, cfg):
    http = FakeHttp({"https://p.jpg": (b"x" * 5000, 200)})
    _use_http(monkeypatch, http)
    tg = FakeTg()
    ids, total, note = await streamer.relay_web_media(
        tg, [WebMedia("photo", "https://p.jpg")], -100,
        caption_html="J\n<blockquote>hello</blockquote>\n#J #j")
    assert ids == [101] and total == 5000 and note == ""
    kw = tg.sent[0][1]
    assert kw["caption"].startswith("J\nhello")
    assert any(isinstance(e, MessageEntityBlockquote) for e in kw["formatting_entities"])


@pytest.mark.asyncio
async def test_album_via_multimedia(monkeypatch, cfg):
    http = FakeHttp({
        "https://a.jpg": (b"a" * 100, 200),
        "https://v.mp4": (b"v" * 300, 200),
        "https://t.jpg": (b"t" * 10, 200),
    })
    _use_http(monkeypatch, http)
    tg = FakeTg()
    items = [WebMedia("photo", "https://a.jpg"),
             WebMedia("video", "https://v.mp4", 1280, 720, 9.5, "https://t.jpg")]
    ids, total, note = await streamer.relay_web_media(tg, items, -100)
    assert len(tg.multi) == 1 and len(tg.multi[0]) == 2
    assert ids == [900, 901] and total == 400 and note == ""
    assert all(sm.message == "" for sm in tg.multi[0]), "相册不该带说明"
    assert "https://t.jpg" in http.requested, "视频要取缩略图"


@pytest.mark.asyncio
async def test_relay_album_caption_on_first(monkeypatch, cfg):
    http = FakeHttp({"https://a": (b"a" * 10, 200), "https://b": (b"b" * 10, 200)})
    _use_http(monkeypatch, http)
    tg = FakeTg()
    await streamer.relay_web_media(
        tg, [WebMedia("photo", "https://a"), WebMedia("photo", "https://b")], -100,
        caption_html="J:\n<blockquote>hi</blockquote>")
    msgs = [sm.message for sm in tg.multi[0]]
    assert msgs[0].startswith("J:") and msgs[1] == ""


@pytest.mark.asyncio
async def test_video_attributes(monkeypatch, cfg):
    http = FakeHttp({"https://v.mp4": (b"v" * 100, 200)})
    _use_http(monkeypatch, http)
    tg = FakeTg()
    await streamer.relay_web_media(
        tg, [WebMedia("video", "https://v.mp4", 1920, 1080, 42.5)], -100)
    attrs = tg.sent[0][1]["attributes"]
    v = [a for a in attrs if isinstance(a, DocumentAttributeVideo)][0]
    assert (v.w, v.h, v.duration, v.supports_streaming) == (1920, 1080, 42.5, True)
    assert not any(isinstance(a, DocumentAttributeAnimated) for a in attrs)


@pytest.mark.asyncio
async def test_gif_is_animated_and_silent(monkeypatch, cfg):
    http = FakeHttp({"https://g.mp4": (b"g" * 50, 200)})
    _use_http(monkeypatch, http)
    tg = FakeTg()
    await streamer.relay_web_media(tg, [WebMedia("gif", "https://g.mp4", 480, 270, 3)], -100)
    attrs = tg.sent[0][1]["attributes"]
    assert any(isinstance(a, DocumentAttributeAnimated) for a in attrs)
    assert [a for a in attrs if isinstance(a, DocumentAttributeVideo)][0].nosound


@pytest.mark.asyncio
async def test_streams_when_length_known(monkeypatch, cfg):
    """有 Content-Length 就走有界管道，临时目录里不该出现任何文件。"""
    http = FakeHttp({"https://v.mp4": (b"v" * 3_000_000, 200)})
    _use_http(monkeypatch, http)
    tg = FakeTg()
    await streamer.relay_web_media(tg, [WebMedia("video", "https://v.mp4")], -100)
    assert tg.uploaded[0][1] == 3_000_000
    assert list(cfg.tmp_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_spools_when_length_unknown(monkeypatch, cfg):
    """没有 Content-Length 时只能落盘，且用完必须删掉。"""
    http = FakeHttp({"https://v.mp4": (b"v" * 12345, 200)}, with_length=False)
    _use_http(monkeypatch, http)
    tg = FakeTg()
    ids, total, _ = await streamer.relay_web_media(tg, [WebMedia("video", "https://v.mp4")], -100)
    assert total == 12345
    assert tg.uploaded[0][1] == 12345
    assert list(cfg.tmp_dir.iterdir()) == [], "临时文件没清理"


@pytest.mark.asyncio
async def test_http_error_raises(monkeypatch, cfg):
    _use_http(monkeypatch, FakeHttp({}))
    with pytest.raises(streamer.TransferError, match="HTTP 404"):
        await streamer.relay_web_media(FakeTg(), [WebMedia("photo", "https://gone")], -100)


@pytest.mark.asyncio
async def test_oversize_rejected(monkeypatch, cfg):
    object.__setattr__(cfg, "max_upload_size", 100)
    _use_http(monkeypatch, FakeHttp({"https://big": (b"x" * 500, 200)}))
    tg = FakeTg()
    with pytest.raises(streamer.TransferError, match="超过上传上限"):
        await streamer.relay_web_media(tg, [WebMedia("video", "https://big")], -100)
    assert tg.uploaded == []


# ================================================================ 调度

def _runner():
    import taskqueue
    r = taskqueue.Runner.__new__(taskqueue.Runner)
    import asyncio
    r.bot = FakeBot()
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


@pytest.fixture
async def qdb(tmp_path):
    import db
    from config import CFG
    object.__setattr__(CFG, "db_path", tmp_path / "q.db")
    await db.init()
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_fast_path_completes_without_relay(qdb, monkeypatch):
    import taskqueue
    r = _runner()
    t = Tweet("1", "J", "j", "hi", [WebMedia("photo", "https://a")])
    tid = await qdb.add_task(42, "https://x.com/j/status/1", 9, 5)
    job = taskqueue.Job(tid, 42, "https://x.com/j/status/1", 9, 5, tweet=t)

    await r._run_tweet(job, "fast")
    assert [c[0] for c in r.bot.calls] == ["photo"]
    assert r.slow.empty(), "直发成功不该进慢通道"
    assert (await qdb.get_task(tid))["state"] == "done"


@pytest.mark.asyncio
async def test_rejected_falls_back_to_slow(qdb):
    import taskqueue
    r = _runner()
    r.bot = FakeBot(reject=True)
    t = Tweet("1", "J", "j", "hi", [WebMedia("video", "https://big.mp4")])
    tid = await qdb.add_task(42, "https://x.com/j/status/1", 9, 5)
    job = taskqueue.Job(tid, 42, "https://x.com/j/status/1", 9, 5, tweet=t)

    await r._run_tweet(job, "fast")
    assert r.slow.qsize() == 1, "被拒后应转慢通道"
    assert job.lane == "slow"
    assert job.tweet is t, "推文数据要随任务带过去，不必再请求一次 API"
    assert any("中转" in x for x in r.said)
    row = await qdb.get_task(tid)
    assert row["lane"] == "slow" and row["extra"]


@pytest.mark.asyncio
async def test_known_oversize_skips_direct_attempt(qdb):
    """已知超限就别白发一次必然失败的请求。"""
    import taskqueue
    r = _runner()
    t = Tweet("1", "J", "j", "hi", [WebMedia("video", "https://big", size=50 * 1024 ** 2)])
    tid = await qdb.add_task(42, "https://x.com/j/status/1", 9, 5)
    job = taskqueue.Job(tid, 42, "https://x.com/j/status/1", 9, 5, tweet=t)

    await r._run_tweet(job, "fast")
    assert r.bot.calls == []
    assert r.slow.qsize() == 1
    assert any("20MB" in x for x in r.said)


def test_tweets_start_in_fast_lane_without_status_message():
    src = (Path(__file__).resolve().parent.parent / "taskqueue.py").read_text()
    sub = src[src.index("    async def submit(self"):src.index("        return task_id\n")]
    assert "if not tweet.is_tweet(link):" in sub, "推文不该先发「已排队」"
    assert "await self.fast.put(job)" in sub
    assert "self.slow.put" not in sub


def test_extra_persisted_for_retry():
    src = (Path(__file__).resolve().parent.parent / "taskqueue.py").read_text()
    assert "extra=json.dumps(job.extra) if job.extra else None" in src
    assert 'extra=json.loads(r["extra"]) if r["extra"] else None' in src


def test_fxtwitter_links_trigger():
    """用户直接发 fxtwitter 链接也要触发。"""
    for u in ("https://fxtwitter.com/Russell3402/status/1969686534",
              "fxtwitter.com/Russell3402/status/1969686534",
              "https://fixupx.com/Russell3402/status/1969686534"):
        assert tweet.find_links(u) == ["https://x.com/Russell3402/status/1969686534"]
