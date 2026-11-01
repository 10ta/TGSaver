#!/usr/bin/env bash
# uninstall.sh —— 卸载 TgSaver（在服务器上以 root 运行：/opt/tgsaver/uninstall.sh）
#
# 停掉并删除 systemd 服务，删除临时目录 /var/cache/tgsaver，去掉 git safe.directory 例外。
# 代码目录 /opt/tgsaver（含 .env 和 tgsaver.db —— 加密后的 Telegram 登录凭据）和系统用户 tgsaver 保留，
# 重新部署时 ./init.sh 直接沿用、不用重新登录；最后打印彻底删除的命令（不替你删）。
# 影响：bot 停止响应，进行中的任务中断（数据库还在，重新部署后启动时会捡回未完成的任务）。
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "请用 root 运行" >&2; exit 1; }

APP_USER=tgsaver APP_DIR=/opt/tgsaver TMP_DIR=/var/cache/tgsaver SERVICE=tgsaver

# 保留的东西：列出还在的，并打印彻底删除的命令（不替你删）
kept() {   # kept 用户名或- 路径...
  local user=$1; shift
  local p have=()
  for p in "$@"; do [[ -e "$p" ]] && have+=("$p"); done
  [[ -n "$user" && "$user" != - ]] && id "$user" >/dev/null 2>&1 || user=""
  (( ${#have[@]} )) || [[ -n "$user" ]] || return 0
  echo "保留（重新部署时直接沿用）：${have[*]}${user:+ · 系统用户 $user}"
  echo "确定不再需要的话，手动删除："
  (( ${#have[@]} )) && echo "  rm -rf ${have[*]}"
  [[ -n "$user" ]] && echo "  userdel $user"
  return 0
}

systemctl disable --now "$SERVICE" >/dev/null 2>&1 || true
rm -f "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
rm -rf "$TMP_DIR"
git config --global --fixed-value --unset-all safe.directory "$APP_DIR" 2>/dev/null || true
echo "✔ TgSaver 已卸载（服务、$TMP_DIR、git safe.directory 例外）"
kept "$APP_USER" "$APP_DIR"
if [[ -e "$APP_DIR" ]]; then
  echo "删掉 $APP_DIR 之后，这台服务器的登录在 Telegram「设置 → 设备」里还挂着，去那里终止它"
fi
