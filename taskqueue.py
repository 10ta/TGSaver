"""双通道任务调度。

  快通道  可直接 forward 的消息。服务端引用，毫秒级，并发 4，永不积压。
  慢通道  受保护内容，需要搬字节。并发 2，其中落盘路径再受磁盘信号量限制。

任务先统一进快通道做一次轻量探测（定位消息 + 判断是否受保护）。
不受保护的当场完成；受保护的改写 lane 后移交慢通道。这样一条大文件
永远不会堵住后面的纯文字请求。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from telethon.errors import FloodWaitError

import acl
import db
import fetcher
import sender
import streamer
import tweet
from config import CFG
from parser import MsgRef, ParseError, parse_link
from session_pool import POOL, NoSession

log = logging.getLogger("queue")

# 这些错误是确定性的，重试没有意义
FATAL = (fetcher.FetchError, ParseError, NoSession, streamer.TransferError,
         tweet.TweetError)

# 投递阶段的确定性错误：bot 不在频道里、被踢、频道不存在等。
# 这类错误重试 3 次只会把已经搬完的大文件重传 3 遍，必须当场判死。
DELIVER_FATAL = (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound)


def _explain_deliver(err: Exception, relay_chat: int) -> str:
    """把 Telegram 的英文报错翻成能直接照做的说明。

    判断顺序要点：具体原因必须排在泛化原因前面。
    "Forbidden: bot was kicked" 同时含有 forbidden 和 kicked，
    先匹配 forbidden 就会给出误导性的「权限不足」。
    """
    msg = str(err).lower()
    if "chat not found" in msg:
        return (f"bot 找不到中转频道 {relay_chat}。\n"
                f"常见原因：换过 BOT_TOKEN 但新 bot 没被加进该频道，"
                f"或 RELAY_CHANNEL_ID 填错了。\n"
                f"请把 bot 加为该频道管理员（Post Messages + Delete Messages）。")
    if "message to copy not found" in msg or "message not found" in msg:
        return "中转频道里的源消息已被删除，无法复制。"
    if "kicked" in msg or "not a member" in msg:
        return f"bot 已被移出中转频道 {relay_chat}，请重新加入并设为管理员。"
    if "not enough rights" in msg or "forbidden" in msg:
        return f"bot 在中转频道 {relay_chat} 里权限不足，请给它管理员权限。"
    return f"投递失败：{err}"


@dataclass
class Job:
    task_id: int
    owner_id: int
    link: str
    request_chat_id: int
    request_msg_id: int
    status_msg_id: Optional[int] = None
    attempts: int = 0
    # 探测阶段的成果，在进程内从快通道传给慢通道
    entity: Any = None
    msg: Any = None
    # 已搬进中转频道的结果。有值就说明字节已经过去了，
    # 后续失败只需重投递，绝不重传。
    relay_chat_id: Optional[int] = None
    relay_ids: Optional[list[int]] = None
    relay_is_album: bool = False
    extra: Optional[dict] = None   # 推文等非 Telegram 来源的呈现方式
    tweet: Any = None              # 进程内缓存的推文数据，快通道转慢通道时复用
    lane: str = "fast"          # 当前所在通道，killall 重新排队时要用
    last_edit: float = field(default=0.0)
    last_text: str = ""

    @property
    def relayed(self) -> bool:
        return bool(self.relay_ids)


class Runner:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.fast: asyncio.Queue[Job] = asyncio.Queue()
        self.slow: asyncio.Queue[Job] = asyncio.Queue()
        self.disk_sem = asyncio.Semaphore(CFG.disk_concurrency)
        self._tasks: list[asyncio.Task] = []
        self._active: dict[int, Job] = {}      # task_id -> Job，正在跑的
        self._started = time.time()

    # ------------------------------------------------------------ 生命周期

    def _spawn(self) -> None:
        for i in range(CFG.fast_concurrency):
            self._tasks.append(asyncio.create_task(self._worker(self.fast, "fast", i)))
        for i in range(CFG.slow_concurrency):
            self._tasks.append(asyncio.create_task(self._worker(self.slow, "slow", i)))

    async def start(self) -> None:
        self._spawn()
        await self._restore()

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _restore(self) -> None:
        """进程重启后捡回上次没跑完的任务。"""
        rows = await db.pending_tasks()
        for r in rows:
            job = Job(
                task_id=r["id"], owner_id=r["owner_id"], link=r["link"],
                request_chat_id=r["request_chat_id"],
                request_msg_id=r["request_msg_id"],
                status_msg_id=r["status_msg_id"], attempts=r["attempts"],
                relay_chat_id=r["relay_chat_id"],
                relay_ids=json.loads(r["relay_ids"]) if r["relay_ids"] else None,
                relay_is_album=bool(r["relay_is_album"]),
                extra=json.loads(r["extra"]) if r["extra"] else None,
            )
            job.lane = r["lane"] or "fast"
            (self.slow if job.lane == "slow" else self.fast).put_nowait(job)
        if rows:
            n_relayed = sum(1 for r in rows if r["state"] == "relayed")
            log.info("恢复 %d 个未完成任务（其中 %d 个已搬运完，只需重投递）",
                     len(rows), n_relayed)

    # ------------------------------------------------------------ 终止全部

    async def killall(self, owner_id: Optional[int] = None) -> dict[str, int]:
        """终止进行中的任务并清空队列。

        owner_id 为 None 表示全部，否则只终止该用户的。

        这里必须小心：worker 是通用的，没法只取消"某个人的"那一个。
        做法是把 worker 全部停掉（会中断正在进行的下载上传），然后把
        不在终止范围内的任务原样放回队列继续跑。早先的版本只在数据库
        侧按 scope 过滤，队列和 worker 却是一刀切 —— 普通用户一条
        /killall 会打断所有人的任务，而别人的任务在库里仍标着 running，
        直到进程重启才会被 recover_stuck 捡回来。
        """
        def mine(j: Job) -> bool:
            return owner_id is None or j.owner_id == owner_id

        # 1. 抽干队列，把别人的挑出来留着
        queued = 0
        spared: list[Job] = []
        for q in (self.fast, self.slow):
            while not q.empty():
                try:
                    j = q.get_nowait()
                except asyncio.QueueEmpty:
                    break
                q.task_done()
                if mine(j):
                    queued += 1
                else:
                    spared.append(j)

        # 2. 停掉所有 worker，中断进行中的传输
        active = list(self._active.values())
        killed = [j for j in active if mine(j)]
        survivors = [j for j in active if not mine(j)]
        await self.stop()
        self._active.clear()

        # 3. 落库 + 清理临时文件
        n, tmps = await db.cancel_all(owner_id)
        freed = streamer.cleanup_orphans(tmps)

        for job in killed:
            await self._say(job, "⛔ 已被 /killall 终止")

        # 4. 重新拉起 worker，把幸免的任务放回原来的通道
        self._spawn()
        for job in survivors + spared:
            await db.update_task(job.task_id, state="pending")
            await (self.slow if job.lane == "slow" else self.fast).put(job)

        if survivors or spared:
            log.info("killall：%d 个他人任务已放回队列继续", 
                     len(survivors) + len(spared))
        log.warning("killall：终止 %d 个进行中、%d 个排队中，清理临时文件 %d 个",
                    len(killed), queued, freed)
        return {"running": len(killed), "queued": queued,
                "marked": n, "files": freed,
                "spared": len(survivors) + len(spared)}

    # ------------------------------------------------------------ 提交

    async def submit(self, owner_id: int, link: str, chat_id: int,
                     msg_id: int) -> int:
        task_id = await db.add_task(owner_id, link, chat_id, msg_id)
        job = Job(task_id, owner_id, link, chat_id, msg_id)

        # 推文走 URL 直发时通常不到一秒就完成，先发一条「已排队」再删掉
        # 反而让结果出现得更慢。只有回退到中转时才显示进度。
        if not tweet.is_tweet(link):
            depth = self.fast.qsize() + self.slow.qsize()
            job.status_msg_id = await self._say(
                job, "已排队" + (f"（前面 {depth} 个）" if depth else "，正在处理…")
            )
            await db.update_task(task_id, status_msg_id=job.status_msg_id)
        await self.fast.put(job)
        return task_id

    def stats(self) -> dict[str, Any]:
        return {
            "fast": self.fast.qsize(),
            "slow": self.slow.qsize(),
            "active": len(self._active),
            "uptime": int(time.time() - self._started),
        }

    # ------------------------------------------------------------ worker

    async def _worker(self, q: asyncio.Queue, lane: str, idx: int) -> None:
        while True:
            try:
                job = await q.get()
            except asyncio.CancelledError:
                return
            self._active[job.task_id] = job
            try:
                await self._run(job, lane)
            except asyncio.CancelledError:
                log.info("task=%s 被取消", job.task_id)
                raise
            except Exception:  # noqa: BLE001
                log.exception("worker 未捕获异常 task=%s", job.task_id)
            finally:
                self._active.pop(job.task_id, None)
                q.task_done()

    async def _run(self, job: Job, lane: str) -> None:
        job.lane = lane
        await db.update_task(job.task_id, state="running", lane=lane)
        try:
            # 已经搬完了（上次投递失败或进程重启），直接重投递，不重传。
            if job.relayed:
                log.info("task=%s 已搬运，跳过传输直接投递", job.task_id)
                await self._deliver(job)
                return
            if tweet.is_tweet(job.link):
                await self._run_tweet(job, lane)
            elif lane == "fast":
                await self._run_fast(job)
            else:
                await self._run_slow(job)
        except FloodWaitError as e:
            await self._flood(job, lane, e.seconds)
        except TelegramRetryAfter as e:
            await self._flood(job, lane, int(e.retry_after))
        except DELIVER_FATAL as e:
            # 投递侧的确定性错误。字节可能已经搬完了，不重试，
            # 但保留 relay_ids —— 用户修好配置后重发链接即可秒完成。
            await self._fail(job, _explain_deliver(e, job.relay_chat_id or 0))
        except FATAL as e:
            await self._fail(job, str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("task=%s 失败", job.task_id)
            await self._retry_or_fail(job, lane, e)

    # ------------------------------------------------------------ 快通道

    async def _run_fast(self, job: Job) -> None:
        ref = parse_link(job.link)
        client = await POOL.acquire(acl.session_user(job.owner_id))
        async with POOL.lock_for(acl.session_user(job.owner_id)):
            entity, msg, protected = await fetcher.probe(client, ref)

            if protected:
                job.entity, job.msg = entity, msg
                await db.update_task(job.task_id, lane="slow", state="pending")
                size = fetcher.media_size(msg)
                hint = f"（{streamer.human_size(size)}）" if size else ""
                why = ("已指定 nosp，重新上传以去掉剧透遮罩"
                       if ref.force_reupload and
                       not fetcher.is_protected(entity, msg)
                       else "该内容禁止转存，转为搬运模式")
                await self._say(job, f"{why}{hint}…")
                await self.slow.put(job)
                return

            relay_ch = await acl.relay_channel_for(job.owner_id)
            res = await fetcher.relay(
                client, ref, relay_ch, entity=entity, msg=msg
            )
        await self._finish(job, res)

    # ------------------------------------------------------------ 慢通道

    async def _run_slow(self, job: Job) -> None:
        ref = parse_link(job.link)
        client = await POOL.acquire(acl.session_user(job.owner_id))

        entity, msg = job.entity, job.msg
        if msg is None:
            async with POOL.lock_for(acl.session_user(job.owner_id)):
                entity, msg, _ = await fetcher.probe(client, ref)

        size = fetcher.media_size(msg)
        need_disk = CFG.stream_max_size > 0 and size > CFG.stream_max_size

        async def register_tmp(path: Optional[str]) -> None:
            await db.update_task(job.task_id, tmp_path=path)

        async def body() -> None:
            relay_ch = await acl.relay_channel_for(job.owner_id)
            async with POOL.lock_for(acl.session_user(job.owner_id)):
                res = await fetcher.relay(
                    client, ref, relay_ch,
                    on_progress=lambda *a: self._progress(job, *a),
                    task_id=job.task_id, register_tmp=register_tmp,
                    entity=entity, msg=msg,
                )
            await self._finish(job, res)

        if need_disk:
            if self.disk_sem.locked():
                await self._say(job, "等待磁盘空闲…")
            async with self.disk_sem:
                await body()
        else:
            await body()

    # ------------------------------------------------------------ 收尾

    async def _run_tweet(self, job: Job, lane: str) -> None:
        """推文。

        auto 模式：先问 Telegram 预览里有没有媒体，齐全就发预览，
                   缺了（长视频、敏感内容、超时等）就改发原始媒体。
        快通道：预览或 URL 直发，由 Telegram 服务器去拉，本机零流量。
        慢通道：URL 直发被拒（太大 / 拉不到）时回退到这里，
                 由本机下载、user 账号上传到中转频道，不受大小限制。
        """
        ref = tweet.parse(job.link)
        tw = job.tweet or await tweet.fetch(ref)
        job.tweet = tw
        if job.extra is None or job.extra.get("kind") != "tweet":
            job.extra = tweet.plan(tw)

        if lane == "fast":
            if (job.extra["mode"] == "preview" and tweet.TWEET_MODE == "auto"
                    and not job.extra.get("checked")):
                await self._check_preview(job, tw)
            if job.extra["mode"] in ("media", "media_long"):
                # 大小未知的先 HEAD 一下，超限的直接走中转，
                # 不去触发一次可预见的「URL 直发被拒」
                await tweet.fill_sizes(tw)
            reason = tweet.needs_relay(tw, job.extra)
            if reason is None:
                try:
                    await sender.send_tweet_direct(
                        self.bot, tw, job.extra,
                        job.request_chat_id, job.request_msg_id)
                except sender.UrlRejected as e:
                    reason = "Telegram 拒绝直接拉取"
                    log.info("task=%s URL 直发被拒，回退中转: %s", job.task_id, e)
                else:
                    await db.update_task(job.task_id, state="done", error=None,
                                         extra=json.dumps(job.extra))
                    await db.bump_usage(job.owner_id, 0)
                    await self._drop_status(job)
                    return

            job.lane = "slow"
            await db.update_task(job.task_id, lane="slow", state="pending",
                                 extra=json.dumps(job.extra))
            await self._say(job, f"{reason}，改为服务器中转…")
            await self.slow.put(job)
            return

        # ---------- 慢通道：服务器中转 ----------
        if not tw.media or job.extra["mode"] == "preview":
            await self._deliver(job)
            return

        n = len(tw.media)
        await self._say(job, f"正在中转 {n} 个媒体…" if n > 1 else "正在中转媒体…")

        async def register_tmp(path: Optional[str]) -> None:
            await db.update_task(job.task_id, tmp_path=path)

        suid = acl.session_user(job.owner_id)
        client = await POOL.acquire(suid)
        relay_ch = await acl.relay_channel_for(job.owner_id)
        async with POOL.lock_for(suid):
            ids, nbytes, note = await streamer.relay_web_media(
                client, tw.media, relay_ch,
                caption_html=job.extra["html"] if job.extra.get("caption") else None,
                on_progress=lambda *a: self._progress(job, *a),
                task_id=job.task_id, register_tmp=register_tmp,
            )
        if note:
            job.extra["split"] = True      # 相册整组失败已逐条发，投递也逐条
        res = fetcher.Relayed(ids, len(ids) > 1 and not note, "B", nbytes, note)
        await self._finish(job, res)

    async def _check_preview(self, job: Job, tw: Any) -> None:
        """auto 模式：发送前确认预览里带齐了媒体，不齐就改成发原始媒体。

        检查本身失败（session 失效、网络问题）时维持预览——至少文字能送到。
        """
        why = tweet.prejudge_preview(tw)
        verdict = "missing" if why else None

        if verdict is None:
            try:
                client = await POOL.acquire(acl.session_user(job.owner_id))
                verdict = await tweet.probe_preview(
                    client, tw,
                    on_pending=lambda: self._say(job, "等待预览生成…"))
                if verdict == "missing":
                    why = "预览缺少媒体"
            except Exception as e:  # noqa: BLE001
                log.warning("task=%s 预览检查失败，按预览发送: %s", job.task_id, e)
                verdict = "ok"

        job.extra["checked"] = True
        if verdict == "missing":
            log.info("task=%s %s，改发原始媒体", job.task_id, why)
            job.extra = {**tweet.plan(tw, mode="media"), "checked": True}
            await self._say(job, f"{why}，改为发送原始媒体…")

    async def _finish(self, job: Job, res: fetcher.Relayed) -> None:
        """搬运完成。先把结果落库，再投递。

        两步分开的意义：投递失败时 relay_ids 已经存下来了，重试不会
        把几百 MB 重新下载上传一遍。
        """
        relay_ch = await acl.relay_channel_for(job.owner_id)
        job.relay_chat_id = relay_ch
        job.relay_ids = res.message_ids
        job.relay_is_album = res.is_album

        await db.update_task(
            job.task_id, state="relayed", tmp_path=None,
            bytes_moved=res.bytes_moved, error=None,
            relay_chat_id=relay_ch,
            relay_ids=json.dumps(res.message_ids),
            relay_is_album=1 if res.is_album else 0,
            extra=json.dumps(job.extra) if job.extra else None,
        )
        await self._deliver(job, note=res.note)

    async def _deliver(self, job: Job, note: str = "") -> None:
        """把中转频道里的结果复制给用户。可独立重跑。"""
        if job.extra and job.extra.get("kind") == "tweet":
            await sender.deliver_tweet(
                self.bot, job.relay_chat_id, job.relay_ids, job.extra,
                job.request_chat_id, job.request_msg_id,
            )
        else:
            await sender.deliver(
                self.bot, job.relay_chat_id, job.relay_ids,
                job.request_chat_id, job.request_msg_id, job.relay_is_album,
            )
        row = await db.get_task(job.task_id)
        moved = row["bytes_moved"] if row else 0
        await db.update_task(job.task_id, state="done", error=None)
        await db.bump_usage(job.owner_id, moved)

        if note:
            await self._say(job, "⚠️ " + note)
        else:
            await self._drop_status(job)

    async def _fail(self, job: Job, reason: str) -> None:
        await db.update_task(job.task_id, state="failed", error=reason[:500])
        await self._say(job, f"❌ {reason}")

    async def _retry_or_fail(self, job: Job, lane: str, err: Exception) -> None:
        job.attempts += 1
        await db.update_task(job.task_id, attempts=job.attempts)
        if job.attempts >= CFG.max_retries:
            await self._fail(job, f"重试 {job.attempts} 次仍失败：{err}")
            return
        delay = min(2 ** job.attempts, 30)
        await self._say(job, f"出错，{delay}s 后第 {job.attempts + 1} 次重试…")
        await asyncio.sleep(delay)
        await db.update_task(job.task_id, state="pending")
        await (self.slow if lane == "slow" else self.fast).put(job)

    async def _flood(self, job: Job, lane: str, seconds: int) -> None:
        """Telegram 明确告诉了等多久，就精确等多久，不做盲目退避。"""
        log.warning("FloodWait %ss task=%s", seconds, job.task_id)
        await self._say(job, f"触发 Telegram 限流，等待 {seconds}s 后继续…")
        await asyncio.sleep(seconds + 1)
        await db.update_task(job.task_id, state="pending")
        await (self.slow if lane == "slow" else self.fast).put(job)

    # ------------------------------------------------------------ 状态消息

    async def _say(self, job: Job, text: str) -> Optional[int]:
        """创建或原地更新状态消息。内容没变就不发请求。"""
        if text == job.last_text:
            return job.status_msg_id
        job.last_text = text
        try:
            if job.status_msg_id is None:
                from aiogram.types import ReplyParameters
                m = await self.bot.send_message(
                    job.request_chat_id, text,
                    reply_parameters=ReplyParameters(
                        message_id=job.request_msg_id),
                )
                job.status_msg_id = m.message_id
            else:
                await self.bot.edit_message_text(
                    text, chat_id=job.request_chat_id,
                    message_id=job.status_msg_id)
        except TelegramBadRequest:
            pass          # 消息被用户删了，或内容未变，忽略
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except Exception as e:  # noqa: BLE001
            log.debug("状态消息更新失败: %s", e)
        return job.status_msg_id

    async def _drop_status(self, job: Job) -> None:
        if job.status_msg_id is None:
            return
        try:
            await self.bot.delete_message(job.request_chat_id, job.status_msg_id)
        except Exception:  # noqa: BLE001
            pass
        job.status_msg_id = None

    def _progress(self, job: Job, phase: str, done: int, total: int,
                  elapsed: float, idx: int = 1, n: int = 1) -> None:
        """由传输层同步调用，这里节流后异步发出去。"""
        now = time.monotonic()
        if now - job.last_edit < CFG.progress_interval and done < total:
            return
        job.last_edit = now
        pct = (done / total * 100) if total else 0
        speed = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / speed if speed > 0 else 0
        head = f"[{idx}/{n}] " if n > 1 else ""
        text = (f"{head}{phase} {pct:.0f}% · "
                f"{streamer.human_size(done)}/{streamer.human_size(total)} · "
                f"{streamer.human_size(int(speed))}/s · 剩余 {_dur(eta)}")
        asyncio.create_task(self._say(job, text))


def _dur(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"
