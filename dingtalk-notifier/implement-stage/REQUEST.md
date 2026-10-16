# User acceptance requirements (2026-10-15)

1. Read LAZY Server's results at configurable intervals in seconds, no faster than its defaults (rollcall 30s, todo 60s).
2. Notify once per task on first remaining time below 24h, 12h, and 3h. Wording: `课程《xxx》的待办“xxxx”截止时间为x月x日xx：xx，还剩x小时！`.
3. On first sight of each rollcall send `课程《xxx》正在雷达/数字点名！！！`. Try app DING and use it if supported. Do not answer rollcalls automatically.
4. Commands `/help /assignment /time /assignment_add`; other messages: `请使用/help查询可用功能`.
5. `/help` returns exactly:

```
/assignment：查看所有待办

/assignment_add：手动添加待办

/time：设置轮询server时间
```

6. `/assignment`: show every task's course, title, deadline with readable formatting.
7. `/assignment_add`: ask for task and full deadline (user's template omitted month; implementer records correction in A018), then retain it for both queries and automatic threshold reminders.
8. `/time`: show `rollcall_watch — 监控进行中的点名` and its actual interval, `todo_watch — 监控待办任务` and its actual interval, then `/task interval <id> <秒> — 修改轮询间隔` and `/task reset <id> — 恢复默认设置`.
9. Interval changes/reset reply with both resulting intervals and current time.
10. Persist one notification per rollcall and one per deadline threshold, never repeat on each 30/60s check.
11. User requires fork of upstream and commits/pushes only to user's own repository. Fork verified as `dreamland-yu/Learning_at_ZJU_third_client`, working branch `codex/dingtalk-reminders`.
12. User confirmed receiving the test DING, then independently authorized Server login after macOS denied CLI Keychain access. On 2026-10-16 the user also confirmed the service's automatic private-chat reply was normal.
