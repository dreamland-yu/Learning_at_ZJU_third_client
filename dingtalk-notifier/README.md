# LAZY 钉钉提醒与私聊指令

使用官方 DingTalk Workspace CLI，以指定账号在固定私聊中回复指令；后台复用 LAZY Server 缓存，监控点名和待办。无需接入大模型。

## 安装与启动（macOS）

在仓库根目录准备 Python 3.12 虚拟环境：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[server]'
.venv/bin/python builder.py
```

先按[官方 CLI 文档](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli#getting-started)安装 `dws` 并完成设备授权，确认发送账号、接收人的 openDingTalkId 和私聊 conversationId。

```sh
cp dingtalk-notifier/bridge.example.json dingtalk-notifier/bridge.private.json
# 编辑 bridge.private.json，填写本机路径和已核实的 profile/接收人/会话 ID。
.venv/bin/python dingtalk-notifier/login_server.py
```

Server 与 `lazy login` 的存储独立。`login_server.py` 在你自己的终端隐藏输入密码，验证后存入本机 Server 的加密存储；不要把密码发到聊天或写入配置文件。当前适配器只允许一个 Server 账号。

```sh
.venv/bin/python dingtalk-notifier/bridge.py --smoke  # 离线验证，不发消息
.venv/bin/python dingtalk-notifier/bridge.py --check  # 真实只读检查
.venv/bin/python dingtalk-notifier/service.py install
.venv/bin/python dingtalk-notifier/bridge.py --status
```

`install` 安装当前用户的 LaunchAgent，并立即启动；以后登录 macOS 时自动运行。Server 由桥接程序管理，仅监听 `127.0.0.1:8765`。Mac 需要开机、联网并保持唤醒。

```sh
.venv/bin/python dingtalk-notifier/service.py stop
.venv/bin/python dingtalk-notifier/service.py start
.venv/bin/python dingtalk-notifier/service.py restart
.venv/bin/python dingtalk-notifier/service.py uninstall
```

`stop` 停止本次运行，`uninstall` 同时删除自动启动项；二者均保留手动待办和去重数据库。私有日志和状态在 `.runtime/`，配置为 `bridge.private.json`，都已被 Git 忽略。

## 私聊指令

仅响应配置中发送人和会话 ID 都匹配的私聊。

| 指令 | 功能 |
|---|---|
| `/help` | 列出三个功能入口 |
| `/assignment` | 按截止时间列出课程、待办名、截止时间，合并手动待办 |
| `/assignment_add` | 开始添加手动待办，10 分钟内回复事项和时间 |
| `/time` | 查看实际缓存查询间隔、下限及修改方法 |
| `/task interval rollcall_watch 45` | 将签到缓存查询间隔设为 45 秒 |
| `/task interval todo_watch 120` | 将待办缓存查询间隔设为 120 秒 |
| `/task reset rollcall_watch` | 恢复签到查询默认值 |
| `/task reset todo_watch` | 恢复待办查询默认值 |

其他消息回复 `请使用/help查询可用功能`。添加待办时，先发 `/assignment_add`，再发：

```text
完成实验报告 2026年10月18日23时59分00秒
```

也接受 `完成实验报告 2026-10-18 23:59:00`，统一按北京时间解释。手动项显示为“手动待办”，只保存本地；目前未提供完成或删除指令。

## 自动提醒规则

- 默认每 30 秒读签到缓存、60 秒读待办缓存；下限不低于对应 Server 间隔。`/task` 修改的是适配端读取缓存的间隔，Server 上游频率保持原配置。
- 每个点名 ID 首次发现时只通知一次，优先应用内 DING。应用内 DING 已实际验证送达；不使用短信或电话。类型不明确时显示“点名”，不猜测数字类型。
- 每个待办在剩余时间首次严格小于 24、12、3 小时时，各提醒一次。只对已确认未提交的普通作业和本地手动待办提醒；其他作业类型或无法确认提交状态时跳过。
- 启动时若已经只剩 2 小时，只补当前最紧迫的 3 小时档，避免连发三条；已过期不补临期通知。截止时间调整后重新计算一轮。
- 指令消息、手动待办、间隔和提醒状态保存到 SQLite，重启后保留。发送超时或结果不明时记录 `unknown`，不换幂等键重发；平台受理不等于用户已读。
- 优先个人事件流收指令，并用有界历史查询补漏；事件不可用时每 10 秒查询指定私聊。不会读取其他好友会话。

验证：

```sh
.venv/bin/python -m unittest discover -s dingtalk-notifier/tests
```

## 早期单次提醒工具

已跑通官方 CLI 以普通账号发送单聊消息，用户已确认收到；操作见下方“普通账号通过官方 CLI 发送”。
另有独立 Python 3.9+ 标准库工具，用于应用机器人、工作通知和群 Webhook；这些模式目前只完成离线验证。
下面的 `send.py` 是早期独立工具，不导入 LAZY、无定时或自动重发；与上面的常驻桥接程序分开运行。

```sh
python3 dingtalk-notifier/send.py --smoke
cp dingtalk-notifier/config.example.json dingtalk-notifier/config.private.json
# 在本地编辑 config.private.json，填入自己有权使用的凭证。
python3 dingtalk-notifier/send.py --config dingtalk-notifier/config.private.json
# 只有下面的显式 --send 会请求 API、发送一条消息：
python3 dingtalk-notifier/send.py --config dingtalk-notifier/config.private.json --send
```

按 `mode` 填写字段；不需要的字段可删除或留空：

| mode | 必需字段 | 用途 |
|---|---|---|
| `bot` | `client_id`, `client_secret`, `robot_code`, `user_id` | 企业内部应用机器人主动单聊 |
| `work_notice` | `client_id`, `client_secret`, `agent_id`, `user_id` | 内部应用工作通知，发送后查询异步结果 |
| `webhook` | `webhook`；启用加签时还需 `secret` | 群机器人；不是私聊 |

`client_id` / `client_secret` 对应应用 AppKey / AppSecret；`agent_id` 是正整数。
`user_id` 是应用所属组织中的一个员工 userId，不是手机号，也不是跨组织通用 ID。
Webhook 必须使用官方 `https://oapi.dingtalk.com/robot/send?access_token=...` 地址；
若机器人启用了关键词或 IP 安全设置，还需让消息内容/运行环境符合该设置。
可用 `--message '测试内容'` 更换文本；缺少字段会报错。无需安装依赖。

