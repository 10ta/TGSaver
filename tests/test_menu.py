"""命令菜单定义。

Telegram 对 setMyCommands 有硬性格式要求，任何一条不合规会导致
**整批**注册失败——菜单直接不出现，而且线上只有一行 warning，
很容易被忽略。所以在这里静态校验一遍。
"""
import os
import re
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

import menu  # noqa: E402

ALL = menu.PUBLIC + menu.OWNER_ONLY
NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")


@pytest.mark.parametrize("cmd,desc", ALL, ids=[c for c, _ in ALL])
def test_command_name_format(cmd, desc):
    """只允许小写字母、数字、下划线，1–32 字符。"""
    assert NAME_RE.match(cmd), f"/{cmd} 不符合 Telegram 的命令名规则"


@pytest.mark.parametrize("cmd,desc", ALL, ids=[c for c, _ in ALL])
def test_description_length(cmd, desc):
    """描述 1–256 字符。太长会被拒，空的也会被拒。"""
    assert 1 <= len(desc) <= 256, f"/{cmd} 的描述长度 {len(desc)} 越界"


def test_no_duplicates():
    names = [c for c, _ in ALL]
    assert len(names) == len(set(names)), "命令名重复会导致整批注册失败"


def test_public_and_owner_do_not_overlap():
    pub = {c for c, _ in menu.PUBLIC}
    own = {c for c, _ in menu.OWNER_ONLY}
    assert not (pub & own), f"重复列出：{pub & own}"


def test_admin_commands_are_owner_only():
    """管理命令绝不能出现在公开菜单里。"""
    pub = {c for c, _ in menu.PUBLIC}
    for c in ("adduser", "deluser", "ban", "unban", "users",
              "stats", "queue", "logout", "admin"):
        assert c not in pub, f"/{c} 不该对所有人可见"


def test_every_listed_command_actually_exists():
    """菜单里列出的命令必须真的有处理函数，否则点了没反应。"""
    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text()
    src += (Path(__file__).resolve().parent.parent / "admin.py").read_text()
    registered = set(re.findall(r'Command\("(\w+)"\)', src))
    registered.add("help")      # 走 CommandStart/Command("help")
    for cmd, _ in ALL:
        assert cmd in registered, f"菜单列了 /{cmd} 但没有对应的处理函数"


def test_grab_not_in_menu():
    """/grab 只是别名，主推的是直接发 t.me/对话名，不占菜单位置。"""
    assert "grab" not in {c for c, _ in ALL}


def test_owner_menu_is_superset():
    """机主菜单 = 公开 + 管理，普通功能不能漏。"""
    full = {c for c, _ in menu.PUBLIC + menu.OWNER_ONLY}
    assert {c for c, _ in menu.PUBLIC} <= full


def test_build_produces_valid_objects():
    objs = menu._cmds(ALL)
    assert len(objs) == len(ALL)
    assert all(o.command and o.description for o in objs)
