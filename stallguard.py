"""卡死看门狗。

线上遇到过：抓取 10 条，状态停在「正在搬运 1/10…」，十个小时没有任何动静，
任务一直标着 running，也不报错。Telethon 的单个请求没有总超时 —— 连到别的数据中心
下载文件的那条连接如果半死不活，请求就永远等下去。

做法：每个任务记一个「最近有进展」的时间。传输层每收到 / 发出一块数据就报一次进展
（进度回调），排队等磁盘名额时也定期报。超过 STALL_TIMEOUT 秒没有任何进展，
就判定卡死：记下卡在哪一步，取消这次运行，抛 Stalled 交给正常的重试流程。
重试前把这个账号的连接换成新的，免得再撞上同一条坏连接。

只看「有没有进展」，不看总时长：两小时的大文件只要一直在动就不会被打断。
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("stall")

# 当前任务的「报进展」函数。看门狗创建被看管的任务时设置，
# 传输层（streamer）里随时可以 touch()，不需要层层传参。
_touch: contextvars.ContextVar[Optional[Callable[[], None]]] = \
    contextvars.ContextVar("tgsaver_touch", default=None)


def touch() -> None:
    """报告「还活着、有进展」。不在看管之下时什么也不做。"""
    cb = _touch.get()
    if cb is not None:
        cb()


class Stalled(RuntimeError):
    """长时间没有任何进展，判定卡死。走普通的重试流程。"""

    def __init__(self, idle: float, where: str) -> None:
        self.idle = int(idle)
        self.where = where
        super().__init__(f"传输卡住 {self.idle}s 没有任何进展（卡在 {where}），已中断")


def await_chain(task: "asyncio.Task") -> list[tuple[str, bool, str]]:
    """顺着 await 链往里走，列出这个任务此刻停在哪些函数里（由外到内）。

    每项是 (文件:行 函数, 是否第三方库, 请求类型)。
    asyncio 自带的 Task.get_stack() 只给最外层一帧，看不出卡在哪个请求上。
    Telethon 的请求帧里有局部变量 request，顺手把请求类型也带上。
    """
    out: list[tuple[str, bool, str]] = []
    coro: Any = task.get_coro()
    seen = 0
    while coro is not None and seen < 60:
        seen += 1
        frame = (getattr(coro, "cr_frame", None) or getattr(coro, "ag_frame", None)
                 or getattr(coro, "gi_frame", None))
        if frame is not None:
            path = frame.f_code.co_filename
            label = f"{os.path.basename(path)}:{frame.f_lineno} {frame.f_code.co_name}"
            third = "site-packages" in path or "/asyncio/" in path
            req = frame.f_locals.get("request")
            rname = type(req).__name__ if req is not None else ""
            out.append((label, third, rname if rname.endswith("Request") else ""))
        coro = (getattr(coro, "cr_await", None) or getattr(coro, "ag_await", None)
                or getattr(coro, "gi_yieldfrom", None))
    return out


def summarize(chain: list[tuple[str, bool, str]]) -> str:
    """给用户看的一句话：我们自己代码里最里面那一步 + 最里面的 Telegram 请求。"""
    ours = [c for c in chain if not c[1]]
    req = next((c[2] for c in reversed(chain) if c[2]), "")
    if ours:
        step = ours[-1][0].split(" ", 1)[1]
    else:
        step = chain[-1][0] if chain else "未知位置"
    return step + (f" → {req}" if req else "")


async def guard(coro: Awaitable[Any], timeout: float, label: str = "",
                on_activity: Optional[Callable[[], None]] = None) -> Any:
    """运行 coro；超过 timeout 秒没有任何 touch() 就取消它并抛 Stalled。

    timeout <= 0 时不看管，直接运行。外面被取消（/killall）时连带取消里面。
    """
    if timeout <= 0:
        return await coro

    last = time.monotonic()

    def bump() -> None:
        nonlocal last
        last = time.monotonic()
        if on_activity is not None:
            on_activity()

    ctx = contextvars.copy_context()
    ctx.run(_touch.set, bump)
    task = asyncio.get_running_loop().create_task(_wrap(coro), context=ctx)
    step = max(0.05, min(30.0, timeout / 4))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=step)
            if task in done:
                return task.result()
            idle = time.monotonic() - last
            if idle >= timeout:
                chain = await_chain(task)
                where = summarize(chain)
                log.error("%s 卡住 %ds 没有进展，卡在：%s\n  %s", label or "任务",
                          int(idle), where,
                          "\n  ".join(c[0] + (f" [{c[2]}]" if c[2] else "")
                                      for c in chain))
                task.cancel()
                try:
                    await task
                except BaseException:  # noqa: BLE001
                    pass
                raise Stalled(idle, where)
    except asyncio.CancelledError:
        task.cancel()
        raise


async def _wrap(coro: Awaitable[Any]) -> Any:
    return await coro
