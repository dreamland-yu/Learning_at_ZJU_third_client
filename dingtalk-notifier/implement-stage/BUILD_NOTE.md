# 钉钉提醒构建记录

| Rung | Feature | Acceptance check | Tier | Status |
|------|---------|------------------|------|--------|
| F0 | 命令入口生成不含秘密的消息预览 | `python3 dingtalk-notifier/send.py --smoke` | MUST | 已通过（真实 CLI；sent=false） |
| F1 | 真实钉钉 API 请求、响应和工作通知异步结果检查 | `python3 -m unittest discover -s dingtalk-notifier -p 'test_*.py'` | MUST | 本地离线检查通过，14 tests；真实 HTTP 契约待有效凭证验证 |
| F2 | 向用户指定好友实际发送测试消息并确认送达 | 官方 DWS CLI 用户身份发送一次；客户端由用户确认 | MUST | 已通过；2026-10-15 用户明确确认收到。Python 应用模式未做真实发送验证 |

## Run record

用户已实际登录开发者后台；已核实当前账号没有浙江大学组织的开发权限。
早期测试团队创建入口提示邮箱不合法；后续已通过专用账号所属 dreamland 组织完成 CLI 授权。
本机可用 Python 3.9.6。

2026-10-14 本地实现记录：

- F0 先实现真实入口并运行 `--smoke`，输出 `preview` / `sent=false` / “仅预览，未发送”。
- F1 实现 `bot`、`work_notice` 和可加签的 `webhook`，只使用当前模式的必需字段。
- `python3 -m unittest discover -s dingtalk-notifier -p 'test_*.py'`：最终 14 个测试通过；全部 HTTP 由本地 fake/mock 替代，无实际 API 或消息请求。
- 测试覆盖显式 `--send` 才 dispatch、请求 body/header、工作通知失败/部分失败、目标已读/未读、`forbidden_list[].userid` 禁发（含与 read 名单冲突）、15 秒 pending、查询业务错误、HTTP 403、发送超时未知、秘密/token/接收 ID 脱敏与 Webhook 固定签名向量。
- `python3 dingtalk-notifier/send.py --config dingtalk-notifier/config.example.json`：预期退出码 1，友好提示缺少字段，无请求。
- 最终再次运行 `python3 dingtalk-notifier/send.py --smoke`：退出码 0，仍明确未发送。
- 所有发送路径无自动重发；工作通知受理但无目标结果时返回 pending；bot/Webhook 只报告 accepted。客户端送达仍需用户确认。

接口字段的官方文档核验由根代理完成：机器人单聊返回 `processQueryKey` / `invalidStaffIdList` / `flowControlledStaffIdList`；
工作通知结果返回 invalid/forbidden/failed/read/unread user ID 数组及 `forbidden_list[].userid`。
代码不输出这些 ID、原始 API body 或 token。

## Deferred

DING、自动调度和 LAZY 接入。

## Assumption sweep

请求了 GPT-6-Astra xhigh 的 fresh-agent 检查，但服务返回模型容量不足。
记录为 `SWEEP_UNAVAILABLE`；未获得审核结论，未用本地测试代替审核。
新文件保持未提交状态，便于在实际钉钉配置完成前继续调整。

## CLI investigation

2026-10-14：桌面钉钉窗口现已可读取，用户已登录专用账号并添加测试好友。当前检查官方 DWS 的纯 CLI 用户授权路径。
官方 npm 包元数据为 `dingtalk-workspace-cli@1.0.64`，带 `postinstall: node install.js`；安装前先检查该脚本，避免写入全局技能目录。
随后已实测完成组织授权、唯一接收人解析和单聊发送。

官方包已解出 macOS arm64 二进制、LICENSE、NOTICE 至被 Git 忽略的 `.runtime`，未执行 npm postinstall。
`dws version` 和 `dws auth login --help` 已真实运行并退出 0，确认版本 v1.0.64 与设备授权支持。
`dws chat +dm --help` 已退出 0，确认该命令按通讯录姓名唯一解析接收人。
用户完成设备授权并同意将 dreamland 组织的 CLI 可用范围保存为“梦屿”一个账号；已保存并核验页面结果。
CLI 搜索返回唯一好友“余矾恩”；使用用户身份、明确 openDingTalkId 和固定幂等键发送一次固定文本。
发送返回 success=true 及 openTaskId，回执记录在被 Git 忽略的 cli-test.private.json。后续异步回执查询因自动审批服务用量限制未执行；没有换路径绕过或重发。
2026-10-15 用户明确确认客户端收到该条测试消息，完成实际送达验证。

2026-10-15 AI 角标核实：本机官方 v1.0.64 的 +dm 帮助显示 ai-tag 默认 true。
使用 +messages-send、已知 openDingTalkId、--ai-tag=false 和 --dry-run，仅本地生成计划，退出 0，executed=false，参数不含 clawType。
首次真实测试使用了默认 AI 角标；本轮没有追加发送，关闭角标后的客户端显示尚未实测。README 已补充本人登录和手动发送命令。

## Blockers

普通账号 CLI 提醒链路当前无阻塞。浙江大学组织应用开发权限仍不可用；独立 Python 应用机器人、工作通知和 Webhook 的真实接口验证仍需相应凭证，与已验证的 CLI 路径分开记录。
