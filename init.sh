#!/usr/bin/env bash
# ============================================================
#  init.sh —— 一键部署 / 修复
#
#  以 root 运行：
#      ./init.sh
#
#  可重复执行：已经做好的步骤会跳过，只补缺失的部分。
#  服务出问题时也可以再跑一遍，它会把权限、目录、属主全部校正。
#
#  这个脚本固化了首次部署实际踩到的每一个坑，见文件末尾清单。
# ============================================================
# 这些脚本用到数组、$'...' 等 bash 语法。无论被 sh / dash 调用，
# 还是可执行位丢失导致 shebang 未生效，都切回 bash 重新执行。
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail

APP_USER="tgsaver"
APP_DIR="/opt/tgsaver"
TMP_DIR="/var/cache/tgsaver"
SERVICE="tgsaver"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; DIM=$'\033[2m'; RST=$'\033[0m'
ok()   { printf "  %s✓%s %s\n" "$GRN" "$RST" "$1"; }
skip() { printf "  %s·%s %s %s(已就绪)%s\n" "$DIM" "$RST" "$1" "$DIM" "$RST"; }
warn() { printf "  %s!%s %s\n" "$YEL" "$RST" "$1"; }
die()  { printf "\n%s✗ %s%s\n\n" "$RED" "$1" "$RST"; exit 1; }
step() { printf "\n%s>%s %s\n" "$GRN" "$RST" "$1"; }

[ "$(id -u)" = "0" ] || die "请用 root 运行：./init.sh"

printf "\n%s═══ TgSaver 部署 ═══%s\n" "$GRN" "$RST"

# ------------------------------------------------------------
# 1. 主机名解析
#    sudo 每次都刷 "unable to resolve host" 警告，源头是
#    /etc/hostname 里的名字在 /etc/hosts 里没有对应条目
# ------------------------------------------------------------
step "主机名解析"
HN=$(cat /etc/hostname 2>/dev/null | tr -d '[:space:]')
if [ -n "$HN" ] && ! grep -q "[[:space:]]$HN\b" /etc/hosts; then
  printf "127.0.1.1\t%s\t%s\n" "$HN" "${HN%%.*}" >> /etc/hosts
  ok "已为 $HN 添加 /etc/hosts 条目"
else
  skip "$HN"
fi

# ------------------------------------------------------------
# 2. 系统依赖
#    Debian 把 venv 拆成独立包，不装就报 ensurepip is not available
#    cryptg 要编译 C 扩展，缺 build-essential / python3-dev 会失败
# ------------------------------------------------------------
step "系统依赖"
NEED=()
for p in python3-venv python3-full build-essential python3-dev; do
  dpkg -s "$p" >/dev/null 2>&1 || NEED+=("$p")
done
command -v sudo >/dev/null 2>&1 || NEED+=(sudo)

