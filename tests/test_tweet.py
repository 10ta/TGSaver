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
    t = Tweet("1", "Russell", "Russell3402", "hi")
    html, _ = tweet.build_html(t, 4096)
    assert "#Russell3402" in html and "#Russell " not in html


# ================================================================ 组装

def _parse(html):
    from telethon.extensions import html as tl_html
    return tl_html.parse(html)


def test_build_format_matches_spec():
    t = Tweet("1", "DT TAKURO", "KarenCo55187924", "がっつり見えてない?")
    html, cut = tweet.build_html(t, 4096)
    assert not cut
    assert html == (
        "<blockquote><b>DT TAKURO</b> : がっつり見えてない?</blockquote>\n"
        '<a href="https://x.com/KarenCo55187924/status/1">原帖链接</a> · #KarenCo55187924')


def test_nickname_fully_bold_colon_outside():
    from telethon.tl.types import MessageEntityBold
    text, ents = _parse(tweet.build_html(Tweet("1", "DT TAKURO", "k", "hi"), 4096)[0])
    assert text.splitlines()[0] == "DT TAKURO : hi"
    bold = [e for e in ents if isinstance(e, MessageEntityBold)]
    assert len(bold) == 1 and (bold[0].offset, bold[0].length) == (0, len("DT TAKURO"))


def test_quote_starts_with_nickname():
    """昵称和冒号并入引用块开头，正文紧跟其后，多行正文保持换行。"""
    text, ents = _parse(tweet.build_html(Tweet("1", "李秀", "x", "line1\nline2"), 4096)[0])
    bq = [e for e in ents if isinstance(e, MessageEntityBlockquote)]
    assert len(bq) == 1
    q = text.encode("utf-16-le")[bq[0].offset * 2:(bq[0].offset + bq[0].length) * 2].decode("utf-16-le")
    assert q == "李秀 : line1\nline2"
    assert "原帖链接" not in q and "#x" not in q


def test_link_line_points_to_x():
    from telethon.tl.types import MessageEntityTextUrl
    text, ents = _parse(tweet.build_html(Tweet("1", "杰克", "jack", "hi"), 4096)[0])
    assert text.splitlines()[-1] == "原帖链接 · #jack"
    assert [e.url for e in ents if isinstance(e, MessageEntityTextUrl)] == \
        ["https://x.com/jack/status/1"]


def test_signature_line(monkeypatch):
    from telethon.tl.types import MessageEntityTextUrl
    monkeypatch.setattr(tweet, "SIGN_TEXT", "@欧派TV")
    monkeypatch.setattr(tweet, "SIGN_URL", "https://t.me/optv4")
    text, ents = _parse(tweet.build_html(Tweet("1", "杰克", "jack", "hi"), 4096)[0])
    assert text.endswith("原帖链接 · #jack\n\nvia @欧派TV"), "署名前要空一行"
    assert [e.url for e in ents if isinstance(e, MessageEntityTextUrl)] == \
        ["https://x.com/jack/status/1", "https://t.me/optv4"]


def test_signature_text_only(monkeypatch):
    monkeypatch.setattr(tweet, "SIGN_TEXT", "@欧派TV")
    monkeypatch.setattr(tweet, "SIGN_URL", "")
    html, _ = tweet.build_html(Tweet("1", "J", "j", "hi"), 4096)
    assert html.endswith("\n\nvia @欧派TV")


def test_signature_absent_by_default():
    """公开仓库默认不带任何人的署名。"""
    assert tweet.SIGN_TEXT == "" and tweet.SIGN_URL == ""
    html, _ = tweet.build_html(Tweet("1", "J", "j", "hi"), 4096)
    assert "via" not in html and not html.endswith("\n")


def test_signature_counted_in_length(monkeypatch):
    monkeypatch.setattr(tweet, "SIGN_TEXT", "@欧派TV" * 5)
    monkeypatch.setattr(tweet, "SIGN_URL", "https://t.me/optv4")
    html, cut = tweet.build_html(Tweet("1", "J", "j", "字" * 3000), 1024)
    assert cut and tweet.utf16_len(_parse(html)[0]) <= 1024


