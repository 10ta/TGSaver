# TgSaver

把 Telegram 消息链接发给它，原样取回来。

受保护内容照样能存，大文件不受 50MB 限制，纯文字请求不会被大视频堵住。

> Send it a Telegram message link, get the message back untouched.
> Handles forward-restricted content and files far beyond the Bot API's 50 MB cap.

---

## 它解决什么问题

Telegram 上想留存一条消息，通常有三道坎：

1. **Bot 读不到。** Bot API 没有「按链接取消息」的接口，bot 也不可能加入你所有的私有频道。
2. **发不出去。** Bot 上传硬上限 50MB，一个 349MB 的视频直接卡死。
3. **转不了。** 有些频道开了「禁止转存」，服务端会拒绝 forward。

TgSaver 用一个私有中转频道把这三道坎一次绕开：

```
你的 user 账号 ──forward──> 中转频道 ──copyMessage──> 你
   (MTProto)                           (Bot API)
```

`copyMessage` 是**服务端引用**，不算 bot 上传，所以完全不受 50MB 约束，
而且天然不带「转发自」抬头，形态与原消息一致。普通内容全程在 Telegram
服务端完成，本机不下载、不上传、不落盘——2GB 视频和一行文字耗时相同。

受保护的内容没法走服务端，字节必须过一次本机。但「过」不等于「落盘」：
下载分片直接喂给上传，中间只留一个 16MB 的有界缓冲。

---

## 特性

- **三条传输路径自动选择**，普通内容零流量零磁盘
- **双通道队列**，大文件搬运不阻塞后面的轻量请求
- **完整保真**：文本 entities、相册分组、视频时长分辨率、缩略图、
  spoiler 标记、文件名、caption 全部保留
- **任务持久化**，进程崩溃重启自动续跑，遗留临时文件自动清理
- **凭据加密存储**，密钥与数据库分离
- **依赖只有 5 个**，无 Docker、无 Redis、无 Celery
- **多用户预留**，改一行开关即可开启

---

## 三条传输路径

| 路径 | 触发条件 | 磁盘 | 流量 | 说明 |
|:--:|---|:--:|:--:|---|
| **A** 直转 | 普通内容 | 0 | 0 | 全程服务端引用 |
| **B** 流式 | 受保护，< 1 GB | 0 | 2× | 边下边传，内存峰值 ~30 MB |
| **C** 落盘 | 受保护，≥ 1 GB | ≤ 文件大小 | 2× | 上传失败可低成本重试 |

分界线由 `STREAM_MAX_SIZE` 控制，设为 `0` 则全部强制流式。

路径 C 落盘前检查 `剩余空间 > 文件 × 1.2`，不足直接失败而非写满盘；
同一时刻最多 1 个任务走这条路。

---

## 双通道队列

```
链接 ──> 快通道(并发4) ──探测──┬── 未受保护 ──> 直转 ──> 投递
                               └── 受保护 ───> 慢通道(并发2) ──> 搬运 ──> 投递
                                                    └─ 落盘路径再限流为 1
```

所有任务先进快通道做一次轻量探测（定位消息 + 判断是否受保护）。不受保护的
当场做完；受保护的移交慢通道。所以一个 1.5 GB 的视频在传的时候，后面的纯
文字链接照样秒回。

`FloodWaitError` 按 Telegram 返回的确切秒数等待，不做盲目退避。

---

## 快速开始

### 1. 准备凭据

