"""部署配置的一致性。

这些都是「代码没错、但服务起不来」的问题，只能靠静态检查拦。
"""
import configparser
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "h")
os.environ.setdefault("BOT_TOKEN", "t")
os.environ.setdefault("OWNER_ID", "42")
os.environ.setdefault("RELAY_CHANNEL_ID", "-1001111111111")
os.environ.setdefault("SECRET_KEY", Fernet.generate_key().decode())


def _service():
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(ROOT / "tgsaver.service")
    return cp["Service"]


def test_no_writable_path_under_tmp():
    """/tmp 在系统重启后被清空。ReadWritePaths 里的路径不存在时，
    systemd 建挂载命名空间就失败（status=226/NAMESPACE），服务根本起不来。"""
    for path in _service().get("ReadWritePaths", "").split():
        assert not path.lstrip("-").startswith("/tmp"), \
            f"{path} 在 /tmp 下，重启后会消失"


def test_tmp_dir_is_systemd_managed():
    """临时目录必须由 systemd 自动创建，不能指望手动建。"""
    assert _service().get("CacheDirectory") == "tgsaver"


def test_code_default_matches_service():
    """代码默认的临时目录必须就是 systemd 创建的那个，否则两边对不上。"""
    from config import CFG
    if "TMP_DIR" not in os.environ:
        assert str(CFG.tmp_dir) == "/var/cache/tgsaver"


def test_env_example_matches_service():
    text = (ROOT / ".env.example").read_text()
    line = next(l for l in text.splitlines() if l.startswith("TMP_DIR="))
    assert line == "TMP_DIR=/var/cache/tgsaver"


def test_init_migrates_old_tmp_setting():
    """已部署的机器上 .env 里多半还写着 /tmp/tgsaver，init.sh 要帮着迁走。"""
    src = (ROOT / "init.sh").read_text()
    assert "TMP_DIR=/tmp/tgsaver" in src and "/var/cache/tgsaver" in src


def test_service_user_matches_init():
    assert _service().get("User") == "tgsaver"
    assert 'APP_USER="tgsaver"' in (ROOT / "init.sh").read_text()


# ------------------------------------------------- 运维脚本只有三个

def test_only_three_shell_scripts():
    assert sorted(p.name for p in ROOT.glob("*.sh")) == ["init.sh", "uninstall.sh", "update.sh"]


def test_shell_scripts_parse():
    import subprocess
    for name in ("init.sh", "update.sh", "uninstall.sh"):
        subprocess.run(["bash", "-n", str(ROOT / name)], check=True)


def test_update_guards_before_every_push():
    """隐私检查原来在 git-guard.sh 里，合进 update.sh 后不能丢。"""
    src = (ROOT / "update.sh").read_text()
    for needle in ("[0-9]{8,10}:[A-Za-z0-9_-]{35}",   # Bot Token
                   "[A-Za-z0-9_-]{200,}",              # session 串
                   "git rm --cached", ".env.example"):
        assert needle in src, needle
    tail = src.split("# ==== 公共部分结束 ====")[1]
    main = tail[tail.index("\nneed_root"):]
    assert main.index("guard_content") < main.index("push_ahead")
    assert "guard_content" in tail[tail.index("setup_remote() {"):tail.index("git push -q -u")]


def test_uninstall_keeps_credentials():
    """卸载不能顺手删掉 .env / 数据库：重新部署要靠它们免登录。"""
    src = (ROOT / "uninstall.sh").read_text()
    body = src.split('set -euo pipefail', 1)[1]
    assert "rm -rf \"$APP_DIR\"" not in body and "rm -rf /opt/tgsaver" not in body
    assert "disable --now" in body