def test_signature_escaped(monkeypatch):
    monkeypatch.setattr(tweet, "SIGN_TEXT", "<b>x</b>")
    monkeypatch.setattr(tweet, "SIGN_URL", 'https://a"b')
    html, _ = tweet.build_html(Tweet("1", "J", "j", "hi"), 4096)
    assert "<b>x</b>" not in html and "&lt;b&gt;" in html
    assert 'a"b' not in html


def test_build_escapes_html():
    t = Tweet("1", "A<b>", "a", "1 < 2 & <script>")
    html, _ = tweet.build_html(t, 4096)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert html.startswith("<blockquote><b>A&lt;b&gt;</b> :")


def test_build_without_text():
    """没有正文时引用块里只有「昵称 :」。"""
    html, _ = tweet.build_html(Tweet("1", "杰克", "jack", ""), 4096)
    assert html.splitlines() == ["<blockquote><b>杰克</b> :</blockquote>",
                                 '<a href="https://x.com/jack/status/1">原帖链接</a> · #jack']


def test_falls_back_to_screen_name_when_no_nickname():
    assert tweet.build_html(Tweet("1", "", "jack", "hi"), 4096)[0].startswith(
        "<blockquote><b>jack</b> : hi")


def test_link_url_not_counted_in_length():
    t = Tweet("1", "J", "j", "字" * 2000)
    html, cut = tweet.build_html(t, 1024)
    assert cut
    n = tweet.utf16_len(_parse(html)[0])
    assert 1020 <= n <= 1024, f"预算不准：{n}"


def test_truncation_counts_emoji_as_two():
    html, cut = tweet.build_html(Tweet("1", "Jack", "jack", "😀" * 1000), 1024)
    assert cut and tweet.utf16_len(_parse(html)[0]) <= 1024


def test_no_truncation_when_fits():
    assert not tweet.build_html(Tweet("1", "J", "j", "short"), 1024)[1]


def test_preview_url_uses_fxtwitter():
    t = Tweet("123", "J", "jack", "")
    assert t.preview_url == "https://fxtwitter.com/jack/status/123"
    assert t.url == "https://x.com/jack/status/123"


# ================================================================ 形态规划

@pytest.fixture
def media_mode(monkeypatch):
    """TWEET_MODE=media：真正发送媒体的那套逻辑。"""
    monkeypatch.setattr(tweet, "TWEET_MODE", "media")


def test_default_mode_is_auto():
    assert tweet.TWEET_MODE == "auto"
    assert tweet.PREVIEW_TIMEOUT == 20


def test_plan_preview_for_media():
    p = tweet.plan(Tweet("1", "J", "j", "hi", [WebMedia("photo", "https://u")]))
    assert p["mode"] == "preview"
    assert p["preview_url"] == "https://fxtwitter.com/j/status/1"


def test_plan_preview_even_for_huge_video():
    """预览模式不发媒体，大小限制根本不相关，永远不需要中转。"""
    t = Tweet("1", "J", "j", "hi", [WebMedia("video", "https://u", size=999 * 1024 ** 2)])
    p = tweet.plan(t)
    assert p["mode"] == "preview" and tweet.needs_relay(t, p) is None


def test_text_only_has_no_preview():
    """纯文字推文不开预览：预览卡片只会把正文再显示一遍。"""
    assert tweet.plan(Tweet("1", "J", "j", "hi"))["mode"] == "text"

def _m(k="photo", size=0):
    return WebMedia(k, "https://u", size=size)


def test_plan_text_only():
    p = tweet.plan(Tweet("1", "J", "j", "hi"))
    assert p["mode"] == "text" and p["kind"] == "tweet"


@pytest.mark.usefixtures("media_mode")
def test_plan_single_media_has_caption():
    p = tweet.plan(Tweet("1", "J", "j", "hi", [_m()]))
    assert p["mode"] == "media" and p["caption"] is True


@pytest.mark.usefixtures("media_mode")
def test_plan_album_also_has_caption():
    """去掉按钮后，相册的说明可以直接挂在第一项上。"""
    p = tweet.plan(Tweet("1", "J", "j", "hi", [_m(), _m("video")]))
    assert p["mode"] == "media" and p["caption"] is True


