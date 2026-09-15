#!/usr/bin/env bash
# ============================================================
#  git-push.sh —— 日常更新代码后推送
#
#  用法：
#      ./git-push.sh "fix: 修正相册分组"
#      ./git-push.sh                      # 不给说明则交互输入
#
#  可选：
#      --yes        跳过确认提示
#      --no-test    跳过 pytest
#      --amend      追加到上一个提交（还没 push 时用）
#
#  每次都会跑一遍隐私检查和测试，通不过不推。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=git-guard.sh
. ./git-guard.sh

MSG=""
ASSUME_YES=0
RUN_TEST=1
AMEND=0

while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y)     ASSUME_YES=1; shift ;;
    --no-test|-n) RUN_TEST=0; shift ;;
    --amend)      AMEND=1; shift ;;
    -*)           die "未知选项：$1" ;;
    *)            MSG="$1"; shift ;;
  esac
done
export ASSUME_YES

[ -d .git ] || die "这里还不是 git 仓库。首次推送请用 ./git-init.sh"

BRANCH=$(git rev-parse --abbrev-ref HEAD)
printf "\n%s═══ TgSaver 推送更新 ═══%s\n  分支：%s\n" "$GRN" "$RST" "$BRANCH"

# ------------------------------------------------------------
if [ -z "$(git status --porcelain)" ]; then
  printf "\n%s工作区干净，没有要提交的改动。%s\n\n" "$YEL" "$RST"
  exit 0
fi

# ------------------------------------------------------------
if [ "$RUN_TEST" = "1" ]; then
  step "跑测试"
  PY=""
  for c in ./venv/bin/python python3 python; do
    command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
  done
  if [ -n "$PY" ] && "$PY" -c "import pytest" 2>/dev/null; then
    if "$PY" -m pytest -q; then
      ok "测试通过"
    else
      die "测试未通过。修好再推，或用 --no-test 跳过。"
    fi
  else
    warn "未找到 pytest，跳过（pip install pytest pytest-asyncio）"
  fi
fi

# ------------------------------------------------------------
step "暂存改动"
git add -A
ok "已暂存"

# ------------------------------------------------------------
run_all_guards
guard_review

# ------------------------------------------------------------
step "提交"
if [ "$AMEND" = "1" ]; then
  git commit -q --amend --no-edit
  ok "已追加到上一个提交：$(git rev-parse --short HEAD)"
else
  if [ -z "$MSG" ]; then
    printf "提交说明："
    read -r MSG
    [ -n "$MSG" ] || die "说明不能为空。"
  fi
  git commit -q -m "$MSG"
  ok "提交完成：$(git rev-parse --short HEAD)"
fi

# ------------------------------------------------------------
step "推送"
if [ "$AMEND" = "1" ]; then
  warn "amend 改写了历史，需要强推"
  git push --force-with-lease origin "$BRANCH"
else
  git push origin "$BRANCH"
fi

printf "\n%s═══ 完成 ═══%s\n\n" "$GRN" "$RST"
