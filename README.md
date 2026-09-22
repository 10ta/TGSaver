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
https://t.me/name/181?comment=4832 频道帖子下的评论
tg://privatepost?channel=..&post=..
tg://resolve?domain=..&post=..
?single  ?thread=N  ?comment=N
```

一条消息里贴多个链接会拆成多个任务。相册自动整组取回。

**评论链接**里的两个数字属于两套独立编号：`181` 是频道里的帖子，
`4832` 是**关联讨论群**里的消息。TgSaver 会先反查出讨论群再按后者取。
前提是你的账号已加入那个讨论群——在 Telegram 里点进评论区会自动入群。

### 推文：发 X / Twitter 链接

```
https://x.com/用户/status/123
https://fxtwitter.com/用户/status/123
```

`twitter.com` / `vxtwitter` / `fixupx` / `fixvx`、`/i/status/`、
带 `/photo/1` 或 `?s=46` 这类尾巴的都认，同一条帖子贴多次只处理一次。

正文数据经 [FxEmbed](https://github.com/FxEmbed/FxEmbed) 的 JSON API
（`api.fxtwitter.com/2/status/{id}`）获取，组装成：

```
┌──────────────────────┐
│  原帖图片 / 视频大图预览  │   ← fxtwitter 链接预览，显示在正文上方
└──────────────────────┘
**用户昵称** :
┃ 帖子正文（引用块）
原帖链接 · #用户ID          ← 「原帖链接」指向 x.com 原帖

via @署名                  ← 可选，见下
```

#### 两种呈现方式

由 `.env` 里的 `TWEET_MODE` 决定：

| | `preview`（默认） | `media` |
|---|---|---|
| 做法 | 一条文字消息 + fxtwitter 链接预览 | 真正发送图片视频 |
| 速度 | 最快，只发一条文字 | 通常 < 1 秒，超限时回退中转 |
| 本机流量 | 0 | 通常 0，回退时 2× |
| 大小限制 | 无 | URL 直发照片 5MB / 视频 20MB，超了走中转 |
| 多图 | fxtwitter 合成的一张拼图 | 独立相册，每张可单独保存 |
| 媒体是否独立副本 | 否，是 Telegram 抓取后缓存的预览 | 是 |

`preview` 模式下，预览地址用 `link_preview_options.url` 单独指定，
不必出现在正文里，所以「原帖链接」仍然指向 x.com。纯文字推文不开预览，
否则预览卡片只会把正文再显示一遍。

`media` 模式的细节：先把 twimg 的 URL 直接交给 Telegram 服务器去拉
（和 Telegram 渲染链接预览是同一个机制），被拒时回退到本机下载、
user 账号上传中转频道再复制。被拒只会发生在用户还什么都没收到的阶段，
不会收到两份。说明挂在相册第一项上，超过 1024 字时拆成「媒体 + 文字」两条。

#### 其他细节

- 纯文字消息上限 4096，超长截断加省略号。长度按 UTF-16 算，emoji 占 2 个；
  超链接的 URL 本身不计入长度
- 图片取推特图床的 `4096x4096` 规格；视频挑 **h264** 的最高码率 mp4
- 帖子被删、账号冻结、受保护时直接报原因，不重试

#### 可选配置

```
TWEET_MODE=preview                      # 或 media
TWEET_SIGNATURE_TEXT=@你的频道           # 末尾 "via 署名" 那一行，留空不显示
TWEET_SIGNATURE_URL=https://t.me/xxx
FXTWITTER_API=https://api.fxtwitter.com # 自建 FxEmbed 实例时改这里
FXTWITTER_HOST=fxtwitter.com            # 生成预览用的域名
```

署名默认为空，所以公开仓库被别人 clone 时不会带上你的频道。

### 私聊内容：发对话地址

私聊里的**单条消息**没有 t.me 链接，Telegram 只为公开频道和超级群的
消息生成链接。这类内容直接把**对话地址**发给 bot，不用带命令：

```
t.me/some_bot        抓最近 1 条媒体
t.me/some_bot 5      抓最近 5 条
t.me/c/1234567890 3  私有频道也行
.some_bot 5          点号简写，手机上更快
123456789 3          数字 id
```

会往回翻最近 200 条消息找媒体，单次最多 20 条。相册只需命中一条，
下游会自动凑齐整组。抓到的内容走和消息链接完全相同的传输与投递路径。

> **不要用 `@some_bot 5` 这种写法。** Telegram 客户端看到消息以
> `@botname ` 开头会拦截成对该 bot 的 inline 查询，消息根本发不出去。
> 代码里仍然接受这种形式（有些 bot 不支持 inline，能发出来），
> 但不推荐依赖它。

裸用户名（不带 `t.me/`、`@` 或点号）一律不认，数字 id 至少 6 位。
这两条限制是为了不把普通聊天误判成抓取指令——不加限制的话 "hello"
也会被当成对话名。`/grab` 作为别名保留。

---

## 保真边界

能完整保留：文本与全部 entities（粗体、链接、代码块）、照片、视频、
GIF、文件、语音、圆形视频、贴纸、位置、联系人、caption、
文件名、视频时长与分辨率、缩略图。相册整组保留。

### 剧透遮罩

| 情况 | 行为 |
|---|---|
| 受保护内容（路径 B/C） | **自动去掉**，因为本来就是重新上传 |
| 普通内容（路径 A） | 默认保留——服务端直转无法剥离 |
| 普通内容 + `nosp` | 改走重传路径，去掉遮罩 |

链接后面单独加一个 `nosp` 即可：

```
https://t.me/chan/123 nosp
```

也支持写成查询参数 `?nosp`，可与 `?single` 并用。

代价是这条链接不再享受零流量零磁盘的直转，会实际下载再上传一遍。
所以做成了开关而不是默认行为——大多数时候你并不需要它。

标记会被写进链接本身再入库，任务重试或进程重启后依然有效。

三处 API 层面的硬限制，遇到时 bot 会明确告知：

| 情形 | 原因 |
|---|---|
| 投票变成票数归零的新投票 | 原始票数无法通过 API 复制 |
| inline 按钮丢失 | 带 callback 的按钮属于原 bot |
| 阅后即焚媒体可能抓不到 | TTL 到期后服务端就没有了 |

**受保护的相册现在也能保持分组。** 实现上没有用 Telethon 自带的
`send_file(列表)`——那条路径不传 `attributes`，会把文件名、视频时长
分辨率、缩略图全部丢掉。这里手工组装 `SendMultiMedia`，逐项保留各自
的属性和剧透标记。万一整组发送失败，会用已上传的句柄退化为逐条发送，
不重传字节，同时提示分组未能保持。

---

## 命令

```
t.me/频道/123      发消息链接即可，无需命令
x.com/用户/status/1 发推文链接，整理成昵称 + 引用 + 大图预览 + 原帖链接
t.me/频道/123 nosp 加 nosp 去掉剧透遮罩（会重传）
t.me/对话名 5      抓私聊内容（没有消息链接的用这个）