@pytest.mark.usefixtures("media_mode")
def test_plan_long_text():
    p = tweet.plan(Tweet("1", "J", "j", "x" * 2000, [_m(), _m()]))
    assert p["mode"] == "media_long" and p["caption"] is False
    assert "…" not in p["html"]


@pytest.mark.usefixtures("media_mode")
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
@pytest.mark.usefixtures("media_mode")
def test_needs_relay(media, expect_relay):
    t = Tweet("1", "J", "j", "", media)
    assert (tweet.needs_relay(t, tweet.plan(t)) is not None) is expect_relay


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
async def test_direct_preview_options():
    import sender
    bot = FakeBot()
    t = Tweet("1", "J", "j", "hi", [WebMedia("photo", "https://a"), WebMedia("video", "https://b")])
    await sender.send_tweet_direct(bot, t, tweet.plan(t), 9, 5)
    assert [c[0] for c in bot.calls] == ["text"], "预览模式只发一条消息，不发媒体"
    kw = bot.calls[0][2]
    lp = kw["link_preview_options"]
    assert lp.url == "https://fxtwitter.com/j/status/1"
    assert lp.prefer_large_media is True and lp.show_above_text is True
    assert not lp.is_disabled
    assert kw["reply_parameters"].message_id == 5


@pytest.mark.asyncio
async def test_relay_path_also_handles_preview():
    """重启后从库里恢复的预览任务走 deliver_tweet，也得能发。"""
    import sender
    bot = FakeBot()
    t = Tweet("1", "J", "j", "hi", [WebMedia("photo", "https://a")])
    await sender.deliver_tweet(bot, None, None, tweet.plan(t), 9, 5)
    assert bot.calls[0][2]["link_preview_options"].show_above_text


@pytest.mark.asyncio
async def test_preview_fast_path_never_relays(qdb, monkeypatch):
    import taskqueue
    monkeypatch.setattr(tweet, "TWEET_MODE", "preview")
    r = _runner()
    t = Tweet("1", "J", "j", "hi", [WebMedia("video", "https://big", size=500 * 1024 ** 2)])
    tid = await qdb.add_task(42, "https://x.com/j/status/1", 9, 5)
    job = taskqueue.Job(tid, 42, "https://x.com/j/status/1", 9, 5, tweet=t)
    await r._run_tweet(job, "fast")
    assert [c[0] for c in r.bot.calls] == ["text"]
    assert r.slow.empty()
    assert (await qdb.get_task(tid))["state"] == "done"


@pytest.mark.asyncio
async def test_direct_text():
    import sender
    bot = FakeBot()
    t, extra = _tw([])
    await sender.send_tweet_direct(bot, t, extra, 9, 5)
    assert [c[0] for c in bot.calls] == ["text"]
    assert bot.calls[0][2]["link_preview_options"].is_disabled
    assert _no_buttons(bot)


@pytest.mark.usefixtures("media_mode")
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


@pytest.mark.usefixtures("media_mode")
@pytest.mark.asyncio
async def test_direct_video_carries_dimensions():
    import sender
    bot = FakeBot()
    t, extra = _tw([WebMedia("video", "https://v.mp4", 1280, 720, 12.7)])
    await sender.send_tweet_direct(bot, t, extra, 9, 5)
    kw = bot.calls[0][2]
    assert (kw["width"], kw["height"], kw["duration"]) == (1280, 720, 12)
    assert kw["supports_streaming"]


@pytest.mark.usefixtures("media_mode")
@pytest.mark.asyncio
async def test_direct_gif_as_animation():
    import sender
    bot = FakeBot()
    t, extra = _tw([WebMedia("gif", "https://g.mp4", 480, 270, 3)])
    await sender.send_tweet_direct(bot, t, extra, 9, 5)
    assert bot.calls[0][0] == "animation"


@pytest.mark.usefixtures("media_mode")
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


@pytest.mark.usefixtures("media_mode")
@pytest.mark.asyncio
async def test_direct_long_text_follows_media():
    import sender
    bot = FakeBot()
    t, extra = _tw([WebMedia("photo", "https://a"), WebMedia("photo", "https://b")],
                   text="x" * 2000)
    await sender.send_tweet_direct(bot, t, extra, 9, 5)
    assert [c[0] for c in bot.calls] == ["group", "text"]
    assert getattr(bot.calls[0][1][0], "caption", None) is None


