# Assumption Ledger — DingTalk notifier
<!-- ASK mode: never -->

A-001–A-015 为早期单次发送工具的历史决策；本轮常驻服务以 A-016 之后的决策为准。Python3.9 约束仅适用于早期 send.py，常驻服务采用独立 Python3.12 环境。

| ID | Under-determined by the request | Chosen | Class | Source |
|----|--------------------------------|--------|-------|--------|
| A-001 | 早期 send.py 接入类型 | 仅早期 send.py 以用户授权的企业内部应用单聊/工作通知为目标，群 Webhook 是可选入口；该工具只实现普通消息。常驻 bridge 的 DING 按 A023 | interface | default |
| A-002 | 本次验证消息内容与对象 | 仅向用户本人或其指定的测试群发送一次“钉钉提醒测试：消息发送链路测试。” | semantic | default |
| A-003 | 应用凭证尚不可用 | 本地 JSON 配置，私有配置不提交 Git；预览与输出隐藏密钥、Webhook token和接收userId | interface | default |
| A-004 | 成功含义 | 预览/离线检查通过不算已送达；内部机器人发送接受不等于客户端收到；工作通知须查询异步结果 | semantic | default |
| A-005 | 早期 send.py 运行环境和依赖 | 仅早期 send.py 兼容本机 Python3.9、只用标准库且不导入LAZY；常驻bridge按A027使用Python3.12/httpx和LAZY Server | interface | default |
| A-006 | 平台权限和组织归属 | 只使用已授权的应用；不把浙江大学成员身份或企业版本当作管理员权限；尚不创建测试组织 | semantic | default |
| A-007 | 模式和字段对应 | `bot` 用 client_id/client_secret/robot_code/user_id；`work_notice` 用 client_id/client_secret/agent_id/user_id；`webhook` 只用 webhook 和可选 secret。client_id/client_secret 映射内部应用 accessToken 请求的 appKey/appSecret | interface | default |
| A-008 | 实际请求契约 | bot 使用 `/v1.0/robot/oToMessages/batchSend` 的 sampleText 模板；工作通知使用 asyncsend_v2 后查询 getsendresult；Webhook 使用 text 消息和可选 timestamp + HMAC-SHA256 加签 | interface | default |
| A-009 | 不完整结果与时间预算 | bot/Webhook 只报 accepted；工作通知只有目标出现在 read/unread 名单且无失败名单时报告平台确认发送（不代表用户已看见），最多查询约 15 秒，否则 pending。任何失败名单产生 failed/partial_failure，不把空名单当成功 | semantic | default |
| A-010 | 早期 send.py 发请求和凭证隐私 | 仅约束早期 send.py：默认预览，显式 --send 才联网；不自动重试，错误不输出原始body/敏感配置，Webhook限官方HTTPS地址。常驻bridge通过 --check 只读联网、--run启用已授权的提醒和回复 | interface | default |
| A-011 | 用户希望直接以账号用 CLI 登录和发消息 | 检查官方 DWS 的用户授权与普通单聊能力；组织授权与好友可达性以真实登录结果为准 | interface | user |
| A-012 | CLI 安装位置和附带技能写入 | 先下载并检查官方包，使用本项目忽略目录 `.runtime` 隔离运行；不运行会向用户技能目录写入的安装器 | interface | default |
| A-013 | 当前测试收件人与消息次数 | 用已登录发送账号“梦屿”向用户明确允许测试的好友“余矾恩”发送一条普通单聊文本，先用只读 CLI 搜索核实唯一 openDingTalkId | semantic | user |
| A-014 | CLI 测试发送后的重试语义 | 本次测试使用固定幂等键，记录目标与回执到被 Git 忽略的 cli-test.private.json；未知投递结果不换键重发 | interface | default |
| A-015 | 用户手动通过 CLI 发送时的 AI 角标 | 说明官方 CLI 默认 ai-tag=true；用户自行输入文本的示例使用 --ai-tag=false，本轮只做 dry-run 核实参数，不追加实际测试消息 | interface | user |
| A-016 | 本轮功能范围 | 实现用户列明的 24/12/3 小时各一次截止提醒、签到首见提醒、四个指令与 /task interval/reset、手动待办；只处理已确认好友与固定私聊 | semantic | user |
| A-017 | 查询间隔作用域 | /time 和 /task 修改适配端读取 Server 缓存的间隔，不修改 Server 上游查询频率；默认 30/60 秒，下限取 Server 对应间隔与 30/60 中较大者；重置为该下限 | interface | user |
| A-018 | 时间和手动输入 | 采用 Asia/Shanghai；手动输入补全明显遗漏的月份，提示“事项 2026年10月18日23时59分00秒”；也接受 YYYY-MM-DD HH:mm:ss。未给课程的手动项标为“手动待办”，只存本地，不写学在浙大 | semantic | default |
| A-019 | 启动或离线后同时满足多个阈值 | 仅发送当前最紧迫的一档并记较早档已错过，避免一次推三条历史提醒；之后每个较小阈值仍单独一次。过期不发临期提醒；同 ID 截止时间改变视为新一轮 | semantic | default |
| A-020 | 持久性和投递状态 | SQLite 持久保存指令消息 ID、签到 ID、任务+截止时间+阈值、待发消息及固定幂等键；发送受理后不再推同事件；结果未知时不换键重发或直接改走 DING/普通双发 | interface | default |
| A-021 | 接入登录态及服务部署 | 在本机回环地址启动独立 LAZY Server；CLI钥匙串访问被macOS拒绝后，改用用户亲自在终端授权的Server加密存储（见A028）；只在进程内转交凭据，不输出密码/token；后台以 macOS LaunchAgent 托管并提供停止命令 | interface | default |
| A-022 | 未提交判定与缓存有效期 | 不仅凭待办存在判定未提交；临期时查询原始提交记录/完成状态，失败或未知跳过并留错误状态；缓存必须携带成功更新时间和当前查询状态，过期/失效不误报 | semantic | default |
| A-023 | DING 和消息来源 | 用户授权测试一次明确标注为测试的应用内 DING（app）；不使用短信或电话。签到优先 DING，明确不支持时普通私聊兜底；普通消息关闭 AI 角标 | interface | user |
| A-024 | 首次启动与指令监听 | 启动只处理此刻之后的聊天指令，重连可有界补读并按 ID 去重；签到发现即通知；手动输入等待 10 分钟，其他有效指令结束等待，非指令普通消息按用户原文提示 | semantic | default |
| A-025 | 缓存陈旧与会话恢复 | Server 成功缓存超过 max(2×上游间隔,60s)、最近查询失败或任务禁用则 stale；失败保留历史缓存但不作为新事实。每用户刷新锁及代次防重复登录，至少60s登录退避，只重试原请求一次 | semantic | default |
| A-026 | 提交状态支持范围 | 初版对普通 homework 核对明确 user_submit_count 和学生提交列表；非支持类型、请求失败、相互矛盾返回未知，不发“未提交”临期提醒；/assignment 仍展示待办原始列表 | semantic | default |
| A-027 | 托管和收消息恢复 | 本机 Python3.12 虚拟环境运行桥接和 Server；优先指定私聊个人事件流，60秒有界历史补查；事件不可用时10秒历史查询兜底。只处理稳定发送人ID及会话ID都匹配的消息；游标与已处理ID持久化 | interface | default |
| A-028 | 后台服务凭据与日志 | 原计划复用CLI钥匙串，但真实检查被macOS返回Keychain Access Denied；停止该路径，由用户在自己终端用login_server.py独立验证并授权Server的加密凭据。后台只读该Server存储；HTTP只走127.0.0.1，token仅内存，关闭访问日志避免查询参数泄露；私有状态和运行日志Git忽略 | interface | default |
| A-029 | 临期消息中的剩余小时 | 使用用户指定的“课程《…》的待办‘…’截止时间为…，还剩…小时！”格式；剩余小时按实际时间差保留一位小数，24/12/3仅用于触发档位；手动项不声称存在提交状态 | semantic | default |
| A-030 | 手动项接受规则 | 只接受截止时间晚于当前时间的手动待办；相等或已过期时提示重新填写，保留输入等待 | semantic | sweep |
| A-031 | Server 不可用时查询手动项 | /assignment 仍显示已有本地手动待办，并明确学在浙大部分未能读取；无本地项时诚实返回查询失败 | semantic | sweep |
| A-032 | Server 账号选择 | 当前后台只接受 Server 凭据存储中恰好一个账号；多账号时拒绝启动，不自动选择其他用户 | interface | sweep |
| A-033 | 待发提醒失效 | 暂时无法核对的截止/签到通知保留等待但不阻塞后续回复；签到成功刷新后确认已结束的待发通知终结为 skipped，不在重启时发“正在签到”的过时消息 | semantic | sweep |

## Notes

A-007/A-008 的字段和请求契约来自已核实的钉钉官方文档，模式选择与默认文本模板由实现者决定。
用户已自行扫码登录。实际创建入口确认浙江大学账号没有开发者权限；用户将自行创建独立测试团队，待回告后继续配置。
用户随后已在桌面客户端登录专用账号并添加一个测试好友，当前优先验证纯 CLI 路径。桌面登录不视作 CLI 登录或已授予其应用权限。
随后已完成 dreamland 组织的设备授权，并按用户确认将 CLI 可用范围保存为专用账号“梦屿”。CLI 已向指定好友发送一次消息，用户于 2026-10-15 明确确认收到。