/status    登录状态、流量与消息统计、队列深度
/killall   终止所有进行中的任务并清空队列
/logout    从 Telegram 服务端撤销 session 并清除本地记录
/help      说明
```

`/status` 会显示累计完成数、已投递消息条数、搬运字节（含双向流量估算），
以及零流量直转与需要搬运的次数对比。

`/killall` 会中断正在进行的下载上传、清空两条队列、清理临时文件，
然后重新拉起 worker。服务本身不重启。

输入框旁的 **Menu** 按钮、以及打 `/` 时弹出的候选列表会自动出现，
启动时通过 `setMyCommands` 注册。管理命令只注册在机主的对话作用域里，
授权用户的菜单看不到它们——但这只是显示层面的隐藏，真正的鉴权在
`admin._guard`，两者不能互相替代。

机主另有一组命令，`/admin` 可查看：

```
/users                 列出所有授权用户
/adduser <id> [id...]  添加，可一次多个
/deluser <id> [id...]  移除
/ban <id> /unban <id>  封禁 / 解封（保留用量记录）
/queue  /stats         队列状态、全局统计
```

被加的人**不会收到任何通知**，加进来直接就能用，移出去就不能用。
让对方找 [@userinfobot](https://t.me/userinfobot) 拿自己的数字 id。

### 三种角色

| | 普通用户 | 管理员 | 机主 |
|---|:--:|:--:|:--:|
| 存消息、抓私聊、`/status` | ✓ | ✓ | ✓ |
| `/killall` | 只终止自己的 | 全部 | 全部 |
| `/stats` `/queue` `/users` | — | ✓ | ✓ |
| `/adduser` `/deluser` `/ban` | — | ✓ | ✓ |
| `/promote` `/demote` | — | — | ✓ |
| `/logout` | — | — | ✓ |

机主由 `.env` 的 `OWNER_ID` 决定，不能被移除、封禁或降级。

`/logout` 收归机主是因为全体共用同一份凭据——注销会让所有人一起用不了，
不该由任何单个用户触发。

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
menu.py           命令菜单注册（按作用域区分机主与普通用户）
tweet.py          推文链接识别、FxTwitter API 解析、消息组装
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

294 项，覆盖：

- **链接解析**的全部形态，含论坛话题三段式（中间那个数字是话题 id 不是消息 id，
  这是最容易写错的地方）、`?single`、`tg://` 协议、各类非法输入