输出不包含密钥、token 或接收 userId，不会打印远端原始响应。
私有配置不要提交；默认私有文件名已加入本目录 `.gitignore`。

| 输出 status | 含义 |
|---|---|
| `preview` | 仅本地预览，未发送 |
| `accepted` | bot / Webhook API 已受理；尚未确认客户端收到 |
| `platform_confirmed` | 工作通知结果中目标列入已读/未读；仍需客户端核对 |
| `pending` | 工作通知已受理，约 15 秒内结果未确认或查询失败 |
| `failed` / `partial_failure` | HTTP、业务或接收者失败；未自动重试 |
| `unknown` | 发送请求发出后发生超时/无效响应，结果未知；先核对客户端，勿盲目重发 |

`delivery_confirmed` 始终为 false：本工具无法代替客户端或用户确认。
退出码 0 表示预览、受理或平台结果确认；3 为 pending；1 为失败或未知。

离线测试（模拟 HTTP，不发送网络消息）：

```sh
python3 -m unittest discover -s dingtalk-notifier -p 'test_*.py'
```

接口依据：[机器人单聊](https://open.dingtalk.com/document/orgapp/chatbots-send-one-on-one-chat-messages-in-batches)、
[工作通知结果](https://open.dingtalk.com/document/orgapp/gets-the-result-of-sending-messages-asynchronously-to-the-enterprise)。
需要所属组织管理员授予对应应用能力和发送权限；本地测试通过不代表已取得平台权限。

## 普通账号通过官方 CLI 发送

已在本目录 `.runtime/dws` 解包官方 `v1.0.64` macOS arm64 二进制，未运行 npm 的技能安装脚本。
它支持无桌面客户端的设备授权；首次仍需用手机或浏览器完成本人授权，不能用邮箱密码或手机号验证码直接命令行登录。

在终端运行以下设置（只影响当前终端，不写入 shell 配置）：

```sh
export DWS_CONFIG_DIR="$PWD/dingtalk-notifier/.cli-state"
alias dws="$PWD/dingtalk-notifier/.runtime/dws"
dws profile list
```

本机完成一次授权后可直接发送，无需每次重新登录。
首次登录、换机器或授权失效后，运行：

```sh
dws auth login --device --no-browser
```

按终端提供的网址和设备码，由本人在浏览器/手机完成授权，核对发送账号并选择已开放 CLI 的组织。
配置目录位于项目中，macOS 凭据仍由 CLI 的系统钥匙串存储管理；配置目录不等于账号隔离。
实际发送前可用 `dws profile list` 确认账号；`--profile` 支持唯一的“组织名:用户名”，也可使用列表返回的精确 `corpId:userId`。

```sh
# 这是实际发送命令；按终端提示确认后发送。
dws --profile '测试团队:发送账号' chat +dm \
  --to '接收人姓名' \
  --content '这里填写你自己要发送的消息' \
  --ai-tag=false
```

`+dm` 通过通讯录解析姓名，重名时需进一步区分。本机已验证指定发送账号可搜索并向测试好友发送；其他目标仍取决于实际可达性。
CLI token 与桌面登录相互独立；完成设备授权后，运行 CLI 不要求桌面钉钉一直登录或打开。正常调用会自动刷新 token，授权失效后需重新登录。

### “通过 AI 发送”角标

官方 v1.0.64 的 `chat +dm` 和 `chat +messages-send` 默认 `--ai-tag=true`。
首次测试未覆盖该默认值，因此请求带有 `clawType: "openClaw"`，用户收到的消息显示了 AI 角标；正文是命令中指定的固定文本。
手动发送自己输入的文本可加 `--ai-tag=false`。本地 `--dry-run` 已确认该选项会从请求中移除 `clawType`；关闭角标后的客户端显示尚未另发消息验证。
可用 `dws chat +dm --help` 查看当前版本的参数说明。

官方说明：[CLI 登录](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli#getting-started)、
[聊天命令](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/blob/main/skills/multi/dingtalk-chat/SKILL.md)。
