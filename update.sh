#!/usr/bin/env bash
# update.sh —— TgSaver 一键更新（在服务器上以 root 运行：/opt/tgsaver/update.sh）
#
#   update.sh            提交本地改动 → 同步代码 → 推送本地提交 → init.sh（依赖、权限、systemd 单元、重启）→ 确认已重启
#   update.sh --status   只看版本和服务状态
#   update.sh --no-pull  不碰 git，直接部署磁盘上的代码（比如刚 git apply 了补丁、还不想提交）
#   update.sh --no-test  推送前不跑测试
#
# 往 GitHub 推之前一定先过隐私检查，再跑 pytest，任何一项不过都不推：
#   .gitignore 覆盖 .env / *.db / *.session；没有机密文件被 git 跟踪；.env.example 必填项为空；
#   要推上去的新增内容里没有 Bot Token、session 串、硬编码凭据（32 位十六进制只提醒）。
# 工作区有改动（手改了代码）时列出文件、问你要提交说明，留空就不提交。
# 还没有 GitHub 远端（代码是拷过来的、不是 clone 的）时问你仓库地址，做首次推送。
# 代码目录就是部署目录（/opt/tgsaver），属主是 tgsaver。脚本会自动加 git safe.directory 例外，
# 结束时（包括失败、Ctrl-C）把属主还给 tgsaver，.env 和数据库保持 600。
# 需要重新登录 Telegram 时会停下来提示，去手动运行 ./init.sh 完成登录。卸载用 ./uninstall.sh。
# ==== 公共部分 v1（各仓库的 update.sh 里这一段完全相同，改的时候一起改）====
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail
SELF="$(readlink -f "$0")"
ARGS=("$@")
if [[ -t 1 ]]; then B=$'\e[1m' G=$'\e[32m' Y=$'\e[33m' R=$'\e[31m' D=$'\e[2m' N=$'\e[0m'; else B='' G='' Y='' R='' D='' N=''; fi
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '%s✔%s %s\n' "$G" "$N" "$*"; }
warn() { printf '%s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '%s✘ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

NO_PULL=0 NO_TEST=0 STATUS=0
for a in "$@"; do
  case "$a" in
    --no-pull) NO_PULL=1 ;;   # 不同步 git、不检查未提交的改动，直接部署磁盘上的代码（比如刚手动打了补丁）
    --no-test) NO_TEST=1 ;;   # 跳过推送前的测试 / 构建检查
    --status)  STATUS=1 ;;    # 只看状态：仓库版本、已部署版本、服务状态，不做任何改动
    -h|--help) sed -n '2,/^# ====/{/^# ====/d;s/^# \{0,1\}//;p}' "$SELF"; exit 0 ;;
    *) die "未知参数：$a（可用：--status / --no-pull / --no-test / --help）" ;;
  esac
done

need_root() { [[ $EUID -eq 0 ]] || die "请用 root 运行：sudo bash $SELF ${ARGS[*]:-}"; }
short() { git rev-parse --short HEAD; }