if [ ${#NEED[@]} -gt 0 ]; then
  printf "  安装：%s\n" "${NEED[*]}"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${NEED[@]}"
  ok "系统依赖已装齐"
else
  skip "python3-venv / build-essential / python3-dev / sudo"
fi

# ------------------------------------------------------------
# 3. 专用系统用户
#    这个程序持有 Telegram 账号的完整读写权限，
#    用 root 跑会让 systemd 的 ProtectSystem 等加固全部失效
# ------------------------------------------------------------
step "专用用户 $APP_USER"
if id "$APP_USER" >/dev/null 2>&1; then
  skip "用户已存在"
else
  adduser --system --group --home "$APP_DIR" --no-create-home "$APP_USER"
  ok "已创建系统用户"
fi

# ------------------------------------------------------------
# 4. 代码位置
# ------------------------------------------------------------
step "代码目录"
if [ ! -f "$APP_DIR/bot.py" ]; then
  FOUND=$(find /root /home /opt -maxdepth 3 -name taskqueue.py 2>/dev/null \
          | head -1 | xargs -r dirname)
  if [ -n "$FOUND" ] && [ "$FOUND" != "$APP_DIR" ]; then
    warn "在 $FOUND 找到代码，移动到 $APP_DIR"
    [ -d "$APP_DIR" ] && rmdir "$APP_DIR" 2>/dev/null || true
    mv "$FOUND" "$APP_DIR"
    ok "已移动"
  else
    die "在 $APP_DIR 找不到代码。请先把仓库放到这里：
    git clone <你的仓库地址> $APP_DIR"
  fi
else
  skip "$APP_DIR"
fi
cd "$APP_DIR"

# ------------------------------------------------------------
# 5. 虚拟环境
#    venv 内部记录绝对路径，目录搬过家就必须重建
# ------------------------------------------------------------
step "虚拟环境"
REBUILD=0
if [ ! -x venv/bin/python ]; then
  REBUILD=1
elif ! venv/bin/python -c '' 2>/dev/null; then
  warn "venv 路径失效（目录可能搬过家），重建"
  REBUILD=1
fi

if [ "$REBUILD" = "1" ]; then
  rm -rf venv
  python3 -m venv venv
  ./venv/bin/pip install -q --upgrade pip
  ./venv/bin/pip install -q -r requirements.txt
  ok "venv 已建好，依赖已装"
else
  skip "venv 可用"
  ./venv/bin/pip install -q -r requirements.txt 2>/dev/null || true
fi

# ------------------------------------------------------------
# 6. 配置文件
# ------------------------------------------------------------
step "配置"
if [ ! -f .env ]; then
  cp .env.example .env
  KEY=$(./venv/bin/python genkey.py)
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$KEY|" .env
  ok "已生成 .env 并填入 SECRET_KEY"
  NEED_EDIT=1
else
  skip ".env 已存在"
  grep -q '^SECRET_KEY=.\+' .env || {
    KEY=$(./venv/bin/python genkey.py)
    sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$KEY|" .env
    ok "补填了 SECRET_KEY"
  }
  NEED_EDIT=0
fi

MISSING=()
for k in API_ID API_HASH BOT_TOKEN OWNER_ID RELAY_CHANNEL_ID; do
  grep -q "^$k=.\+" .env || MISSING+=("$k")
done

# ------------------------------------------------------------
# 7. 目录与权限
#    login.py 若用 root 跑过，生成的 tgsaver.db 属主是 root，
#    systemd 以 tgsaver 身份启动后读不了，会一直报"未登录"
# ------------------------------------------------------------
step "目录与权限"
mkdir -p "$TMP_DIR"
chown "$APP_USER:$APP_USER" "$TMP_DIR"
# 老配置里的 /tmp/tgsaver 会在系统重启后消失，导致服务起不来，迁走
if grep -q '^TMP_DIR=/tmp/tgsaver$' "$APP_DIR/.env" 2>/dev/null; then
  sed -i 's|^TMP_DIR=/tmp/tgsaver$|TMP_DIR=/var/cache/tgsaver|' "$APP_DIR/.env"
  ok ".env 里的 TMP_DIR 已从 /tmp/tgsaver 迁到 /var/cache/tgsaver"
fi
rm -rf /tmp/tgsaver 2>/dev/null || true
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"
[ -f "$APP_DIR/tgsaver.db" ] && chmod 600 "$APP_DIR/tgsaver.db"
chmod +x "$APP_DIR"/*.sh 2>/dev/null || true
ok "属主 $APP_USER，.env 与数据库 600"

# ------------------------------------------------------------
# 8. systemd
# ------------------------------------------------------------
step "systemd"
if [ ! -f "/etc/systemd/system/$SERVICE.service" ] || \
   ! cmp -s tgsaver.service "/etc/systemd/system/$SERVICE.service"; then
  cp tgsaver.service "/etc/systemd/system/$SERVICE.service"
  systemctl daemon-reload
  ok "单元文件已安装"
else
  skip "单元文件已是最新"
fi

# ------------------------------------------------------------
# 9. 下一步
# ------------------------------------------------------------
if [ ${#MISSING[@]} -gt 0 ]; then
  printf "\n%s═══ 还需要你填配置 ═══%s\n\n" "$YEL" "$RST"
  printf "  缺少：%s\n\n" "${MISSING[*]}"
  cat <<'EOF'
  获取方式：
    API_ID / API_HASH   https://my.telegram.org -> API development tools
                        （申请后每个账号只能有一个应用，先去 /apps 看看
                          是不是其实已经建好了）
    BOT_TOKEN           @BotFather -> /newbot
    OWNER_ID            @userinfobot 发一句话，记下它回的数字
    RELAY_CHANNEL_ID    新建【私有】频道 -> Administrators -> 加入你的 bot
                        权限勾上 Post Messages + Delete Messages
                        频道里发条消息，复制链接 t.me/c/1234567890/2
                        填 -100 + 那串数字 = -1001234567890

  编辑：
      nano /opt/tgsaver/.env

  填完后再跑一次本脚本：
      /opt/tgsaver/init.sh
EOF
  printf "\n"
  exit 0
fi
ok "5 项必填配置齐全"

# ------------------------------------------------------------
# 10. 登录
#     必须用 tgsaver 身份跑，否则数据库属主错误
#     验证码要在别的设备上看，绝不能贴进任何 Telegram 聊天窗口
# ------------------------------------------------------------
step "登录凭据"
HAS_SESSION=$(sudo -u "$APP_USER" ./venv/bin/python - <<'PY' 2>/dev/null || echo no
import asyncio, db
async def m():
    await db.init()
    r = await db.get_user(__import__("config").CFG.owner_id)
    print("yes" if r and r["session_status"] == "ok" else "no")
    await db.close()
asyncio.run(m())
PY
)

if [ "$HAS_SESSION" = "yes" ]; then
  skip "已登录"
else
  printf "\n"
  warn "需要登录 Telegram。注意两点："
  printf "      1. 这里填【手机号】，不是 bot token\n"
  printf "      2. 验证码在别的设备上看，%s绝不要贴进任何 Telegram 窗口%s\n" "$RED" "$RST"
  printf "         （Telegram 会判定为钓鱼并立即作废）\n\n"
  sudo -u "$APP_USER" ./venv/bin/python login.py
  chmod 600 "$APP_DIR/tgsaver.db" 2>/dev/null || true
  chown "$APP_USER:$APP_USER" "$APP_DIR/tgsaver.db" 2>/dev/null || true
fi

# ------------------------------------------------------------
# 11. 启动
# ------------------------------------------------------------
step "启动服务"
systemctl enable -q "$SERVICE" 2>/dev/null || true
systemctl restart "$SERVICE"
sleep 3

if systemctl is-active --quiet "$SERVICE"; then
  ok "服务运行中"
  printf "\n%s最近日志：%s\n" "$DIM" "$RST"
  journalctl -u "$SERVICE" -n 12 --no-pager -o cat | sed 's/^/    /'
  printf "\n%s═══ 完成 ═══%s\n" "$GRN" "$RST"
  printf "  给 bot 发一条公开频道链接试试，例如 https://t.me/durov/1\n"
  printf "  实时日志：%sjournalctl -u %s -f%s\n\n" "$DIM" "$SERVICE" "$RST"
else
  printf "\n%s服务启动失败，日志如下：%s\n\n" "$RED" "$RST"
  journalctl -u "$SERVICE" -n 30 --no-pager -o cat | sed 's/^/    /'
  printf "\n"
  die "请根据上面的报错检查 .env 配置。"
fi

# ============================================================
#  本脚本处理的坑（都是实际部署时踩过的）
#
#  1. sudo 报 unable to resolve host        -> 补 /etc/hosts
#  2. venv 报 ensurepip is not available    -> 装 python3-venv
#  3. cryptg 编译失败                        -> 装 build-essential python3-dev
#  4. 目录搬家后 venv 失效                    -> 检测并重建
#  5. root 跑 login.py 导致数据库属主错误      -> 强制用 tgsaver 身份
#  6. Unit tgsaver.service not found         -> 自动安装单元文件
#  7. 系统重启后 /tmp 清空，/tmp/tgsaver 不存在导致服务起不来
#     -> 临时目录改为 systemd 管理的 /var/cache/tgsaver（CacheDirectory）
#  8. .env / 数据库权限过宽                    -> 统一 chmod 600
#  9. 把 bot token 填进 login.py 的手机号提示  -> 启动前明确提示
# ============================================================