| 需要 | 从哪来 |
|---|---|
| `API_ID` / `API_HASH` | [my.telegram.org](https://my.telegram.org) → API development tools |
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `OWNER_ID` | [@userinfobot](https://t.me/userinfobot) 发一句话，它回的数字 |

`API_ID`/`API_HASH` 服务的是 MTProto 通道（Telethon 用它取消息），
`BOT_TOKEN` 服务的是 Bot API 通道，两者缺一不可。

### 2. 建中转频道

1. Telegram → New Channel → 选 **Private**
2. 频道设置 → Administrators → Add Admin → 加入你的 bot
   权限勾上 **Post Messages** 和 **Delete Messages**
3. 在频道里随便发一条消息，复制消息链接，形如 `https://t.me/c/1234567890/2`
4. 频道 id 即 `-100` + 链接里那串数字 → `-1001234567890`

启动时会自动验证这个频道，配错直接报错退出，不会等到跑任务才发现。

### 3. 一键部署

```bash
git clone https://github.com/<你的用户名>/tgsaver.git /opt/tgsaver
cd /opt/tgsaver
./init.sh
```

`init.sh` 以 root 运行，会自动完成：装系统依赖、建专用用户、建 venv、
生成 `SECRET_KEY`、校正权限属主、安装 systemd 单元、引导登录、启动服务。

**可重复执行。** 配置没填完它会停下来告诉你缺什么，填完再跑一次即可；
服务出问题时也可以再跑一遍，它会把目录、属主、权限全部校正。

如果想手动来：

```bash
apt install python3-venv python3-full build-essential python3-dev
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env && ./venv/bin/python genkey.py   # 粘进 SECRET_KEY
nano .env && chmod 600 .env
adduser --system --group --home /opt/tgsaver tgsaver
mkdir -p /tmp/tgsaver && chown tgsaver:tgsaver /tmp/tgsaver
chown -R tgsaver:tgsaver /opt/tgsaver
sudo -u tgsaver ./venv/bin/python login.py    # 必须用 tgsaver 身份
```

登录时有两个坑：

> **这里填手机号，不是 bot token。** Telethon 的提示文案把两种都列了，
> 但 TgSaver 取消息必须用你的个人账号——bot 账号读不到任意消息链接。
>
> **验证码请从其它设备的 Telegram 上读，用眼睛看、手敲进终端。**
> 一旦粘贴进任何 Telegram 聊天窗口（包括转发给自己、存收藏夹），
> Telegram 会判定为钓鱼并立即作废。

### 4. 验证

```bash
systemctl status tgsaver
journalctl -u tgsaver -f
```

先发一条公开频道链接验证路径 A（例如 `https://t.me/durov/1`），
再找个禁止转存的频道验证路径 B。

---

## 配置

必填 5 项，其余都有默认值。完整说明见 [`.env.example`](.env.example)。

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAX_UPLOAD_SIZE` | 2 GB | 账号上传上限。Premium 可改 `4294967296` |
| `STREAM_MAX_SIZE` | 1 GB | 超过则走落盘路径，`0` = 全部流式 |
| `TMP_DIR` | `/tmp/tgsaver` | 落盘路径用的临时目录 |
| `FAST_CONCURRENCY` | 4 | 快通道并发 |
| `SLOW_CONCURRENCY` | 2 | 慢通道并发 |
| `DISK_CONCURRENCY` | 1 | 同时走落盘路径的任务数 |
| `PROGRESS_INTERVAL` | 5 | 进度条刷新秒数，太小会触发限流 |

---

## 支持的链接形态

```
https://t.me/name/123              公开频道 / 群
https://t.me/name/45/123           公开论坛群（45 是话题 id，不是消息 id）
https://t.me/c/1234567890/123      私有
https://t.me/c/1234567890/45/123   私有论坛群
https://t.me/b/botname/123         bot 频道
tg://privatepost?channel=..&post=..
tg://resolve?domain=..&post=..
?single  ?thread=N  ?comment=N
```

一条消息里贴多个链接会拆成多个任务。相册自动整组取回。

---

## 保真边界

能完整保留：文本与全部 entities（粗体、链接、剧透、代码块）、照片、视频、
GIF、文件、语音、圆形视频、贴纸、位置、联系人、caption、spoiler 标记、
文件名、视频时长与分辨率、缩略图。相册整组保留。

三处 API 层面的硬限制，遇到时 bot 会明确告知：

| 情形 | 原因 |
|---|---|
| 投票变成票数归零的新投票 | 原始票数无法通过 API 复制 |
| inline 按钮丢失 | 带 callback 的按钮属于原 bot |
| 受保护相册无法保持分组 | 只能逐条转存 |

---

## 命令

```
/help      说明
/status    登录状态、流量与消息统计、队列深度
/killall   终止所有进行中的任务并清空队列
/logout    从 Telegram 服务端撤销 session 并清除本地记录
```

`/status` 会显示累计完成数、已投递消息条数、搬运字节（含双向流量估算），
以及零流量直转与需要搬运的次数对比。

`/killall` 会中断正在进行的下载上传、清空两条队列、清理临时文件，
然后重新拉起 worker。服务本身不重启。

管理命令（`/users` `/queue` `/stats` 已可用，其余需开启多用户）：

```
/users  /queue  /stats
/adduser  /deluser  /ban  /unban  /promote  /demote  /revoke
```

---

## 项目结构

```
bot.py            入口：bot client + 调度器 + session 池
config.py         配置加载
crypto.py         session 加解密
db.py             SQLite schema（users / tasks）
acl.py            权限判定单点  ← 开多用户只改这里
parser.py         链接解析（纯函数，无副作用）
session_pool.py   按 user_id 索引的惰性连接池，LRU + 空闲回收
fetcher.py        定位消息、判定路径、送进中转频道
streamer.py       有界异步管道 + 媒体属性重建
sender.py         copyMessage 投递
taskqueue.py      双通道队列、重试、FloodWait、进度
admin.py          用户管理与审批
login.py          首次交互登录
genkey.py         生成加密密钥
tests/            34 项测试

init.sh           一键部署 / 修复（幂等，可反复跑）
git-init.sh       首次推送到 GitHub
git-push.sh       日常更新推送
git-guard.sh      隐私检查（被上面两个引用）
```

---

## 推送到 GitHub

仓库里带了两个脚本，每次推送前自动跑隐私检查，通不过就不推。

```bash
chmod +x git-*.sh

# 首次：先在 GitHub 建一个空仓库（不要勾 Add README / .gitignore / License）
./git-init.sh git@github.com:你的用户名/tgsaver.git

# 以后每次改完代码
./git-push.sh "fix: 修正相册分组"
```

脚本用了 bash 语法，但开头有自举，用 `sh` / `dash` 调用或可执行位丢失
都能正常工作，不会报 `Syntax error: "(" unexpected`。

部署后目录属主是 `tgsaver`，而 git 通常用 root 跑，会触发 git 的
`dubious ownership` 拒绝。脚本会自动加 `safe.directory` 例外，并在
退出时（包括推送失败、检查中止、Ctrl-C）把属主还原给服务用户，
保证 `.env` 和数据库始终是 `tgsaver` 所有、权限 600。

`git-push.sh` 会先跑 `pytest`，测试不过不推。可选参数：

| 参数 | 作用 |
|---|---|
| `--yes` / `-y` | 跳过确认提示 |
| `--no-test` / `-n` | 跳过测试 |
| `--amend` | 追加到上一个提交（尚未 push 时用） |

### 检查了什么

| 检查项 | 拦截的情况 |
|---|---|
| `.gitignore` 完整性 | 文件缺失或漏了 `.env` / `*.db` / `*.session` |
| **已跟踪的机密文件** | 之前误提交过 `.env`——`.gitignore` 对已入库文件无效 |
| 暂存内容扫描 | 代码里出现 Bot Token、session 串、硬编码凭据 |
| `.env.example` | 必填项被填了真实值 |
| 提交前复核 | 列出实际要提交的文件，等你确认 |

第二项最值得留意：`.gitignore` 只对**未被跟踪**的文件生效。如果你在加
`.gitignore` 之前已经 `git add` 过 `.env`，后面再怎么写 ignore 规则都没用。
脚本会检测到并给出 `git rm --cached` 的修复命令——但那时 token 已经进过
commit 历史，等同泄露，必须去 @BotFather 用 `/revoke` 换新的。

---

## 测试

```bash
pip install pytest pytest-asyncio
pytest -q
```

42 项，覆盖：

- **链接解析**的全部形态，含论坛话题三段式（中间那个数字是话题 id 不是消息 id，
  这是最容易写错的地方）、`?single`、`tg://` 协议、各类非法输入
- **有界管道**的字节完整性、分块精确性、内存上界、下载错误传播、进度单调性
- **回归用例**：改中转频道后是否生效、投递错误是否被正确判死、
  已搬运的任务重试时是否跳过传输、老数据库能否平滑升级

改代码后先跑这个。

---

## 安全

- session 用 Fernet 加密后存库，**密钥只在 `.env`，不入库**。数据库被单独拖走解不开。
- `/logout` 会调用 `auth.logOut` 真正从 Telegram 服务端撤销，而非只删本地。
- 日志不记录 session、token、密码。
- systemd 单元带 `ProtectSystem=strict` 等加固项。
- `.env` 与 `*.db` 已在 `.gitignore` 中，**克隆后务必 `chmod 600`**。

> ⚠️ **持有 session 等同于持有该 Telegram 账号的完全读写权限。**
> 自用没问题；若开放给他人，必须让对方知情，并注意数据中心 IP 异地登录
> 可能触发 Telegram 风控。

---

## 开启多用户

改 `acl.py` 一行后重启：

```python
MULTI_USER = True
```

数据库 schema、队列、传输层、投递层全部已按 `owner_id` 参数化，无需改动。
打开后自动生效：陌生人发消息 → 自动提交申请 → 管理员收到带
「通过 / 拒绝 / 封禁」按钮的通知；`/adduser` `/ban` 等命令对通过的用户生效。

还需补两件事（单用户用不上所以没写）：

1. **`/login` 二维码登录。** 别人没有 shell 跑不了 `login.py`。
   用 Telethon 的 `client.qr_login()` 即可，**不能用验证码登录**，
   Telegram 会作废任何发进聊天窗口的验证码。
2. **每用户独立中转频道。** 否则彼此能看到对方存的内容。
   `users.relay_channel_id` 字段和 `acl.relay_channel_for()` 已留好，
   登录成功后用对方 session 调 `CreateChannel` + `InviteToChannel` 建一个写进去即可。

---

## 关于 50MB 限制的补充

50MB 不是 Telegram 平台限制，而是 **Bot API HTTP 网关**的限制。绕过它有三条路：

1. **`copyMessage` 服务端引用** —— 本项目采用。文件已在 Telegram 服务器上，不算上传。
2. **自建 [Local Bot API Server](https://github.com/tdlib/telegram-bot-api)** ——
   上传上限升到 2000MB，代价是要跑一个 C++ 服务。
3. **用 bot token 走 MTProto**（Telethon / Pyrogram 的 `start(bot_token=...)`）
   —— 不经 HTTP 网关，上限 2GB。

本项目用第 1 条，所以**不需要**自建 API server。后两条留作备选。

---

## License

MIT
