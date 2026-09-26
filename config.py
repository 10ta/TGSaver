"""配置加载。所有配置集中在此，其它模块只 import CFG。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise SystemExit(
            f"\n[配置缺失] .env 中的 {name} 没有填写。\n"
            f"请参考 .env.example 中对应条目的说明。\n"
        )
    return v


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    owner_id: int
    relay_channel_id: int
    secret_key: str

    db_path: Path
    tmp_dir: Path

    max_upload_size: int
    stream_max_size: int
    disk_headroom: float

    fast_concurrency: int
    slow_concurrency: int
    disk_concurrency: int

    session_idle_timeout: int
    session_pool_size: int
    progress_interval: int
    max_retries: int
    log_level: str


CFG = Config(
    api_id=int(_req("API_ID")),
    api_hash=_req("API_HASH"),
    bot_token=_req("BOT_TOKEN"),
    owner_id=int(_req("OWNER_ID")),
    relay_channel_id=int(_req("RELAY_CHANNEL_ID")),
    secret_key=_req("SECRET_KEY"),
    db_path=Path(os.getenv("DB_PATH", "./tgsaver.db")).expanduser(),
    tmp_dir=Path(os.getenv("TMP_DIR", "/var/cache/tgsaver")).expanduser(),
    max_upload_size=_int("MAX_UPLOAD_SIZE", 2097152000),
    stream_max_size=_int("STREAM_MAX_SIZE", 1073741824),
    disk_headroom=_float("DISK_HEADROOM", 1.2),
    fast_concurrency=_int("FAST_CONCURRENCY", 4),
    slow_concurrency=_int("SLOW_CONCURRENCY", 2),
    disk_concurrency=_int("DISK_CONCURRENCY", 1),
    session_idle_timeout=_int("SESSION_IDLE_TIMEOUT", 600),
    session_pool_size=_int("SESSION_POOL_SIZE", 20),
    progress_interval=_int("PROGRESS_INTERVAL", 5),
    max_retries=_int("MAX_RETRIES", 3),
    log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