- **有界管道**的字节完整性、分块精确性、内存上界、下载错误传播、进度单调性
- **回归用例**：改中转频道后是否生效、投递错误是否被正确判死、
  已搬运的任务重试时是否跳过传输、老数据库能否平滑升级
- **相册整组**：逐项属性是否保留、caption 是否逐项对应、剧透是否被剥离、
  整组失败能否退化为逐条且不重传、超 10 项是否分块、超限是否提前拒绝
- **内部伪链接**：私聊 peer 不加 -100 前缀、不被 find_links 误抓
- **抓取目标识别**：反例多于正例，确保普通聊天不会被误判成抓取指令，
  且带消息 id 的链接绝不会被当成抓取目标吞掉
- **推文**：链接识别（含 fxtwitter 等镜像与误判反例）、API 响应解析
  （tombstone / 404 / 401 / 媒体顺序 / 码率与大小）、输出格式逐字比对、
  HTML 转义、UTF-16 截断、URL 直发的各种媒体类型、被拒时用户未收到任何
  内容、快通道回退慢通道、已知超限跳过直发、中转路径的流式与落盘
- **命令菜单**：命令名与描述符合 Telegram 格式、无重复、管理命令不外泄、
  菜单里列出的每个命令都真的有处理函数
- **授权模型**：准入判定、身份只有两种（残留的 admin 角色不获得特权、
  老库自动降级）、管理命令对授权用户有反馈而对陌生人静默、
  以及最关键的「发起人与凭据归属分离」——
  含对 taskqueue / bot / admin 取凭据与鉴权方式的接线检查
- **killall 范围**：普通用户终止自己的任务时，别人的任务必须被放回
  原通道继续跑，而不是被一起打断后卡在 running
- **nosp 标记**：各种链接形态下的解析、与 single 并存、写回链接后幂等、
  落库重解析仍有效、`nospam` 这类词不误触发

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

## 多用户

机主 + 一份白名单，`/adduser` 加人即可，无需对方做任何操作。

身份只有两种，没有中间层：

| | 授权用户 | 机主 |
|---|---|---|
| 发链接、抓取、`nosp` | ✓ | ✓ |
| `/status` | 只看自己的用量 | 只看自己的用量 |
| `/killall` | 只终止自己的任务 | 终止全部 |
| `/users` `/adduser` `/deluser` `/ban` `/unban` | ✗ | ✓ |
| `/queue` `/stats`（全局） | ✗ | ✓ |
| `/logout` | ✗ | ✓ |

`/logout` 收给机主，是因为大家共用同一份凭据，
任何一个人注销都会让所有人立刻停摆。

> ⚠️ **授权用户是用机主的账号去取消息的。**
>
> 也就是说，被授权的人能读到你账号能读到的一切——包括你的私有频道，
> 以及用 `t.me/对话名 5` 抓取你和任意人 / bot 的私聊记录。
>
> **只加你自己的其他号，或完全信任的人。**

这是刻意的取舍：共用凭据省掉了每人单独登录、每人单独中转频道的全部
复杂度。代价就是上面那条，必须清楚。

用量按**发起人**记账，所以各人 `/status` 看到的是自己的数字；
凭据和中转频道则统一走机主的。这两件事在代码里由 `acl.session_user()`
分开，是整个模型的关键。

日后若要改成每人用自己的凭据，改动集中在那一个函数：让它返回 `uid`
本身而不是机主 id，再补一套二维码登录流程（不能用验证码登录，
Telegram 会作废任何发进聊天窗口的验证码）。其余模块都已经通过它取
凭据，不需要动。

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