# sync_repo [允许有改动的文件...]
#   工作区有未提交的改动 → 退出（列出的生成文件除外）；本地和远端分叉 → 退出
#   落后 → 快进拉取；领先 → 记在 AHEAD 里，由调用方决定推送还是只提示
#   拉取后如果 update.sh 自己变了，用新版本重新执行一遍
AHEAD=0
sync_repo() {
  git rev-parse --git-dir >/dev/null 2>&1 || die "$PWD 不是 git 仓库"
  local br bad f behind
  br=$(git rev-parse --abbrev-ref HEAD)
  [[ "$br" != HEAD ]] || die "当前不在任何分支上（detached HEAD），先 git checkout 回主分支"
  if (( NO_PULL )); then   # 明确要求部署磁盘上的代码（比如刚 git apply 了补丁），不检查改动、不同步
    warn "--no-pull：不同步，直接部署磁盘上的代码（$br @ $(short)$(git diff --quiet || echo '，含未提交的改动')）"
    return 0
  fi
  bad=""
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    printf '%s\n' "$@" | grep -qxF -- "$f" || bad+="    $f"$'\n'
  done < <(git status --porcelain --untracked-files=no | cut -c4-)
  [[ -z "$bad" ]] || die "有未提交的改动，先提交或还原（git stash / git checkout -- 文件）：
$bad"
  [[ -n "${UPDATE_REEXEC:-}" ]] && { ok "代码已同步（$br @ $(short)）"; return 0; }   # 刚刚已经同步过（新版本 update.sh 重新执行 / 根目录脚本调用）
  git fetch -q origin "$br" || die "git fetch 失败（网络不通，或远端没有 $br 分支）"
  read -r behind AHEAD < <(git rev-list --left-right --count "origin/$br...HEAD")
  (( behind && AHEAD )) && die "本地和远端分叉了：本地多 $AHEAD 个提交，远端多 $behind 个。
    手动处理：git pull --rebase，解决冲突后再运行本脚本"
  if (( behind )); then
    local before_self after_self old
    old=$(short)
    before_self=$(sha256sum "$SELF" | cut -c1-64)
    git merge -q --ff-only "origin/$br"
    ok "已拉取 $behind 个新提交：$old → $(short)"
    after_self=$(sha256sum "$SELF" 2>/dev/null | cut -c1-64 || true)
    if [[ "$before_self" != "$after_self" && -z "${UPDATE_REEXEC:-}" ]]; then
      warn "update.sh 本身也更新了，用新版本重新执行"
      UPDATE_REEXEC=1 exec bash "$SELF" "${ARGS[@]}"
    fi
  elif (( ! AHEAD )); then
    ok "代码已是最新（$br @ $(short)）"
  fi
  return 0
}

# push_ahead：把本地领先的提交推到远端（推送即部署的项目用）
push_ahead() {
  local br; br=$(git rev-parse --abbrev-ref HEAD)
  (( NO_PULL )) && return 0
  read -r _ AHEAD < <(git rev-list --left-right --count "origin/$br...HEAD")
  if (( AHEAD )); then
    git push -q origin "$br" || die "git push 失败"
    ok "已推送 $AHEAD 个提交到 origin/$br"
  fi
}

# 部署记录：/opt/<应用>/DEPLOYED 里写「commit 时间」，--status 和下次更新时用来对比
mark_deployed() { printf '%s %s\n' "$(short)" "$(date '+%F %T')" > "$1"; }
deployed_of()   { [[ -f "$1" ]] && cut -d' ' -f1 "$1" || echo "未记录"; }

# same_file 仓库里的文件 已部署的文件：两边不一致就退出
same_file() { cmp -s "$1" "$2" || die "$2 和仓库里的 $1 不一致，部署没有生效"; }

# restarted 服务名：确认服务在运行，而且是本脚本开始之后启动的（不是旧进程）
T0=$(date +%s)
restarted() {
  local s=$1 ts
  systemctl is-active --quiet "$s" || die "$s 没有在运行，看日志：journalctl -u $s -n 50 --no-pager"
  ts=$(date -d "$(systemctl show -p ActiveEnterTimestamp --value "$s")" +%s 2>/dev/null || echo 0)
  (( ts >= T0 - 2 )) || die "$s 还是旧进程（启动于 $(systemctl show -p ActiveEnterTimestamp --value "$s")），没有被重启。手动：systemctl restart $s"
  ok "$s 已重启，运行中"
}
# ==== 公共部分结束 ====

# ---------------------------------------------------------------- 隐私检查
# 检查的是「要进 GitHub 的东西」，.gitignore 已经挡掉的文件不用管。

