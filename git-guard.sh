#!/usr/bin/env bash
# ============================================================
#  git-guard.sh —— 共用的隐私检查
#
#  被 git-init.sh 和 git-push.sh 引用，不单独执行。
#  所有检查都针对「即将被提交的内容」，而不是工作目录，
#  因为 .gitignore 已经挡掉的文件不需要再管。
# ============================================================

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; DIM=$'\033[2m'; RST=$'\033[0m'

ok()   { printf "  %s✓%s %s\n" "$GRN" "$RST" "$1"; }
warn() { printf "  %s!%s %s\n" "$YEL" "$RST" "$1"; }
die()  { printf "\n%s✗ %s%s\n\n" "$RED" "$1" "$RST"; exit 1; }
step() { printf "\n%s>%s %s\n" "$GRN" "$RST" "$1"; }

# ------------------------------------------------------------
# 1. .gitignore 必须存在且覆盖关键条目
# ------------------------------------------------------------
guard_gitignore() {
  [ -f .gitignore ] || die ".gitignore 不存在。没有它，一次 git add -A 就会把凭据推上去。"

  local missing=()
  for pat in '.env' '*.db' '*.session'; do
    grep -qxF "$pat" .gitignore || missing+=("$pat")
  done
  [ ${#missing[@]} -eq 0 ] || die ".gitignore 缺少条目：${missing[*]}"
  ok ".gitignore 覆盖 .env / *.db / *.session"
}

# ------------------------------------------------------------
# 2. 已被 git 跟踪的机密文件
#    .gitignore 对「已经入库」的文件无效，这是最常见的翻车点
# ------------------------------------------------------------
guard_tracked() {
  local tracked
  tracked=$(git ls-files | grep -E '(^|/)\.env$|\.db$|\.db-wal$|\.db-shm$|\.session$|\.key$' || true)
  if [ -n "$tracked" ]; then
    printf "\n%s以下机密文件已被 git 跟踪：%s\n" "$RED" "$RST"
    printf "    %s\n" $tracked
    printf "\n%s加 .gitignore 对它们无效，必须先移出索引：%s\n" "$YEL" "$RST"
    printf "    git rm --cached %s\n" "$(echo "$tracked" | tr '\n' ' ')"
    printf "\n如果它们已经进过 commit 历史，token 就算泄露了。\n"
    printf "请去 @BotFather 用 /revoke 换一个新 BOT_TOKEN。\n"
    die "已中止。"
  fi
  ok "无机密文件被 git 跟踪"
}

# ------------------------------------------------------------
# 3. 暂存区内容扫描
#    检查即将提交的每一行有没有长得像凭据的东西
# ------------------------------------------------------------
guard_staged_content() {
  local diff hits

  # 只看新增行，删除行无所谓
  diff=$(git diff --cached --no-color -U0 | grep '^+' | grep -v '^+++' || true)
  [ -n "$diff" ] || { ok "暂存区无新增内容"; return; }

  # --- Bot token: 8~10 位数字 + 冒号 + 35 位字符 ---
  hits=$(printf '%s\n' "$diff" | grep -E '[0-9]{8,10}:[A-Za-z0-9_-]{35}' || true)
  [ -z "$hits" ] || { printf "\n%s发现疑似 Bot Token：%s\n%s\n" "$RED" "$RST" "$hits"; \
                      die "别提交它。去 @BotFather 用 /revoke 换一个。"; }

  # --- Telethon StringSession: 超长的 base64 串 ---
  hits=$(printf '%s\n' "$diff" | grep -E '[A-Za-z0-9_-]{200,}' || true)
  [ -z "$hits" ] || { printf "\n%s发现超长字符串，疑似 session：%s\n%.200s...\n" "$RED" "$RST" "$hits"; \
                      die "别提交它。"; }

  # --- 填了值的 API_HASH / SECRET_KEY / TOKEN 赋值 ---
  hits=$(printf '%s\n' "$diff" \
         | grep -iE '^\+.*(API_HASH|SECRET_KEY|BOT_TOKEN|API_ID)[[:space:]]*[=:][[:space:]]*["'"'"']?[A-Za-z0-9_/+-]{6,}' \
         | grep -viE '(getenv|os\.environ|_req\(|CFG\.|cfg\.|\.env\.example|<|your|xxx|填)' || true)
  [ -z "$hits" ] || { printf "\n%s发现疑似硬编码凭据：%s\n%s\n" "$RED" "$RST" "$hits"; \
                      die "把它挪到 .env 里去。"; }

  ok "暂存区内容无凭据特征"

  # --- 32 位十六进制（api_hash 的形状）只警告，可能是正常哈希 ---
  hits=$(printf '%s\n' "$diff" | grep -oE '\b[0-9a-f]{32}\b' | sort -u || true)
  if [ -n "$hits" ]; then
    warn "出现 32 位十六进制串（api_hash 也是这个形状），请自行确认："
    printf "      %s\n" $hits
  fi
}

# ------------------------------------------------------------
# 4. .env.example 的必填项必须留空
# ------------------------------------------------------------
guard_example() {
  [ -f .env.example ] || return 0
  local filled
  filled=$(grep -E '^(API_ID|API_HASH|BOT_TOKEN|OWNER_ID|RELAY_CHANNEL_ID|SECRET_KEY)=.+' .env.example || true)
  if [ -n "$filled" ]; then
    printf "\n%s.env.example 里的必填项被填了真实值：%s\n%s\n" "$RED" "$RST" "$filled"
    die "这个文件会公开，必须留空。"
  fi
  ok ".env.example 必填项为空"
}

# ------------------------------------------------------------
# 5. 最终确认：把实际要提交的文件列给人看
# ------------------------------------------------------------
guard_review() {
  local files count
  files=$(git diff --cached --name-status)
  [ -n "$files" ] || die "暂存区是空的，没有要提交的内容。"

  count=$(printf '%s\n' "$files" | wc -l)
  printf "\n%s即将提交 %s 个文件：%s\n" "$DIM" "$count" "$RST"
  printf '%s\n' "$files" | sed 's/^/    /'

  if [ "${ASSUME_YES:-0}" = "1" ]; then
    printf "\n%s(--yes 已指定，跳过确认)%s\n" "$DIM" "$RST"
    return 0
  fi
  printf "\n确认无误？[y/N] "
  read -r ans
  [ "$ans" = "y" ] || [ "$ans" = "Y" ] || die "已取消。"
}

# ------------------------------------------------------------
# 全套检查
# ------------------------------------------------------------
run_all_guards() {
  step "隐私检查"
  guard_gitignore
  guard_example
  guard_tracked
  guard_staged_content
}
