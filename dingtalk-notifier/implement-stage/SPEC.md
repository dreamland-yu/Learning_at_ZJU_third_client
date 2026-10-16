# 钉钉提醒链路验证

目标：在本机常驻运行 LAZY Server 与钉钉普通账号“梦屿”的提醒/指令服务。

输入：已登录 LAZY 的本机账号、Server 缓存、指定好友私聊指令、手动待办。

输出：课程/待办/截止时间列表，签到首见提醒，24/12/3 小时各一次截止提醒，/help /assignment /assignment_add /time 与 /task interval|reset 回复；SQLite 保存手动待办、设置、消息去重、提醒状态。私有状态不提交。

成功命令：`python3 dingtalk-notifier/bridge.py --smoke`；单元测试；真实 Server 缓存读取、限定私聊收发及应用内 DING 测试；常驻进程健康状态。

Base commit：`53f6cf2c02b53fe79ff12c11da90bb31d91a899f`。

范围：复用 Server 默认 30/60 秒数据更新，适配端查询频率可配置且不低于对应 Server 间隔。仅提醒、查询与手动待办，不执行签到或提交。DING 仅应用内，平台明确不可用时退回普通消息。保留已有单次发送工具。

ASK：never；缺少登录或平台凭证时继续完成本地可验证工作，但不声称已送达。
