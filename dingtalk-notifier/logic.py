"""Reminder decisions only: this module never sends a message."""
from domain import parse_time


class Reminders:
    THRESHOLDS = (24, 12, 3)

    def __init__(self, store):
        self.store = store

    @staticmethod
    def _deadline_key(task, hours):
        return f'deadline:{task.reminder_key}:{hours}h'

    @staticmethod
    def _deadline_text(task, now):
        subject = (f'手动待办“{task.title}”' if task.source == 'manual'
                   else f'课程《{task.course}》的待办“{task.title}”')
        deadline = parse_time(task.deadline)
        deadline_text = f'{deadline.month}月{deadline.day}日{deadline:%H:%M}'
        remaining = (deadline - parse_time(now)).total_seconds() / 3600
        hours_text = f'{remaining:.1f}'.rstrip('0').rstrip('.')
        return f'{subject}截止时间为{deadline_text}，还剩{hours_text}小时！'

    def queue_deadlines(self, tasks, now):
        current = parse_time(now)
        count = 0
        for task in tasks:
            # Unknown submission state is not evidence of non-submission.
            if task.submitted is not False or task.deadline is None:
                continue
            remaining = (parse_time(task.deadline) - current).total_seconds()
            eligible = [hours for hours in self.THRESHOLDS if 0 < remaining < hours * 3600]
            if not eligible:
                continue
            selected = min(eligible)
            earlier_keys = []
            for hours in self.THRESHOLDS:
                if hours <= selected:
                    continue
                key = self._deadline_key(task, hours)
                self.store.enqueue(key, self._deadline_text(task, current), status='skipped')
                earlier_keys.append(key)
            # A queued but unsent earlier threshold can survive a restart. Retire
            # it too, while preserving accepted/unknown delivery records.
            self.store._skip_pending(earlier_keys)
            if self.store.enqueue(
                self._deadline_key(task, selected), self._deadline_text(task, current),
            ):
                count += 1
        return count

    def queue_rollcalls(self, raw_items, now):
        if isinstance(raw_items, dict):
            raw_items = raw_items.get('rollcalls', [])
        count = 0
        for item in raw_items or []:
            if not isinstance(item, dict):
                continue
            rollcall_id = item.get('rollcall_id')
            if rollcall_id is None or rollcall_id == '':
                continue
            types = {str(item.get(field, '')).strip().lower()
                     for field in ('type', 'source', 'rollcall_type')}
            if item.get('is_radar') is True or 'radar' in types:
                mode = '雷达点名'
            elif 'number' in types:
                mode = '数字点名'
            else:
                mode = '点名'
            course = item.get('course_title') or item.get('course_name') or '未知课程'
            if self.store.enqueue(
                f'rollcall:{rollcall_id}', f'课程《{course}》正在{mode}！！！', kind='ding',
            ):
                count += 1
        return count