@pytest.mark.usefixtures("media_mode")
@pytest.mark.asyncio
async def test_direct_rejection_raises_url_rejected():
    import sender
    bot = FakeBot(reject=True)
    t, extra = _tw([WebMedia("video", "https://big.mp4")])
    with pytest.raises(sender.UrlRejected):
        await sender.send_tweet_direct(bot, t, extra, 9, 5)
    assert bot.calls == [], "被拒时用户什么都不该收到，才能安全回退"


@pytest.mark.usefixtures("media_mode")
@pytest.mark.asyncio
async def test_relay_delivery_album_uses_copy_messages():
    import sender
    bot = FakeBot()
    _, extra = _tw([_m(), _m()])
    await sender.deliver_tweet(bot, -100, [1, 2], extra, 9, 5)
    assert [c[0] for c in bot.calls] == ["copies"]


@pytest.mark.usefixtures("media_mode")
@pytest.mark.asyncio
async def test_relay_delivery_split_copies_individually():
    import sender
    bot = FakeBot()
    _, extra = _tw([_m(), _m()])
    extra["split"] = True
    await sender.deliver_tweet(bot, -100, [1, 2], extra, 9, 5)
    assert [c[0] for c in bot.calls] == ["copy", "copy"]


@pytest.mark.usefixtures("media_mode")
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


@pytest.mark.usefixtures("media_mode")
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


@pytest.mark.usefixtures("media_mode")
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


@pytest.mark.usefixtures("media_mode")
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



# ================================================================ 预览检查（auto 模式）

from telethon.tl.types import (  # noqa: E402
    Document, MessageMediaEmpty, MessageMediaWebPage, Photo,
    WebPage, WebPageEmpty, WebPagePending,
)
from telethon.tl.types.messages import WebPagePreview  # noqa: E402


def _photo():
    return Photo(id=1, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1)


def _doc(mime):
    return Document(id=1, access_hash=1, file_reference=b"", date=None,
                    mime_type=mime, size=1, dc_id=1, attributes=[])


def _page(photo=None, document=None, embed=None, wrap=True):
    wp = WebPage(id=1, url="u", display_url="u", hash=0, photo=photo,
                 document=document, embed_url=embed)
    media = MessageMediaWebPage(webpage=wp)
    return WebPagePreview(media=media, chats=[], users=[]) if wrap else media


def _pending(wrap=True):
    media = MessageMediaWebPage(webpage=WebPagePending(id=1, date=None))
    return WebPagePreview(media=media, chats=[], users=[]) if wrap else media


PHOTO_TW = Tweet("1", "J", "j", "hi", [WebMedia("photo", "https://a")])
VIDEO_TW = Tweet("1", "J", "j", "hi", [WebMedia("video", "https://v")])


@pytest.mark.parametrize("wrap", [True, False], ids=["新协议包装", "旧协议直返"])
def test_judge_handles_both_protocol_shapes(wrap):
    assert tweet.judge_preview(_page(photo=_photo(), wrap=wrap), PHOTO_TW) == "ok"
    assert tweet.judge_preview(_pending(wrap=wrap), PHOTO_TW) == "pending"


def test_judge_photo_ok():
    assert tweet.judge_preview(_page(photo=_photo()), PHOTO_TW) == "ok"


def test_judge_empty_preview_is_missing():
    assert tweet.judge_preview(_page(), PHOTO_TW) == "missing"
    assert tweet.judge_preview(MessageMediaEmpty(), PHOTO_TW) == "missing"
    empty = WebPagePreview(media=MessageMediaWebPage(webpage=WebPageEmpty(id=1)),
                           chats=[], users=[])
    assert tweet.judge_preview(empty, PHOTO_TW) == "missing"


def test_judge_video_needs_video_document():
    assert tweet.judge_preview(_page(document=_doc("video/mp4")), VIDEO_TW) == "ok"


def test_judge_cover_only_counts_as_missing():
    """原帖有视频、预览只剩封面图：算失败。"""
    assert tweet.judge_preview(_page(photo=_photo()), VIDEO_TW) == "missing"


