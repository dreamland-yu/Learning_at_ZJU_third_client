"""Parse the fixed private-chat commands without executing user input as code."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from domain import MINIMUM_INTERVALS, TZ, parse_time


HELP_TEXT = (
    "/assignment：查看所有待办\n\n"
    "/assignment_add：手动添加待办\n\n"
    "/time：设置轮询server时间"
)
UNKNOWN_TEXT = "请使用/help查询可用功能"
ADD_FORMAT = (
    "请输入需要添加的待办和截止时间，格式："
    "<具体事项> <截止时间（xxxx年xx月xx日xx时xx分xx秒）>\n"
    "例如：完成实验报告 2026年10月18日23时59分00秒\n"
    "也支持：完成实验报告 2026-10-18 23:59:00"
)
ADD_PROMPT = ADD_FORMAT + "\n请在10分钟内回复。"
TASK_USAGE = (
    "用法：/task interval <id> <秒> 或 /task reset <id>\n"
    "id 仅支持 rollcall_watch / todo_watch，秒数须为整数且不低于当前下限。"
)
INTERVAL_FAILURE = "查询失败，暂时无法获取 Server 轮询间隔，请稍后重试。"

_MANUAL_CHINESE = re.compile(
    r"(?P<title>.+?)\s+(?P<year>[0-9]{4})年(?P<month>[0-9]{1,2})月"
    r"(?P<day>[0-9]{1,2})日(?P<hour>[0-9]{1,2})时"
    r"(?P<minute>[0-9]{1,2})分(?P<second>[0-9]{1,2})秒",
    re.DOTALL,
)
_MANUAL_ISO = re.compile(
    r"(?P<title>.+?)\s+(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-"
    r"(?P<day>[0-9]{2})\s+(?P<hour>[0-9]{2}):"
    r"(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})",
    re.DOTALL,
)


def _date_text(value):
    return parse_time(value).strftime("%Y年%m月%d日 %H:%M:%S")


def _manual_input(text):
    match = _MANUAL_CHINESE.fullmatch(text) or _MANUAL_ISO.fullmatch(text)
    if not match or not match["title"].strip():
        raise ValueError("invalid manual task format")
    fields = {name: int(match[name]) for name in (
        "year", "month", "day", "hour", "minute", "second"
    )}
    return match["title"].strip(), datetime(**fields, tzinfo=TZ)


class Commands:
    def __init__(self, store, backend):
        self.store = store
        self.backend = backend

    async def handle(self, text, message_id, now):
        """Enqueue one reply, then remember the incoming message's stable ID."""
        if self.store.seen_message(message_id):
            return
        now = parse_time(now)
        text = str(text or "").strip()
        pending = self.store.pending_add(now)

        if text in ("/help", "/assignment", "/assignment_add", "/time"):
            self.store.clear_pending_add()
            if text == "/help":
                reply = HELP_TEXT
            elif text == "/assignment":
                reply = await self._assignments()
            elif text == "/assignment_add":
                self.store.set_pending_add(now + timedelta(minutes=10))
                reply = ADD_PROMPT
            else:
                reply = await self._time()
        elif text.split(maxsplit=1)[:1] == ["/task"]:
            reply = await self._task(text, now)
        elif not text.startswith("/") and pending:
            reply = self._add_manual(text, message_id, now)
        else:
            reply = UNKNOWN_TEXT

        self.store.enqueue("reply:" + message_id, reply)
        self.store.mark_message(message_id)

    async def _assignments(self):
        unavailable = False
        try:
            tasks = list(await self.backend.get_tasks())
        except Exception:
            tasks = []
            unavailable = True
        try:
            tasks += self.store.manual_tasks()
            tasks.sort(key=lambda task: (
                parse_time(task.deadline) if task.deadline else datetime.max.replace(tzinfo=TZ),
                str(task.id),
            ))
            if not tasks:
                if unavailable:
                    return "查询失败，暂时无法获取待办，请稍后重试。"
                return "当前没有待办。"
            rows = (["学在浙大待办暂时无法读取，以下仅为本地手动待办："] if unavailable
                    else ["全部待办（按截止时间排序）："])
            for index, task in enumerate(tasks, 1):
                course = task.course or ("手动待办" if task.source == "manual" else "未注明课程")
                deadline = _date_text(task.deadline) if task.deadline else "未设置"
                rows.append(f"{index}. [{course}] {task.title}\n   截止时间：{deadline}")
            return "\n".join(rows)
        except Exception:
            return "查询失败，暂时无法获取待办，请稍后重试。"

    def _add_manual(self, text, message_id, now):
        try:
            title, deadline = _manual_input(text)
        except ValueError:
            return "格式错误，请填写事项和完整的年月日时分秒。\n" + ADD_FORMAT
        if deadline <= now:
            return "截止时间必须晚于当前时间，请重新填写。\n" + ADD_FORMAT
        self.store.add_manual(message_id, title, deadline)
        self.store.clear_pending_add()
        return f"添加成功：{title}\n截止时间：{_date_text(deadline)}"

    async def _intervals(self):
        upstream = await self.backend.get_minimums()
        minimums = {}
        effective = {}
        for task_id, baseline in MINIMUM_INTERVALS.items():
            minimum = upstream[task_id]
            if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum <= 0:
                raise ValueError("invalid upstream interval")
            minimums[task_id] = max(baseline, minimum)
            saved = self.store.get_setting("interval:" + task_id)
            effective[task_id] = (
                max(minimums[task_id], saved)
                if isinstance(saved, int) and not isinstance(saved, bool)
                else minimums[task_id]
            )
        return minimums, effective

    @staticmethod
    def _interval_text(intervals):
        return (f"rollcall_watch {intervals['rollcall_watch']} s / "
                f"todo_watch {intervals['todo_watch']} s")

    async def _time(self):
        try:
            minimums, intervals = await self._intervals()
        except Exception:
            return INTERVAL_FAILURE
        return (
            "rollcall_watch — 监控进行中的点名\n"
            f"  间隔: {intervals['rollcall_watch']}s\n\n"
            "todo_watch — 监控待办任务\n"
            f"  间隔: {intervals['todo_watch']}s\n\n"
            "请使用/task interval <id> <秒> — 修改轮询间隔\n"
            "/task reset <id> — 恢复默认设置。\n\n"
            "当前下限：" + self._interval_text(minimums)
        )

    async def _task(self, text, now):
        parts = text.split()
        reset = len(parts) == 3 and parts[1] == "reset"
        change = len(parts) == 4 and parts[1] == "interval"
        if not (reset or change) or parts[2] not in MINIMUM_INTERVALS:
            return TASK_USAGE
        if change and not re.fullmatch(r"[0-9]+", parts[3]):
            return TASK_USAGE
        try:
            minimums, intervals = await self._intervals()
        except Exception:
            return INTERVAL_FAILURE
        task_id = parts[2]
        try:
            interval = minimums[task_id] if reset else int(parts[3])
        except ValueError:
            return TASK_USAGE
        if interval < minimums[task_id]:
            return f"{task_id} 的轮询间隔不能低于 {minimums[task_id]} s。"
        self.store.set_setting("interval:" + task_id, interval)
        self.store.clear_pending_add()
        intervals[task_id] = interval
        action = "重置" if reset else "修改"
        return (f"已{action}轮询间隔，现在间隔为 {self._interval_text(intervals)}\n"
                f"当前时间：{_date_text(now)}")
