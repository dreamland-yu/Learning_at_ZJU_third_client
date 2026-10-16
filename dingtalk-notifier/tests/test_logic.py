"""Offline boundary checks for durable reminder decisions."""
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from domain import Task, TZ
from logic import Reminders
from state_store import Store


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.sqlite3'
        self.store = Store(self.path)
        self.reminders = Reminders(self.store)
        self.now = datetime(2026, 10, 15, 12, tzinfo=TZ)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def task(self, hours=30, task_id='1', submitted=False, **kwargs):
        return Task(
            id=task_id, course='课程甲', title='作业甲',
            deadline=self.now + timedelta(hours=hours), submitted=submitted, **kwargs,
        )

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.reminders = Reminders(self.store)

    def status(self, key):
        with sqlite3.connect(self.path) as connection:
            row = connection.execute('SELECT status FROM outbox WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def test_three_thresholds_strict_boundaries_and_one_per_tick(self):
        task = self.task()
        for hours, selected in ((24, None), (23.9, 24), (12, None),
                                (11.9, 12), (3, None), (2.9, 3)):
            now = task.deadline - timedelta(hours=hours)
            self.assertEqual(self.reminders.queue_deadlines([task], now), int(selected is not None))
            self.assertEqual(self.reminders.queue_deadlines([task], now), 0)
            if selected is not None:
                notice = self.store.outbox_pending()[0]
                self.assertTrue(notice['key'].endswith(f':{selected}h'))
                self.assertIn(f'还剩{hours}小时！', notice['text'])
                self.assertNotIn('尚未提交', notice['text'])
                self.store.finish_notice(notice['key'], 'accepted')
        self.assertEqual(self.reminders.queue_deadlines([task], task.deadline), 0)

    def test_deadline_messages_use_actual_hours_local_date_and_manual_wording(self):
        lazy = self.task(hours=23.9)
        manual = self.store.add_manual('manual', '考试复习', self.now + timedelta(hours=2))
        self.assertEqual(self.reminders.queue_deadlines([lazy, manual], self.now), 2)
        self.assertEqual([n['text'] for n in self.store.outbox_pending()], [
            '课程《课程甲》的待办“作业甲”截止时间为10月16日11:54，还剩23.9小时！',
            '手动待办“考试复习”截止时间为10月15日14:00，还剩2小时！',
        ])
        for notice in self.store.outbox_pending():
            self.assertNotIn('尚未提交', notice['text'])
            self.assertNotIn('.0小时', notice['text'])

    def test_restart_keeps_uuid_and_does_not_queue_again(self):
        task = self.task(hours=20)
        self.assertEqual(self.reminders.queue_deadlines([task], self.now), 1)
        before = self.store.outbox_pending()[0]
        UUID(before['idempotency_key'])
        self.reopen()
        self.assertEqual(self.reminders.queue_deadlines([task], self.now), 0)
        self.assertEqual(self.store.outbox_pending(), [before])
        self.store.finish_notice(before['key'], 'unknown', 'Delivery outcome is unknown.')
        self.reopen()
        self.assertEqual(self.store.outbox_pending(), [])
        self.assertEqual(self.reminders.queue_deadlines([task], self.now), 0)

    def test_start_below_three_hours_skips_earlier_thresholds(self):
        task = self.task(hours=2)
        self.assertEqual(self.reminders.queue_deadlines([task], self.now), 1)
        self.assertEqual(len(self.store.outbox_pending()), 1)
        for hours in (24, 12):
            self.assertEqual(self.status(self.reminders._deadline_key(task, hours)), 'skipped')
        self.assertTrue(self.store.outbox_pending()[0]['key'].endswith(':3h'))

    def test_pending_older_threshold_is_skipped_after_offline_gap(self):
        task = self.task(hours=20)
        self.reminders.queue_deadlines([task], self.now)
        older = self.store.outbox_pending()[0]['key']
        self.reopen()
        self.assertEqual(self.reminders.queue_deadlines([task], self.now + timedelta(hours=18)), 1)
        self.assertEqual(self.status(older), 'skipped')
        self.assertEqual(len(self.store.outbox_pending()), 1)
        self.assertTrue(self.store.outbox_pending()[0]['key'].endswith(':3h'))

    def test_crossing_threshold_does_not_overwrite_accepted_outcome(self):
        task = self.task(hours=20)
        self.reminders.queue_deadlines([task], self.now)
        older = self.store.outbox_pending()[0]['key']
        self.store.finish_notice(older, 'accepted')
        self.reminders.queue_deadlines([task], self.now + timedelta(hours=18))
        self.assertEqual(self.status(older), 'accepted')

    def test_deadline_change_starts_new_round(self):
        first = self.task(hours=20)
        self.reminders.queue_deadlines([first], self.now)
        old_key = self.store.outbox_pending()[0]['key']
        self.store.finish_notice(old_key, 'accepted')
        changed = self.task(hours=21)
        self.assertNotEqual(changed.reminder_key, first.reminder_key)
        self.assertEqual(self.reminders.queue_deadlines([changed], self.now), 1)
        self.assertNotEqual(self.store.outbox_pending()[0]['key'], old_key)

    def test_submitted_unknown_expired_and_no_deadline_are_skipped(self):
        tasks = [self.task(2, 'submitted', True), self.task(2, 'unknown', None),
                 self.task(-1, 'expired'), self.task(0, 'now'),
                 Task('no-date', '课', '无截止', None, submitted=False)]
        self.assertEqual(self.reminders.queue_deadlines(tasks, self.now), 0)
        self.assertEqual(self.store.outbox_pending(), [])
        # Unknown does not consume a future chance to remind once verified.
        self.assertEqual(self.reminders.queue_deadlines([self.task(2, 'unknown')], self.now), 1)

    def test_multiple_tasks_and_sources_are_independent(self):
        tasks = [self.task(20, 'same'), self.task(10, 'second'),
                 self.task(2, 'same', source='manual')]
        self.assertEqual(self.reminders.queue_deadlines(tasks, self.now), 3)
        notices = self.store.outbox_pending()
        self.assertEqual(len({n['key'] for n in notices}), 3)
        self.assertEqual(len({n['idempotency_key'] for n in notices}), 3)
        self.assertEqual(self.reminders.queue_deadlines(tasks, self.now), 0)

    def test_rollcall_disappears_reappears_and_survives_restart(self):
        radar = {'rollcall_id': 7, 'course_title': '微积分', 'is_radar': True}
        self.assertEqual(self.reminders.queue_rollcalls([radar], self.now), 1)
        notice = self.store.outbox_pending()[0]
        self.assertEqual(notice['kind'], 'ding')
        self.assertEqual(notice['text'], '课程《微积分》正在雷达点名！！！')
        self.assertEqual(self.reminders.queue_rollcalls([], self.now), 0)
        self.assertEqual(self.reminders.queue_rollcalls([radar], self.now), 0)
        self.reopen()
        self.assertEqual(self.reminders.queue_rollcalls([radar], self.now), 0)
        self.assertEqual(self.store.outbox_pending(), [notice])

    def test_explicit_number_type_and_unknown_non_radar(self):
        items = [
            {'rollcall_id': 1, 'course_name': '数分', 'type': 'number', 'is_radar': False},
            {'rollcall_id': 2, 'course_title': '线代', 'is_radar': False},
            {'rollcall_id': 3, 'course_title': '物理', 'source': 'number'},
            {'course_title': '缺ID'}, None,
        ]
        self.assertEqual(self.reminders.queue_rollcalls({'rollcalls': items}, self.now), 3)
        texts = [n['text'] for n in self.store.outbox_pending()]
        self.assertEqual(texts, ['课程《数分》正在数字点名！！！', '课程《线代》正在点名！！！',
                                 '课程《物理》正在数字点名！！！'])

    def test_store_settings_manual_tasks_message_dedup_and_pending_input(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.store.set_setting('intervals', {'todo': 60, 'enabled': True})
        self.assertEqual(self.store.get_setting('missing', 123), 123)
        task = self.store.add_manual('abc', '考试复习', self.now + timedelta(hours=2))
        duplicate = self.store.add_manual('abc', '不覆盖原内容', self.now + timedelta(hours=4))
        self.assertEqual(task, duplicate)
        self.assertEqual((task.course, task.source, task.submitted), ('手动待办', 'manual', False))
        self.store.set_pending_add(self.now + timedelta(minutes=10))
        self.assertTrue(self.store.pending_add(self.now))
        self.assertFalse(self.store.pending_add(self.now + timedelta(minutes=10)))
        self.assertFalse(self.store.seen_message('msg-1'))
        self.store.mark_message('msg-1')
        self.store.mark_message('msg-1')
        self.reopen()
        self.assertEqual(self.store.get_setting('intervals'), {'todo': 60, 'enabled': True})
        self.assertEqual(self.store.manual_tasks(), [task])
        self.assertTrue(self.store.seen_message('msg-1'))
        self.assertTrue(self.store.pending_add(self.now))
        self.store.clear_pending_add()
        self.assertFalse(self.store.pending_add(self.now))
        self.assertEqual(self.reminders.queue_deadlines(self.store.manual_tasks(), self.now), 1)

    def test_enqueue_retains_original_body_and_idempotency_and_limits_pending(self):
        self.assertTrue(self.store.enqueue('a', '原文'))
        self.assertTrue(self.store.enqueue('b', '第二条', kind='ding'))
        first = self.store.outbox_pending(limit=1)[0]
        self.assertFalse(self.store.enqueue('a', '改文', kind='ding'))
        self.assertEqual(self.store.outbox_pending(limit=1), [first])
        self.assertEqual(self.store.outbox_pending(limit=0), [])
        self.store.finish_notice('a', 'accepted')
        self.assertTrue(self.store.has_notice('a'))
        self.assertEqual([n['key'] for n in self.store.outbox_pending()], ['b'])


if __name__ == '__main__':
    unittest.main()