def test_judge_embed_player_not_trusted():
    """内嵌播放器是实时去源站拉的，原帖删了就放不了，不算有视频。"""
    assert tweet.judge_preview(
        _page(photo=_photo(), embed="https://fxtwitter.com/embed"), VIDEO_TW) == "missing"


@pytest.mark.parametrize("media,why", [
    ([WebMedia("video", "a"), WebMedia("video", "b")], "多个视频"),
    ([WebMedia("photo", "a"), WebMedia("video", "b")], "混排"),
    ([WebMedia("photo", "a"), WebMedia("gif", "b")], "混排"),
])
def test_prejudge_obvious_failures(media, why):
    assert why in tweet.prejudge_preview(Tweet("1", "J", "j", "", media))


def test_prejudge_passes_multi_photo_and_single_video():
    many = Tweet("1", "J", "j", "", [WebMedia("photo", str(i)) for i in range(4)])
    assert tweet.prejudge_preview(many) is None, "多图由 fxtwitter 拼图，不算失败"
    assert tweet.prejudge_preview(VIDEO_TW) is None


class SeqClient:
    """按顺序吐出预设结果的假 Telethon 客户端。"""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    async def __call__(self, req):
        assert type(req).__name__ == "GetWebPagePreviewRequest"
        assert req.message == "https://fxtwitter.com/j/status/1"
        self.calls += 1
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.mark.asyncio
async def test_probe_polls_until_ready():
    c = SeqClient(_pending(), _pending(), _page(photo=_photo()))
    notes = []
    v = await tweet.probe_preview(c, PHOTO_TW, timeout=5, interval=0.01,
                                  on_pending=lambda: notes.append(1))
    assert v == "ok" and c.calls == 3
    assert notes == [1], "「等待预览生成」只提示一次"


@pytest.mark.asyncio
async def test_probe_timeout_counts_as_missing():
    c = SeqClient(_pending())
    v = await tweet.probe_preview(c, PHOTO_TW, timeout=0.05, interval=0.01)
    assert v == "missing"


@pytest.mark.asyncio
async def test_probe_immediate_result_no_notice():
    notes = []
    v = await tweet.probe_preview(SeqClient(_page(photo=_photo())), PHOTO_TW,
                                  timeout=5, on_pending=lambda: notes.append(1))
    assert v == "ok" and notes == []


def test_timeout_configurable():
    """在子进程里验证：reload 会重建模块里的类，污染其他测试。"""
    import subprocess
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "TWEET_PREVIEW_TIMEOUT": "7.5", "TWEET_MODE": "MEDIA"}
    out = subprocess.run(
        [sys.executable, "-c", "import tweet; print(tweet.PREVIEW_TIMEOUT, tweet.TWEET_MODE)"],
        cwd=root, env=env, capture_output=True, text=True, check=True).stdout.split()
    assert out == ["7.5", "media"], "大小写也应被规整"


# ---------------------------------------------------------------- auto 调度

@pytest.fixture
def auto_mode(monkeypatch):
    monkeypatch.setattr(tweet, "TWEET_MODE", "auto")


def _use_client(monkeypatch, client):
    import taskqueue

    async def acquire(uid):
        if isinstance(client, Exception):
            raise client
        return client
    monkeypatch.setattr(taskqueue.POOL, "acquire", acquire)


async def _auto_job(qdb, tw):
    import taskqueue
    tid = await qdb.add_task(42, "https://x.com/j/status/1", 9, 5)
    return taskqueue.Job(tid, 42, "https://x.com/j/status/1", 9, 5, tweet=tw), tid


@pytest.mark.asyncio
@pytest.mark.usefixtures("auto_mode")
async def test_auto_preview_ok_sends_preview(qdb, monkeypatch):
    _use_client(monkeypatch, SeqClient(_page(photo=_photo())))
    r = _runner()
    job, tid = await _auto_job(qdb, PHOTO_TW)
    await r._run_tweet(job, "fast")
    assert [c[0] for c in r.bot.calls] == ["text"]
    assert r.bot.calls[0][2]["link_preview_options"].show_above_text
    assert (await qdb.get_task(tid))["state"] == "done"


