"""Offline protocol and orchestration checks; no app, keyring, or live CLI calls."""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runtime
from backend import Backend, BackendError
from domain import TZ
from dws_client import Dws, DwsError


class BackendAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Supplying credentials and a token means these tests cannot enter the
        # existing operator's credential store or authentication flow.
        self.backend = Backend(credentials={'studentid': 'offline', 'password': 'unused'})
        await self.backend.client.aclose()
        self.backend.token = 'offline-token'
        self.calls = []
        self.payloads = {}

        def handle(request):
            self.calls.append(request.url.path)
            value = self.payloads[request.url.path]
            if isinstance(value, Exception):
                raise value
            if isinstance(value, httpx.Response):
                return value
            return httpx.Response(200, json=value)

        self.backend.client = httpx.AsyncClient(
            base_url='http://127.0.0.1:8765', transport=httpx.MockTransport(handle),
        )
        self.now = datetime(2026, 10, 15, 12, tzinfo=TZ)

    async def asyncTearDown(self):
        await self.backend.close()

    def cache_payload(self, data, **metadata):
        return {'status': 'ok', 'stale': False, 'fetched_at': 10000,
                'data': data, **metadata}

    def todo(self, task_id='1', hours=2, **fields):
        return {'id': task_id, 'course_name': '课程甲', 'title': '实验报告',
                'end_time': (self.now + timedelta(hours=hours)).isoformat(), **fields}

    async def test_raw_todo_and_rollcall_cache_shapes_and_deadline_parsing(self):
        radar = {'rollcall_id': 7, 'course_title': '高数', 'is_radar': True}
        self.payloads['/api/data/todo_watch'] = self.cache_payload({'todo_list': [self.todo()]})
        self.payloads['/api/data/rollcall_watch'] = self.cache_payload({'rollcalls': [radar]})
        with patch('backend.time.time', return_value=10000):
            tasks = await self.backend.get_tasks()
            self.assertEqual(await self.backend.get_rollcalls(), [radar])
        self.assertEqual(tasks[0].deadline, self.now + timedelta(hours=2))
        self.assertEqual((tasks[0].course, tasks[0].title), ('课程甲', '实验报告'))
        self.assertIsNone(tasks[0].submitted)

    async def test_stale_missing_metadata_and_future_timestamps_are_rejected(self):
        cases = [
            {'stale': True}, {'status': 'unknown'}, {'fetched_at': None},
            {'fetched_at': '10000'}, {'fetched_at': 10011},
        ]
        for change in cases:
            with self.subTest(change=change):
                self.payloads['/api/data/todo_watch'] = self.cache_payload({'todo_list': []}, **change)
                with patch('backend.time.time', return_value=10000):
                    with self.assertRaises(BackendError):
                        await self.backend.get_tasks()
        self.payloads['/api/data/todo_watch'] = {'status': 'ok', 'data': {'todo_list': []}}
        with self.assertRaises(BackendError):
            await self.backend.get_tasks()

    async def test_pending_cache_can_become_ready_without_upstream_query(self):
        values = [httpx.Response(200, json={'status': 'pending'}), httpx.Response(
            200, json=self.cache_payload({'todo_list': []}))]
        await self.backend.client.aclose()
        self.backend.client = httpx.AsyncClient(
            base_url='http://127.0.0.1:8765',
            transport=httpx.MockTransport(lambda _: values.pop(0)),
        )
        with patch('backend.asyncio.sleep', new=AsyncMock()) as sleep:
            with patch('backend.time.time', return_value=10000):
                self.assertEqual(await self.backend.get_tasks(), [])
        sleep.assert_awaited_once_with(1)

    async def test_unavailable_cache_does_not_make_submission_request(self):
        self.payloads['/api/data/todo_watch'] = self.cache_payload(
            {'todo_list': [self.todo()]}, stale=True,
        )
        with self.assertRaises(BackendError):
            await self.backend.checked_tasks(self.now)
        self.assertEqual(self.calls, ['/api/data/todo_watch'])

    async def test_submission_requires_exact_boolean_and_failure_remains_unknown(self):
        self.payloads['/api/data/todo_watch'] = self.cache_payload({'todo_list': [self.todo()]})
        for submitted, expected in ((False, False), (True, True), (None, None),
                                    ('false', None), (0, None), (1, None)):
            with self.subTest(submitted=submitted):
                self.payloads['/api/assignments/submission/1'] = {
                    'status': 'ok' if isinstance(submitted, bool) else 'unknown',
                    'submitted': submitted,
                }
                with patch('backend.time.time', return_value=10000):
                    tasks = await self.backend.checked_tasks(self.now)
                self.assertIs(tasks[0].submitted, expected)
        self.payloads['/api/assignments/submission/1'] = httpx.Response(503)
        with patch('backend.time.time', return_value=10000):
            self.assertIsNone((await self.backend.checked_tasks(self.now))[0].submitted)

    async def test_submission_is_not_requested_for_expired_remote_or_undated_tasks(self):
        self.payloads['/api/data/todo_watch'] = self.cache_payload({'todo_list': [
            self.todo('expired', -1), self.todo('future', 25), self.todo('none', end_time=None),
        ]})
        with patch('backend.time.time', return_value=10000):
            tasks = await self.backend.checked_tasks(self.now)
        self.assertEqual(len(tasks), 3)
        self.assertTrue(all(task.submitted is None for task in tasks))
        self.assertEqual(self.calls, ['/api/data/todo_watch'])

    async def test_server_task_minimums_raise_floors_and_require_enabled_monitors(self):
        self.payloads['/api/tasks'] = {'tasks': [
            {'task_id': 'rollcall_watch', 'interval': 45, 'enabled': True},
            {'task_id': 'todo_watch', 'interval': 10, 'enabled': True},
        ]}
        self.assertEqual(await self.backend.get_minimums(), {'rollcall_watch': 45, 'todo_watch': 60})
        self.payloads['/api/tasks']['tasks'][0]['enabled'] = False
        with self.assertRaises(BackendError):
            await self.backend.get_minimums()


class DwsAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dws = Dws({'recipient_open_dingtalk_id': 'fixed-friend'})
        self.notice = {'key': 'offline-event', 'text': '离线测试，不发网络消息',
                       'kind': 'chat', 'idempotency_key': str(uuid4())}

    @staticmethod
    def chat_receipt():
        return {'identity': 'user', 'ok': True, 'result': {
            'success': True, 'result': {'openTaskId': 'offline-chat-task'},
        }}

    @staticmethod
    def ding_receipt():
        return {'ok': True, 'data': {
            'success': True, 'result': {'openDingId': 'offline-ding-task'},
        }}

    async def test_real_chat_acceptance_shape_disables_ai_tag_and_retains_idempotency(self):
        self.dws.run = AsyncMock(return_value=self.chat_receipt())
        self.assertEqual(await self.dws.send(self.notice), 'accepted')
        first = self.dws.run.await_args.args[0]
        self.assertEqual(first[:2], ['chat', '+messages-send'])
        self.assertIn('--ai-tag=false', first)
        self.assertEqual(first[first.index('--open-dingtalk-id') + 1], 'fixed-friend')
        await self.dws.send(self.notice)
        self.assertEqual(self.dws.run.await_args.args[0], first)

    async def test_incomplete_chat_success_does_not_claim_acceptance(self):
        for payload in ({}, {'ok': False}, {'ok': True},
                        {'ok': True, 'result': {'success': True}},
                        {'ok': True, 'result': {'success': True, 'result': {}}}):
            with self.subTest(payload=payload):
                self.dws.run = AsyncMock(return_value=payload)
                self.assertEqual(await self.dws.send(self.notice), 'unknown')

    async def test_real_ding_acceptance_is_app_only_without_chat_fallback(self):
        self.dws.run = AsyncMock(return_value=self.ding_receipt())
        notice = {**self.notice, 'kind': 'ding'}
        self.assertEqual(await self.dws.send(notice), 'accepted')
        self.assertEqual(self.dws.run.await_count, 1)
        args = self.dws.run.await_args.args[0]
        self.assertEqual(args[:2], ['ding', '+send-personal'])
        self.assertEqual(args[args.index('--type') + 1], 'app')
        self.assertEqual(args[args.index('--uuid') + 1], notice['idempotency_key'])

    async def test_contradictory_or_incomplete_ding_receipt_never_claims_acceptance(self):
        for payload in (
            {'ok': False, 'data': {'success': True, 'result': {'openDingId': 'unexpected'}}},
            {'data': {'success': True, 'result': {'openDingId': 'unexpected'}}},
            {'ok': True, 'data': {'success': True}},
            {'ok': True, 'data': {}},
        ):
            with self.subTest(payload=payload):
                self.dws.run = AsyncMock(return_value=payload)
                self.assertEqual(await self.dws.send({**self.notice, 'kind': 'ding'}), 'unknown')
                self.assertEqual(self.dws.run.await_count, 1)

    async def test_ding_timeout_is_unknown_and_never_falls_back_to_chat(self):
        self.dws.run = AsyncMock(side_effect=DwsError('timeout'))
        self.assertEqual(await self.dws.send({**self.notice, 'kind': 'ding'}), 'unknown')
        self.assertEqual(self.dws.run.await_count, 1)
        self.assertEqual(self.dws.run.await_args.args[0][:2], ['ding', '+send-personal'])

    async def test_explicit_ding_rejection_has_exactly_one_chat_fallback(self):
        self.dws.run = AsyncMock(side_effect=[
            {'ok': True, 'data': {'success': False}}, self.chat_receipt(),
        ])
        self.assertEqual(await self.dws.send({**self.notice, 'kind': 'ding'}), 'accepted')
        self.assertEqual(self.dws.run.await_count, 2)
        self.assertEqual([call.args[0][:2] for call in self.dws.run.await_args_list],
                         [['ding', '+send-personal'], ['chat', '+messages-send']])

    async def test_chat_timeout_does_not_retry_or_change_idempotency(self):
        self.dws.run = AsyncMock(side_effect=DwsError('timeout'))
        self.assertEqual(await self.dws.send(self.notice), 'unknown')
        self.assertEqual(self.dws.run.await_count, 1)


class RuntimeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge = runtime.Bridge({
            'state_db': str(Path(self.tmp.name) / 'state.sqlite3'),
            'recipient_open_dingtalk_id': 'fixed-friend',
            'conversation_id': 'fixed-private-conversation',
        })
        await self.bridge.backend.close()
        self.backend = SimpleNamespace(
            get_tasks=AsyncMock(return_value=[]), checked_tasks=AsyncMock(return_value=[]),
            get_rollcalls=AsyncMock(return_value=[]), get_minimums=AsyncMock(return_value={
                'rollcall_watch': 45, 'todo_watch': 120,
            }), close=AsyncMock(),
        )
        self.bridge.backend = self.backend
        self.bridge.commands.backend = self.backend
        self.bridge.dws.run = AsyncMock(side_effect=AssertionError('No real CLI allowed.'))
        self.now = datetime.now(TZ).replace(microsecond=0)
        self.bridge.history_cursor = self.now.isoformat()

    async def asyncTearDown(self):
        self.bridge.store.close()
        self.tmp.cleanup()

    def message(self, text='/help', message_id='message-1', **fields):
        return {
            'sender_open_dingtalk_id': 'fixed-friend',
            'conversation_id': 'fixed-private-conversation',
            'message_id': message_id, 'content': text,
            'createTime': (self.now + timedelta(seconds=1)).isoformat(), **fields,
        }

    async def test_requires_fixed_friend_conversation_and_stable_message_id(self):
        invalid = [
            self.message(sender_open_dingtalk_id='another-friend'),
            self.message(conversation_id='another-conversation'),
            self.message(message_id=None), self.message(content={'text': '/help'}),
        ]
        for message in invalid:
            await self.bridge.incoming(message)
        self.assertEqual(self.bridge.store.outbox_pending(), [])
        self.assertFalse(self.bridge.store.seen_message('message-1'))
        await self.bridge.incoming(self.message())
        self.assertTrue(self.bridge.store.seen_message('message-1'))
        self.assertEqual([n['key'] for n in self.bridge.store.outbox_pending()], ['reply:message-1'])

    async def test_stream_and_history_duplicate_message_only_replies_and_queries_once(self):
        # stream_loop forwards its decoded NDJSON object to incoming unchanged.
        message = self.message('/assignment')
        self.bridge.dws.history = AsyncMock(return_value={'messages': [message], 'complete': True})
        await asyncio.gather(self.bridge.incoming(message), self.bridge.history_once())
        self.backend.get_tasks.assert_awaited_once()
        self.assertEqual([n['key'] for n in self.bridge.store.outbox_pending()], ['reply:message-1'])
        self.assertEqual(self.bridge.store.get_setting('history_cursor'), self.bridge.history_cursor)

    async def test_history_uses_messages_field_and_ignores_old_commands(self):
        old = self.message('/assignment', 'old', createTime=(self.now - timedelta(seconds=1)).isoformat())
        current = self.message('/help', 'current')
        self.bridge.dws.history = AsyncMock(return_value={
            'messages': [old, current], 'complete': True,
        })
        await self.bridge.history_once()
        self.assertFalse(self.bridge.store.seen_message('old'))
        self.assertTrue(self.bridge.store.seen_message('current'))
        self.backend.get_tasks.assert_not_awaited()
        self.bridge.dws.history = AsyncMock(return_value={'items': [current], 'complete': True})
        with self.assertRaises(RuntimeError):
            await self.bridge.history_once()

    async def test_history_overlap_keeps_new_messages_with_second_precision(self):
        self.bridge.history_floor = (self.now - timedelta(minutes=1)).isoformat()
        self.bridge.history_cursor = (self.now + timedelta(microseconds=500000)).isoformat()
        # The source truncates createTime to seconds; this can be newer than the
        # last query boundary despite looking half a second older than its cursor.
        message = self.message('/help', 'same-second', createTime=self.now.isoformat())
        self.bridge.dws.history = AsyncMock(return_value={'messages': [message], 'complete': True})
        await self.bridge.history_once()
        self.assertTrue(self.bridge.store.seen_message('same-second'))
        self.assertEqual(len(self.bridge.store.outbox_pending()), 1)
        await self.bridge.history_once()
        self.assertEqual(len(self.bridge.store.outbox_pending()), 1)

    async def test_partial_history_preserves_cursor_and_reports_failure(self):
        before = self.bridge.history_cursor
        self.bridge.dws.history = AsyncMock(return_value={'messages': [], 'complete': False})
        with self.assertRaises(RuntimeError):
            await self.bridge.history_once()
        self.assertEqual(self.bridge.history_cursor, before)
        self.assertEqual(self.bridge.errors['history'], 'partial_response')

    async def test_held_deadlines_do_not_block_replies_rollcalls_or_eligible_deadlines(self):
        for index in range(25):
            self.bridge.store.enqueue(f'deadline:held:{index}', 'Held')
        self.bridge.store.enqueue('reply:urgent', 'Help')
        self.bridge.store.enqueue('rollcall:new', 'Rollcall', kind='ding')
        self.bridge.store.enqueue('deadline:eligible', 'Eligible')
        self.bridge.allowed_deadlines = {'deadline:eligible'}
        notices = self.bridge.store.outbox_sendable(self.bridge.allowed_deadlines)
        self.assertEqual([row['key'] for row in notices],
                         ['reply:urgent', 'rollcall:new', 'deadline:eligible'])

    async def test_unknown_submission_checks_remain_visible_in_runtime_status(self):
        self.backend.submission_errors = {'1': 'unsupported_type'}
        await self.bridge.poll_once('todo_watch')
        self.assertEqual(self.bridge.errors['submission_checks'], 1)
        self.backend.submission_errors = {}
        await self.bridge.poll_once('todo_watch')
        self.assertNotIn('submission_checks', self.bridge.errors)

    async def test_restart_does_not_send_queued_rollcall_that_has_ended(self):
        self.bridge.store.enqueue('rollcall:old', '正在签到', kind='ding')
        self.backend.get_rollcalls.return_value = []
        await self.bridge.poll_once('rollcall_watch')
        self.assertEqual(self.bridge.allowed_rollcalls, set())
        self.assertEqual(self.bridge.store.outbox_pending(), [])
        self.assertEqual(self.bridge.store.outbox_stats()['skipped'], 1)

    async def test_rollcall_query_failure_holds_notification_without_claiming_end(self):
        self.bridge.store.enqueue('rollcall:waiting', '正在签到', kind='ding')
        self.backend.get_rollcalls.side_effect = BackendError('Cache stale')
        await self.bridge.poll_once('rollcall_watch')
        self.assertEqual(len(self.bridge.store.outbox_pending()), 1)
        self.assertEqual(self.bridge.store.outbox_sendable(
            set(), allowed_rollcalls=self.bridge.allowed_rollcalls), [])

    async def test_deadline_expiring_between_poll_and_delivery_is_not_sent(self):
        self.bridge.store.enqueue('deadline:expired', 'Outdated')
        self.bridge.allowed_deadlines = {'deadline:expired'}
        self.bridge.deadline_expiries = {'deadline:expired': self.now - timedelta(seconds=1)}
        self.bridge.dws.send = AsyncMock()

        async def stop_on_sleep(_):
            self.bridge.stop.set()

        with patch.object(runtime.asyncio, 'sleep', side_effect=stop_on_sleep):
            await self.bridge.dispatch_loop()
        self.bridge.dws.send.assert_not_awaited()
        self.assertEqual(self.bridge.store.outbox_stats()['skipped'], 1)

    async def test_interval_change_and_reset_do_not_reset_last_poll_or_exceed_server_floor(self):
        self.bridge.minimums = {'rollcall_watch': 45, 'todo_watch': 120}
        await self.bridge.incoming(self.message('/task interval todo_watch 180', 'set-180'))
        self.assertEqual(self.bridge.interval('todo_watch'), 180)
        await self.bridge.incoming(self.message('/task interval todo_watch 1', 'too-fast'))
        self.assertEqual(self.bridge.interval('todo_watch'), 180)
        self.bridge.store.set_setting('interval:rollcall_watch', 1)
        self.assertEqual(self.bridge.interval('rollcall_watch'), 45)

        clock = SimpleNamespace(value=1000.0, changed=False)
        polls = []

        async def poll(task_id):
            polls.append(clock.value)
            self.bridge.last_poll_mono[task_id] = clock.value

        async def sleep(delay):
            clock.value += delay
            if clock.value >= 1100 and not clock.changed:
                clock.changed = True
                await self.bridge.incoming(self.message('/task reset todo_watch', 'reset-120'))
            if clock.value >= 1250:
                self.bridge.stop.set()

        self.bridge.poll_once = AsyncMock(side_effect=poll)
        with patch.object(runtime, 'time', SimpleNamespace(monotonic=lambda: clock.value)):
            with patch.object(runtime, 'asyncio', SimpleNamespace(sleep=sleep)):
                await self.bridge.poll_loop('todo_watch')
        self.assertEqual(polls, [1000, 1120, 1240])
        self.assertTrue(all(b - a >= 120 for a, b in zip(polls, polls[1:])))

    async def test_new_manual_task_is_eligible_immediately_without_waiting_for_upstream(self):
        self.backend.checked_tasks.side_effect = BackendError('School unavailable.')
        await self.bridge.incoming(self.message('/assignment_add', 'add-prompt'))
        deadline = self.now + timedelta(hours=2)
        await self.bridge.incoming(self.message(
            f'本地复习 {deadline:%Y-%m-%d %H:%M:%S}', 'manual-item',
        ))
        self.backend.checked_tasks.assert_not_awaited()
        self.backend.get_tasks.assert_not_awaited()
        reminders = [n for n in self.bridge.store.outbox_pending() if n['key'].startswith('deadline:')]
        self.assertEqual(len(reminders), 1)
        self.assertIn('手动待办“本地复习”', reminders[0]['text'])
        self.assertIn(reminders[0]['key'], self.bridge.allowed_deadlines)

    async def test_manual_reminders_still_queue_when_school_poll_fails(self):
        self.bridge.store.add_manual('local', '离线也提醒', self.now + timedelta(hours=2))
        self.backend.checked_tasks.side_effect = BackendError('School unavailable.')
        await self.bridge.poll_once('todo_watch')
        reminders = [n for n in self.bridge.store.outbox_pending() if n['key'].startswith('deadline:')]
        self.assertEqual(len(reminders), 1)
        self.assertIn(reminders[0]['key'], self.bridge.allowed_deadlines)
        self.assertEqual(self.bridge.errors['todo_watch'], 'BackendError')


if __name__ == '__main__':
    unittest.main()
