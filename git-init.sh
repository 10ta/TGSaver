#!/usr/bin/env bash
# ============================================================
#  git-init.sh —— 首次初始化并推送到 GitHub
#
#  用法：
#      ./git-init.sh git@github.com:你的用户名/tgsaver.git
#      ./git-init.sh https://github.com/你的用户名/tgsaver.git
#
#  可选：
#      --yes     跳过确认提示
#      --branch  指定分支名（默认 main）
#
#  在 GitHub 上先建好空仓库（不要勾 Add README / .gitignore /
#  License，否则首次推送会冲突），再跑这个。
# ============================================================
# 这些脚本用到数组、$'...' 等 bash 语法。无论被 sh / dash 调用，
# 还是可执行位丢失导致 shebang 未生效，都切回 bash 重新执行。
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=git-guard.sh
. ./git-guard.sh

REMOTE=""
BRANCH="main"
ASSUME_YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y)   ASSUME_YES=1; shift ;;
    --branch|-b) BRANCH="$2"; shift 2 ;;
    -*)         die "未知选项：$1" ;;
    *)          REMOTE="$1"; shift ;;
  esac
done
export ASSUME_YES

[ -n "$REMOTE" ] || die "用法：./git-init.sh <仓库地址> [--branch main] [--yes]"

printf "\n%s═══ TgSaver 首次推送 ═══%s\n" "$GRN" "$RST"
printf "  远程：%s\n  分支：%s\n" "$REMOTE" "$BRANCH"

# ------------------------------------------------------------
step "仓库属主"
guard_ownership

step "初始化仓库"
if [ -d .git ]; then
  warn "已存在 .git，跳过 init（若要重来请先 rm -rf .git）"
else
  git init -q
  ok "git init 完成"
fi

git symbolic-ref HEAD "refs/heads/$BRANCH" 2>/dev/null || true
ok "分支设为 $BRANCH"

# 身份未配置时 commit 会失败，提前查
if ! git config user.email >/dev/null 2>&1; then
  warn "未配置 git 身份，请先执行："
  printf '      git config --global user.name  "你的名字"\n'
  printf '      git config --global user.email "你的邮箱"\n'
  die "已中止。"
fi
ok "git 身份：$(git config user.name) <$(git config user.email)>"

# ------------------------------------------------------------
step "暂存文件"
git add -A
ok "已暂存（.gitignore 中的文件自动排除）"

# ------------------------------------------------------------
run_all_guards
guard_review

# ------------------------------------------------------------
step "提交"
git commit -q -m "feat: TgSaver 初始版本

Telegram 消息归档 bot：
- 中转频道方案绕开 Bot API 50MB 上传限制
- 受保护内容流式转存，零磁盘占用
- 快慢双通道队列，大文件不阻塞轻量请求
- 任务持久化，崩溃自动续跑"
ok "提交完成：$(git rev-parse --short HEAD)"

# ------------------------------------------------------------
step "推送"
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REMOTE"
  ok "已更新 origin"
else
  git remote add origin "$REMOTE"
  ok "已添加 origin"
fi

git push -u origin "$BRANCH"

printf "\n%s═══ 完成 ═══%s\n" "$GRN" "$RST"
printf "  仓库已推送。以后更新代码用：%s./git-push.sh \"提交说明\"%s\n\n" "$DIM" "$RST"