@pytest.mark.asyncio
@pytest.mark.usefixtures("auto_mode")
async def test_auto_missing_switches_to_media(qdb, monkeypatch):
    """那条十几分钟视频的情况：预览里没视频 -> 改发原始视频。"""
    _use_client(monkeypatch, SeqClient(_page(photo=_photo())))
    r = _runner()
    job, tid = await _auto_job(qdb, VIDEO_TW)
    await r._run_tweet(job, "fast")
    assert [c[0] for c in r.bot.calls] == ["video"], "应改为 URL 直发视频"
    assert "<blockquote>" in r.bot.calls[0][2]["caption"]
    assert any("改为发送原始媒体" in x for x in r.said)


@pytest.mark.asyncio
@pytest.mark.usefixtures("auto_mode")
async def test_auto_missing_then_rejected_goes_relay_as_media(qdb, monkeypatch):
    """预览失败 -> 改 media -> URL 直发被拒 -> 慢通道。到慢通道时必须仍是 media，
    不能又被重新规划回 preview。"""
    _use_client(monkeypatch, SeqClient(_page()))
    r = _runner()
    r.bot = FakeBot(reject=True)
    job, tid = await _auto_job(qdb, VIDEO_TW)
    await r._run_tweet(job, "fast")
    assert r.slow.qsize() == 1
    queued = r.slow.get_nowait()
    assert queued.extra["mode"] == "media" and queued.extra["checked"]

    import json
    assert json.loads((await qdb.get_task(tid))["extra"])["mode"] == "media", \
        "落库的规划也得是 media，重启后才不会变回预览"


@pytest.mark.asyncio
@pytest.mark.usefixtures("auto_mode")
async def test_auto_timeout_switches_to_media(qdb, monkeypatch):
    monkeypatch.setattr(tweet, "PREVIEW_TIMEOUT", 0.05)
    _use_client(monkeypatch, SeqClient(_pending()))
    r = _runner()
    job, _ = await _auto_job(qdb, PHOTO_TW)
    await r._run_tweet(job, "fast")
    assert [c[0] for c in r.bot.calls] == ["photo"]
    assert any("等待预览生成" in x for x in r.said)


@pytest.mark.asyncio
@pytest.mark.usefixtures("auto_mode")
async def test_auto_check_error_falls_back_to_preview(qdb, monkeypatch):
    """检查本身出错（session 失效等）时按预览发，至少文字能到。"""
    _use_client(monkeypatch, RuntimeError("session 失效"))
    r = _runner()
    job, tid = await _auto_job(qdb, VIDEO_TW)
    await r._run_tweet(job, "fast")
    assert [c[0] for c in r.bot.calls] == ["text"]
    assert (await qdb.get_task(tid))["state"] == "done"


@pytest.mark.asyncio
@pytest.mark.usefixtures("auto_mode")
async def test_auto_prejudge_skips_probe(qdb, monkeypatch):
    """图视频混排一看就知道预览装不下，不用去问 Telegram。"""
    c = SeqClient(_page(photo=_photo()))
    _use_client(monkeypatch, c)
    r = _runner()
    tw = Tweet("1", "J", "j", "hi", [WebMedia("photo", "a"), WebMedia("video", "b")])
    job, _ = await _auto_job(qdb, tw)
    await r._run_tweet(job, "fast")
    assert c.calls == 0
    assert [c_[0] for c_ in r.bot.calls] == ["group"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("auto_mode")
async def test_auto_text_only_never_probes(qdb, monkeypatch):
    c = SeqClient(_page())
    _use_client(monkeypatch, c)
    r = _runner()
    job, _ = await _auto_job(qdb, Tweet("1", "J", "j", "hi"))
    await r._run_tweet(job, "fast")
    assert c.calls == 0 and [x[0] for x in r.bot.calls] == ["text"]


@pytest.mark.asyncio
async def test_forced_preview_never_probes(qdb, monkeypatch):
    monkeypatch.setattr(tweet, "TWEET_MODE", "preview")
    c = SeqClient(_page())
    _use_client(monkeypatch, c)
    r = _runner()
    job, _ = await _auto_job(qdb, VIDEO_TW)
    await r._run_tweet(job, "fast")
    assert c.calls == 0 and [x[0] for x in r.bot.calls] == ["text"]
