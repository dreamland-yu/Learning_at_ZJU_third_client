"""Concurrent message reception, local cache reads, and persistent delivery queue."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta

import httpx

from backend import Backend
from commands import Commands
from domain import MINIMUM_INTERVALS, TZ, parse_time
from dws_client import Dws
from logic import Reminders
from state_store import Store

LOG = logging.getLogger('lazy-ding')
ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / '.runtime'


class LocalServer:
    def __init__(self):
        self.process = None

    async def start(self):
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            try:
                response = await client.get('http://127.0.0.1:8765/api/health')
                if response.status_code == 200 and response.json().get('status') == 'ok':
                    return
            except (httpx.HTTPError, ValueError):
                pass
            RUNTIME.mkdir(exist_ok=True)
            with (RUNTIME / 'server.log').open('ab') as output:
                self.process = subprocess.Popen(
                    [sys.executable, '-m', 'uvicorn', 'lazy.server.app:app', '--host',
                     '127.0.0.1', '--port', '8765', '--no-access-log'],
                    cwd=ROOT.parent, stdout=output, stderr=output)
            for _ in range(20):
                if self.process.poll() is not None:
                    raise RuntimeError('Local Server failed to start; see private server.log.')
                await asyncio.sleep(0.5)
                try:
                    response = await client.get('http://127.0.0.1:8765/api/health')
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
        raise RuntimeError('Local Server readiness timed out.')

    async def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(self.process.wait), 8)
            except asyncio.TimeoutError:
                LOG.error('Local Server is still shutting down.')


class Bridge:
    def __init__(self, config):
        self.config = config
        self.store = Store(config['state_db'])
        self.store.recover_inflight()
        self.backend = Backend(config.get('server_url', 'http://127.0.0.1:8765'))
        self.dws = Dws(config)
        self.commands = Commands(self.store, self.backend)
        self.reminders = Reminders(self.store)
        self.server = LocalServer()
        self.stop = asyncio.Event()
        self.stream_process = None
        self.minimums = dict(MINIMUM_INTERVALS)
        self.allowed_deadlines = set()
        self.deadline_expiries = {}
        self.allowed_rollcalls = set()
        self.last_poll = {}
        self.last_poll_mono = {key: 0 for key in MINIMUM_INTERVALS}
        self.errors = {}
        self.stream_ready = False
        self.started_at = datetime.now(TZ)
        self.message_lock = asyncio.Lock()
        self.history_cursor = self.store.get_setting('history_cursor')
        if not self.history_cursor:
            self.history_cursor = self.started_at.replace(microsecond=0).isoformat()
            self.store.set_setting('history_cursor', self.history_cursor)
        self.history_floor = self.store.get_setting('history_floor', self.history_cursor)
        self.store.set_setting('history_floor', self.history_floor)

    def interval(self, task_id):
        saved = self.store.get_setting('interval:' + task_id, self.minimums[task_id])
        if not isinstance(saved, int) or isinstance(saved, bool):
            saved = self.minimums[task_id]
        return max(self.minimums[task_id], saved)

    async def poll_once(self, task_id):
        now = datetime.now(TZ)
        try:
            if task_id == 'rollcall_watch':
                rows = await self.backend.get_rollcalls()
                self.allowed_rollcalls = {
                    f"rollcall:{row['rollcall_id']}" for row in rows
                    if isinstance(row, dict) and row.get('rollcall_id') is not None}
                self.store.retire_ended_rollcalls(self.allowed_rollcalls)
                self.reminders.queue_rollcalls(rows, now)
            else:
                tasks = await self.backend.checked_tasks(now)
                tasks += self.store.manual_tasks()
                self.allowed_deadlines = {
                    f'deadline:{task.reminder_key}:{hours}h'
                    for task in tasks if task.deadline and task.submitted is False
                    and 0 < (task.deadline - now).total_seconds() < 86400
                    for hours in (24, 12, 3)
                }
                self.deadline_expiries = {
                    f'deadline:{task.reminder_key}:{hours}h': task.deadline
                    for task in tasks if task.deadline and task.submitted is False
                    for hours in (24, 12, 3)}
                self.reminders.queue_deadlines(tasks, now)
                unknown = getattr(self.backend, 'submission_errors', {})
                if unknown:
                    self.errors['submission_checks'] = len(unknown)
                else:
                    self.errors.pop('submission_checks', None)
            self.last_poll[task_id] = now.isoformat()
            self.errors.pop(task_id, None)
        except Exception as error:
            self.errors[task_id] = type(error).__name__
            LOG.warning('%s query unavailable: %s', task_id, type(error).__name__)
            if task_id == 'todo_watch':
                # Local tasks remain usable even while the school is unavailable.
                tasks = self.store.manual_tasks()
                self.allowed_deadlines = {
                    f'deadline:{task.reminder_key}:{hours}h'
                    for task in tasks if task.deadline and
                    0 < (task.deadline - now).total_seconds() < 86400
                    for hours in (24, 12, 3)
                }
                self.reminders.queue_deadlines(tasks, now)
                self.deadline_expiries = {
                    f'deadline:{task.reminder_key}:{hours}h': task.deadline
                    for task in tasks if task.deadline for hours in (24, 12, 3)}
            else:
                self.allowed_rollcalls.clear()
        self.last_poll_mono[task_id] = time.monotonic()

    async def poll_loop(self, task_id):
        while not self.stop.is_set():
            if time.monotonic() - self.last_poll_mono[task_id] >= self.interval(task_id):
                await self.poll_once(task_id)
            await asyncio.sleep(0.5)

    async def dispatch_loop(self):
        while not self.stop.is_set():
            # Filter before LIMIT: held reminders must not block replies or DINGs.
            for notice in self.store.outbox_sendable(
                    self.allowed_deadlines, allowed_rollcalls=self.allowed_rollcalls):
                if notice['key'].startswith('rollcall:') and notice['key'] not in self.allowed_rollcalls:
                    continue
                if notice['key'].startswith('deadline:') and notice['key'] not in self.allowed_deadlines:
                    continue
                expiry = self.deadline_expiries.get(notice['key'])
                if expiry and expiry <= datetime.now(TZ):
                    self.store.finish_notice(notice['key'], 'skipped', 'Deadline has passed.')
                    continue
                # A crash during a remote call has unknown delivery, never a fresh UUID.
                self.store.finish_notice(notice['key'], 'sending')
                try:
                    status = await self.dws.send(notice)
                except Exception as error:
                    status = 'unknown'
                    LOG.warning('Delivery outcome unknown: %s', type(error).__name__)
                self.store.finish_notice(notice['key'], status)
                if status != 'accepted':
                    self.errors['delivery'] = status
                await asyncio.sleep(0.25)
            await asyncio.sleep(0.5)

    async def incoming(self, data):
        sender = data.get('sender_open_dingtalk_id') or data.get('senderId')
        conversation = data.get('conversation_id') or data.get('conversationId')
        message_id = data.get('message_id') or data.get('messageId')
        if (sender != self.config['recipient_open_dingtalk_id'] or
                conversation != self.config['conversation_id'] or not message_id):
            return
        text = data.get('content', data.get('text', ''))
        if not isinstance(text, str):
            return
        # Some transports wrap a plain text message's content in a JSON string.
        if text.startswith('{'):
            try:
                body = json.loads(text)
                if isinstance(body, dict):
                    text = body.get('content', body.get('text', text))
            except ValueError:
                pass
        if not isinstance(text, str):
            return
        async with self.message_lock:
            before = {task.id for task in self.store.manual_tasks()}
            await self.commands.handle(text, str(message_id), datetime.now(TZ))
            added = [task for task in self.store.manual_tasks() if task.id not in before]
            if added:
                now = datetime.now(TZ)
                self.reminders.queue_deadlines(added, now)
                self.allowed_deadlines.update(
                    f'deadline:{task.reminder_key}:{hours}h'
                    for task in added for hours in (24, 12, 3)
                )
                self.deadline_expiries.update({
                    f'deadline:{task.reminder_key}:{hours}h': task.deadline
                    for task in added for hours in (24, 12, 3)})

    async def history_once(self):
        boundary = datetime.now(TZ)
        start = parse_time(self.history_cursor) - timedelta(seconds=5)
        payload = await self.dws.history(start.isoformat())
        if not isinstance(payload.get('messages'), list):
            raise RuntimeError('History response unavailable.')
        for message in payload['messages']:
            # Initial deployment never interprets old conversations as commands.
            created = parse_time(message.get('createTime'))
            if created is not None and created < parse_time(self.history_floor):
                continue
            await self.incoming(message)
        if payload.get('complete') is True:
            self.history_cursor = boundary.isoformat()
            self.store.set_setting('history_cursor', self.history_cursor)
        else:
            self.errors['history'] = 'partial_response'
            raise RuntimeError('History response is partial; cursor was retained.')

    async def history_loop(self):
        while not self.stop.is_set():
            try:
                await self.history_once()
                self.errors.pop('history', None)
            except Exception as error:
                self.errors['history'] = type(error).__name__
                LOG.warning('Private history check unavailable: %s', type(error).__name__)
            # A read API is a recovery path, not a second interactive consumer.
            delay = 60 if self.stream_ready else 10
            for _ in range(delay):
                if self.stop.is_set():
                    return
                await asyncio.sleep(1)

    async def stream_loop(self):
        while not self.stop.is_set():
            process = None
            stderr_reader = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *self.dws.command(['event', 'consume', 'user_im_message_receive_o2o',
                        '--open-dingtalk-id', self.config['recipient_open_dingtalk_id'],
                        '--flatten', '-f', 'ndjson']),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, env=self.dws.environment(), limit=1024*1024)
                self.stream_process = process

                async def read_status():
                    async for line in process.stderr:
                        if b'[event] ready' in line:
                            self.stream_ready = True
                            self.errors.pop('stream', None)

                stderr_reader = asyncio.create_task(read_status())
                async for line in process.stdout:
                    try:
                        payload = json.loads(line)
                        if isinstance(payload, dict):
                            await self.incoming(payload)
                    except (ValueError, UnicodeError):
                        continue
                await process.wait()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.errors['stream'] = type(error).__name__
            finally:
                self.stream_ready = False
                if stderr_reader:
                    stderr_reader.cancel()
                    await asyncio.gather(stderr_reader, return_exceptions=True)
                if process and process.returncode is None:
                    if process.stdin:
                        process.stdin.close()
                    process.terminate()  # SIGTERM allows the CLI to cancel its subscription.
                    try:
                        await asyncio.wait_for(process.wait(), 8)
                    except asyncio.TimeoutError:
                        LOG.error('Event consumer cleanup is still pending.')
                self.stream_process = None
            if not self.stop.is_set():
                self.errors['stream'] = 'reconnecting'
                await asyncio.sleep(10)

    def write_status(self, running=True):
        data = {
            'running': running, 'pid': os.getpid(), 'started_at': self.started_at.isoformat(),
            'updated_at': datetime.now(TZ).isoformat(), 'stream_ready': self.stream_ready,
            'intervals': {key: self.interval(key) for key in self.minimums},
            'last_poll': self.last_poll, 'errors': self.errors,
            'outbox': self.store.outbox_stats(), 'manual_tasks': len(self.store.manual_tasks()),
        }
        temp = RUNTIME / 'status.tmp'
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        temp.replace(RUNTIME / 'status.json')

    async def status_loop(self):
        while not self.stop.is_set():
            try:
                self.minimums = await self.backend.get_minimums()
                self.errors.pop('server', None)
            except Exception as error:
                self.errors['server'] = type(error).__name__
            self.write_status()
            await asyncio.sleep(5)

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)
        jobs = []
        try:
            await self.server.start()
            await self.backend.authenticate()
            self.minimums = await self.backend.get_minimums()
            # Revalidate queued reminders before enabling any remote sends.
            await self.poll_once('todo_watch')
            await self.poll_once('rollcall_watch')
            self.write_status()
            jobs = [asyncio.create_task(self.poll_loop(key)) for key in self.minimums]
            jobs += [asyncio.create_task(self.dispatch_loop()), asyncio.create_task(self.history_loop()),
                     asyncio.create_task(self.stream_loop()), asyncio.create_task(self.status_loop())]
            stop_task = asyncio.create_task(self.stop.wait())
            done, _ = await asyncio.wait([*jobs, stop_task], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not stop_task and task.exception():
                    raise task.exception()
            stop_task.cancel()
        finally:
            self.stop.set()
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            self.write_status(running=False)
            await self.backend.close()
            await self.server.close()
            self.store.close()