# guard_repo：.gitignore 完整、没有机密文件被跟踪、.env.example 必填项为空
guard_repo() {
  local pat missing=() tracked filled
  [[ -f .gitignore ]] || die ".gitignore 不存在。没有它，一次 git add -A 就会把凭据推上去"
  for pat in '.env' '*.db' '*.session'; do grep -qxF "$pat" .gitignore || missing+=("$pat"); done
  (( ${#missing[@]} == 0 )) || die ".gitignore 缺少条目：${missing[*]}"
  # .gitignore 对已经入库的文件无效，这是最常见的翻车点
  tracked=$(git ls-files | grep -E '(^|/)\.env$|\.db$|\.db-wal$|\.db-shm$|\.session$|\.key$' || true)
  [[ -z "$tracked" ]] || die "这些机密文件被 git 跟踪了：
$(sed 's/^/    /' <<<"$tracked")
  先移出索引：git rm --cached $(tr '\n' ' ' <<<"$tracked")
  如果它们已经进过提交历史就等同泄露：去 @BotFather 用 /revoke 换新的 BOT_TOKEN"
  if [[ -f .env.example ]]; then
    filled=$(grep -E '^(API_ID|API_HASH|BOT_TOKEN|OWNER_ID|RELAY_CHANNEL_ID|SECRET_KEY)=.+' .env.example || true)
    [[ -z "$filled" ]] || die ".env.example 里的必填项填了真实值（这个文件是公开的，必须留空）：
$filled"
  fi
  return 0
}

# guard_content <git diff 参数...>：扫描新增行里像凭据的东西（删除行无所谓）
guard_content() {
  local added hits
  added=$(git diff --no-color -U0 "$@" | grep '^+' | grep -v '^+++' || true)
  [[ -n "$added" ]] || return 0
  hits=$(grep -E '[0-9]{8,10}:[A-Za-z0-9_-]{35}' <<<"$added" || true)
  [[ -z "$hits" ]] || die "发现疑似 Bot Token，别推它，去 @BotFather 用 /revoke 换一个：
$hits"
  hits=$(grep -E '[A-Za-z0-9_-]{200,}' <<<"$added" | cut -c1-200 || true)
  [[ -z "$hits" ]] || die "发现超长字符串，疑似 Telethon session：
$hits…"
  hits=$(grep -iE '^\+.*(API_HASH|SECRET_KEY|BOT_TOKEN|API_ID)[[:space:]]*[=:][[:space:]]*["'"'"']?[A-Za-z0-9_/+-]{6,}' <<<"$added" \
         | grep -viE '(getenv|os\.environ|_req\(|CFG\.|cfg\.|\.env\.example|<|your|xxx|填)' || true)
  [[ -z "$hits" ]] || die "发现疑似硬编码凭据，挪到 .env 里去：
$hits"
  hits=$(grep -oE '\b[0-9a-f]{32}\b' <<<"$added" | sort -u || true)
  [[ -z "$hits" ]] || warn "出现 32 位十六进制串（api_hash 也是这个形状），自己确认一下：$(tr '\n' ' ' <<<"$hits")"
  return 0
}

# stage_all：git add -A 后再查一遍；不过就把暂存撤掉，改动原样留在工作区
stage_all() {
  guard_repo
  git add -A
  ( guard_repo; guard_content --cached ) || { git reset -q; exit 1; }
  git diff --cached --name-status | sed 's/^/    /'
}

# ask 提示 变量名：读一行，剥掉终端混进来的控制序列（比如深浅色主题通知 ESC[?997;1n）
ask() {
  local _a
  while read -r -t 0.05 -n 256 _ 2>/dev/null; do :; done   # 先清掉输入缓冲里残留的东西
  read -r -p "$1" _a || _a=""
  printf -v "$2" '%s' "$(printf '%s' "$_a" | sed $'s/\033\\[[0-9;?]*[A-Za-z]//g')"
}

# run_tests：推送前跑 pytest；先按 requirements.txt 补依赖，免得新加的依赖让测试误报
run_tests() {
  (( NO_TEST )) && { warn "--no-test：推送前不跑测试"; return 0; }
  [[ -x venv/bin/python ]] || { warn "还没有 venv（没跑过 init.sh），跳过测试"; return 0; }
  venv/bin/pip install -q -r requirements.txt || die "pip install 失败"
  venv/bin/python -c 'import pytest, pytest_asyncio' 2>/dev/null || {
    warn "venv 里没装 pytest，跳过测试（要测：venv/bin/pip install pytest pytest-asyncio）"; return 0; }
  venv/bin/python -m pytest -q -p no:cacheprovider || die "测试没过，不推送。修好再跑；确定要推就加 --no-test"
  ok "测试通过"
}

# setup_remote：还没有 origin（代码是拷过来的）时问仓库地址，建仓库并首次推送
setup_remote() {
  local url br
  git remote get-url origin >/dev/null 2>&1 && return 0
  [[ -t 0 ]] || die "还没有配置 GitHub 远端。在终端里运行 ./update.sh，它会问你仓库地址"
  step "首次推送到 GitHub"
  echo "先在 GitHub 建一个空仓库（不要勾 Add README / .gitignore / License）"
  ask "仓库地址（如 git@github.com:你的用户名/tgsaver.git）：" url
  [[ -n "$url" ]] || die "没填地址，已退出"
  [[ -d .git ]] || git init -q -b main
  git config user.name >/dev/null && git config user.email >/dev/null \
    || die "还没设置 git 身份：git config --global user.name 你的名字; git config --global user.email 你的邮箱"
  stage_all
  if ! git diff --cached --quiet; then
    local ans; ask "以上文件提交并推送到 $url？[y/N] " ans
    [[ "${ans// }" =~ ^[yY]([eE][sS])?$ ]] || { git reset -q; die "已取消"; }
    git commit -q -m "feat: TgSaver 初始版本"
  fi
  br=$(git rev-parse --abbrev-ref HEAD)
  guard_content "$(git hash-object -t tree /dev/null)" HEAD
  run_tests
  git remote add origin "$url"
  git push -q -u origin "$br" || { git remote remove origin; die "git push 失败（地址、权限或网络），远端没有保存，修好再跑"; }
  ok "已推送到 $url（$br）"
}

# commit_local：工作区有改动时列出文件、问提交说明；留空就不提交（之后 sync_repo 会拦下未提交的改动）
commit_local() {
  local msg
  [[ -n "$(git status --porcelain)" ]] || return 0
  [[ -t 0 ]] || return 0
  step "提交本地改动"
  stage_all
  ask "提交说明（留空 = 不提交）：" msg
  if [[ -z "${msg// }" ]]; then git reset -q; warn "没有提交"; return 0; fi
  git commit -q -m "$msg"
  ok "已提交：$msg"
}

need_root
cd "$(dirname "$SELF")"
APP_USER=tgsaver SERVICE=tgsaver
git config --global --get-all safe.directory 2>/dev/null | grep -qxF "$PWD" \
  || git config --global --add safe.directory "$PWD"
# 用 trap 而不是写在末尾：推送失败、检查中止、Ctrl-C 都会跳过末尾语句，属主停在 root 服务就读不到 .env
trap 'id "$APP_USER" >/dev/null 2>&1 && chown -R "$APP_USER:$APP_USER" "$PWD" 2>/dev/null; chmod 600 .env tgsaver.db 2>/dev/null; true' EXIT

if (( STATUS )); then
  br=$(git rev-parse --abbrev-ref HEAD)
  git fetch -q origin "$br" 2>/dev/null || warn "fetch 失败，远端版本可能不是最新"
  echo "本地     $br @ $(short)（代码目录就是部署目录）"
  echo "远端     origin/$br @ $(git rev-parse --short "origin/$br" 2>/dev/null || echo 无)"
  echo "服务     $(systemctl is-active $SERVICE 2>/dev/null || true)，启动于 $(systemctl show -p ActiveEnterTimestamp --value $SERVICE)"
  echo "代码     最后一次提交于 $(git log -1 --format=%cd --date='format:%F %T')"
  exit 0
fi

if (( ! NO_PULL )); then
  setup_remote
  commit_local
fi

step "同步代码"
sync_repo
if (( AHEAD )); then
  br=$(git rev-parse --abbrev-ref HEAD)
  step "推送本地的 $AHEAD 个提交"
  git log --oneline "origin/$br..HEAD" | sed 's/^/    /'
  guard_repo
  guard_content "origin/$br..HEAD"
  ok "隐私检查通过"
  run_tests
  push_ahead
fi

step "部署（init.sh）"
bash init.sh </dev/null || die "init.sh 失败，见上面的输出。要重新登录 Telegram 的话手动运行 ./init.sh"
restarted "$SERVICE"
ok "TgSaver 已更新到 $(short)"
