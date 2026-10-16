import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commands import ADD_PROMPT, UNKNOWN_TEXT, Commands
from domain import TZ, Task
from state_store import Store


class FakeStore:
    def __init__(self):
        self.settings = {}
        self.tasks = []
        self.pending_until = None
        self.messages = set()
        self.replies = {}
        self.operations = []

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def manual_tasks(self):
        return list(self.tasks)

    def add_manual(self, task_id, title, deadline):
        if not any(task.id == task_id for task in self.tasks):
            self.tasks.append(Task(task_id, "手动待办", title, deadline, "manual"))

    def pending_add(self, now):
        if self.pending_until and self.pending_until <= now:
            self.pending_until = None
        return self.pending_until is not None

    def set_pending_add(self, until):
        self.pending_until = until

    def clear_pending_add(self):
        self.pending_until = None

    def seen_message(self, message_id):
        return message_id in self.messages

    def mark_message(self, message_id):
        self.operations.append(("mark", message_id))
        self.messages.add(message_id)

    def enqueue(self, key, text, kind="chat", status="pending"):
        self.operations.append(("enqueue", key))
        self.replies.setdefault(key, text)


class FakeBackend:
    def __init__(self):
        self.tasks = []
        self.minimums = {"rollcall_watch": 30, "todo_watch": 60}
        self.failure = None
        self.calls = []

    async def get_tasks(self):
        self.calls.append("tasks")
        if self.failure:
            raise RuntimeError(self.failure)
        return self.tasks

    async def get_minimums(self):
        self.calls.append("minimums")
        if self.failure:
            raise RuntimeError(self.failure)
        return self.minimums


class CommandsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeStore()
        self.backend = FakeBackend()
        self.commands = Commands(self.store, self.backend)
        self.now = datetime(2026, 10, 15, 23, 59, 30, tzinfo=TZ)
        self.sequence = 0

    async def send(self, text, now=None, message_id=None):
        self.sequence += 1
        message_id = message_id or str(self.sequence)
        await self.commands.handle(text, message_id, now or self.now)
        return self.store.replies.get("reply:" + message_id)

    async def test_help_exact_body_and_enqueue_before_mark(self):
        self.assertEqual(await self.send("/help"), (
            "/assignment：查看所有待办\n\n"
            "/assignment_add：手动添加待办\n\n"
            "/time：设置轮询server时间"
        ))
        self.assertEqual(self.store.operations[-2:], [("enqueue", "reply:1"), ("mark", "1")])

    async def test_duplicate_message_is_complete_noop(self):
        await self.send("/assignment", message_id="same")
        before = list(self.store.operations)
        await self.send("/assignment_add", message_id="same")
        self.assertEqual(self.store.operations, before)
        self.assertEqual(self.backend.calls, ["tasks"])
        self.assertIsNone(self.store.pending_until)

    async def test_school_outage_still_shows_local_manual_tasks(self):
        self.store.tasks = [Task('local', '手动待办', '准备汇报',
                                 self.now + timedelta(hours=5), source='manual')]
        self.backend.failure = 'private error must not be exposed'
        reply = await self.send('/assignment')
        self.assertIn('学在浙大待办暂时无法读取', reply)
        self.assertIn('准备汇报', reply)
        self.assertNotIn('private error', reply)

    async def test_unknown_and_plain_messages(self):
        for text in ("你好", "", "/unknown", "/help extra", "$(touch /tmp/not-a-command)"):
            self.assertEqual(await self.send(text), UNKNOWN_TEXT)
        self.assertFalse(self.backend.calls)
        self.assertFalse(self.store.settings)

    async def test_assignments_merge_sort_and_show_unknown_deadline(self):
        self.backend.tasks = [
            Task("late", "数学", "课程作业", self.now + timedelta(days=2)),
            Task("none", "物理", "无截止事项", None),
        ]
        self.store.tasks = [Task("manual", "手动待办", "手动事项", self.now + timedelta(days=1), "manual")]
        reply = await self.send("/assignment")
        self.assertLess(reply.index("手动事项"), reply.index("课程作业"))
        self.assertLess(reply.index("课程作业"), reply.index("无截止事项"))
        self.assertIn("[手动待办]", reply)
        self.assertIn("2026年10月16日 23:59:30", reply)
        self.assertIn("截止时间：未设置", reply)
        self.assertEqual(self.backend.calls, ["tasks"])

    async def test_empty_assignments(self):
        self.assertEqual(await self.send("/assignment"), "当前没有待办。")

    async def test_add_prompt_includes_month_and_both_formats(self):
        self.assertEqual(await self.send("/assignment_add"), ADD_PROMPT)
        self.assertIn("xxxx年xx月xx日", ADD_PROMPT)
        self.assertIn("2026-10-18 23:59:00", ADD_PROMPT)
        self.assertEqual(self.store.pending_until, self.now + timedelta(minutes=10))

    async def test_add_chinese_crosses_day_and_duplicate_is_not_added(self):
        await self.send("/assignment_add")
        reply = await self.send("实验报告 2026年10月16日00时00分01秒", message_id="new-task")
        self.assertIn("添加成功", reply)
        self.assertEqual(self.store.tasks[0].id, "new-task")
        self.assertEqual(self.store.tasks[0].deadline, datetime(2026, 10, 16, 0, 0, 1, tzinfo=TZ))
        self.assertIsNone(self.store.pending_until)
        await self.send("实验报告 2026年10月16日00时00分01秒", message_id="new-task")
        self.assertEqual(len(self.store.tasks), 1)

    async def test_add_iso_has_spaces_in_title(self):
        await self.send("/assignment_add")
        self.assertIn("添加成功", await self.send("完成 Python 实验 2026-10-16 00:01:00"))
        self.assertEqual(self.store.tasks[0].title, "完成 Python 实验")

    async def test_add_rejects_missing_title_invalid_date_and_missing_month(self):
        await self.send("/assignment_add")
        for text in ("2026年10月16日00时00分01秒", "报告 2026年13月16日00时00分01秒",
                     "报告 2026年16日00时00分01秒", "报告 2026-10-16 00:01", "   "):
            self.assertIn("格式错误", await self.send(text))
            self.assertEqual(self.store.pending_until, self.now + timedelta(minutes=10))
        self.assertFalse(self.store.tasks)

    async def test_add_rejects_past_or_equal_deadlines(self):
        await self.send("/assignment_add")
        for text in ("报告 2026-10-14 23:59:30", "报告 2026-10-15 23:59:30"):
            self.assertIn("截止时间必须晚于当前时间", await self.send(text))
        self.assertFalse(self.store.tasks)
        self.assertIsNotNone(self.store.pending_until)

    async def test_add_wait_expires_at_ten_minutes(self):
        await self.send("/assignment_add")
        reply = await self.send("报告 2026-10-16 01:00:00", self.now + timedelta(minutes=10))
        self.assertEqual(reply, UNKNOWN_TEXT)
        self.assertFalse(self.store.tasks)
        self.assertIsNone(self.store.pending_until)

    async def test_valid_commands_cancel_pending_even_if_query_fails(self):
        for text in ("/help", "/assignment", "/time", "/task interval todo_watch 90", "/task reset todo_watch"):
            await self.send("/assignment_add")
            await self.send(text)
            self.assertIsNone(self.store.pending_until, text)
        await self.send("/assignment_add")
        self.backend.failure = "private-token"
        await self.send("/assignment")
        self.assertIsNone(self.store.pending_until)

    async def test_invalid_commands_do_not_cancel_pending_or_add_task(self):
        await self.send("/assignment_add")
        for text in ("/unknown", "/task interval todo_watch nope", "/task reset other", "/task interval todo_watch 1"):
            await self.send(text)
            self.assertIsNotNone(self.store.pending_until, text)
        self.assertFalse(self.store.tasks)
        self.assertFalse(self.store.settings)

    async def test_time_uses_live_server_floors_and_stored_settings(self):
        self.backend.minimums = {"rollcall_watch": 45, "todo_watch": 120}
        self.store.settings = {"interval:rollcall_watch": 80, "interval:todo_watch": 90}
        reply = await self.send("/time")
        self.assertEqual(reply, (
            "rollcall_watch — 监控进行中的点名\n"
            "  间隔: 80s\n\n"
            "todo_watch — 监控待办任务\n"
            "  间隔: 120s\n\n"
            "请使用/task interval <id> <秒> — 修改轮询间隔\n"
            "/task reset <id> — 恢复默认设置。\n\n"
            "当前下限：rollcall_watch 45 s / todo_watch 120 s"
        ))
        self.backend.minimums["rollcall_watch"] = 100
        self.assertIn("rollcall_watch — 监控进行中的点名\n  间隔: 100s", await self.send("/time"))

    async def test_time_always_applies_baselines(self):
        self.backend.minimums = {"rollcall_watch": 1, "todo_watch": 2}
        reply = await self.send("/time")
        self.assertTrue(reply.startswith(
            "rollcall_watch — 监控进行中的点名\n  间隔: 30s\n\n"
            "todo_watch — 监控待办任务\n  间隔: 60s\n\n"
        ))

    async def test_interval_change_persists_and_new_handler_reads_setting(self):
        reply = await self.send("/task interval rollcall_watch 90")
        self.assertEqual(reply, "已修改轮询间隔，现在间隔为 rollcall_watch 90 s / todo_watch 60 s\n当前时间：2026年10月15日 23:59:30")
        self.assertEqual(self.store.settings["interval:rollcall_watch"], 90)
        self.commands = Commands(self.store, self.backend)
        self.assertIn("rollcall_watch — 监控进行中的点名\n  间隔: 90s", await self.send("/time"))

    async def test_interval_below_current_server_minimum_is_not_saved(self):
        self.backend.minimums["todo_watch"] = 100
        self.assertIn("不能低于 100 s", await self.send("/task interval todo_watch 99"))
        self.assertFalse(self.store.settings)
        self.assertIn("todo_watch 100 s", await self.send("/task interval todo_watch 100"))

    async def test_interval_reset_uses_live_floor(self):
        await self.send("/task interval todo_watch 180")
        self.backend.minimums["todo_watch"] = 120
        reply = await self.send("/task reset todo_watch")
        self.assertIn("已重置轮询间隔，现在间隔为 rollcall_watch 30 s / todo_watch 120 s", reply)
        self.assertEqual(self.store.settings["interval:todo_watch"], 120)

    async def test_invalid_task_syntax_never_changes_settings(self):
        for text in ("/task", "/task reset", "/task interval other 60", "/task reset other",
                     "/task interval todo_watch 60.0", "/task interval todo_watch -1",
                     "/task interval todo_watch 60;echo secret", "/task reset todo_watch extra"):
            self.assertIn("用法：", await self.send(text))
        self.assertFalse(self.store.settings)
        self.assertFalse(self.backend.calls)

    async def test_backend_exceptions_are_honest_and_do_not_leak(self):
        self.backend.failure = "access_token=do-not-print secret-password"
        for text in ("/assignment", "/time", "/task interval todo_watch 90", "/task reset todo_watch"):
            reply = await self.send(text)
            self.assertIn("查询失败", reply)
            self.assertNotIn("do-not-print", reply)
            self.assertNotIn("secret-password", reply)
        self.assertFalse(self.store.settings)

    async def test_bad_upstream_intervals_do_not_claim_defaults_are_live(self):
        self.backend.minimums = {"rollcall_watch": "30", "todo_watch": 60}
        self.assertIn("查询失败", await self.send("/time"))
        self.assertFalse(self.store.settings)

    async def test_time_is_displayed_in_shanghai(self):
        now_utc = self.now.astimezone(timezone.utc)
        reply = await self.send("/task reset todo_watch", now_utc)
        self.assertIn("当前时间：2026年10月15日 23:59:30", reply)

    async def test_sqlite_restart_preserves_settings_pending_manual_and_dedup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.sqlite3"
            store = Store(path)
            try:
                handler = Commands(store, self.backend)
                await handler.handle("/task interval todo_watch 120", "setting", self.now)
                await handler.handle("/assignment_add", "add-command", self.now)
            finally:
                store.close()

            store = Store(path)
            try:
                handler = Commands(store, self.backend)
                await handler.handle("/task reset todo_watch", "setting", self.now)
                self.assertEqual(store.get_setting("interval:todo_watch"), 120)
                self.assertTrue(store.pending_add(self.now))
                await handler.handle("重启后的事项 2026-10-16 01:00:00", "manual", self.now)
                self.assertEqual([task.id for task in store.manual_tasks()], ["manual"])
            finally:
                store.close()

            store = Store(path)
            try:
                self.assertEqual(store.get_setting("interval:todo_watch"), 120)
                self.assertTrue(store.seen_message("manual"))
                self.assertFalse(store.pending_add(self.now))
                handler = Commands(store, self.backend)
                await handler.handle("/time", "time", self.now)
                replies = {row["key"]: row["text"] for row in store.outbox_pending()}
                self.assertIn("todo_watch — 监控待办任务\n  间隔: 120s", replies["reply:time"])
                self.assertIn("添加成功", replies["reply:manual"])
                self.assertEqual(len(replies), 4)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
