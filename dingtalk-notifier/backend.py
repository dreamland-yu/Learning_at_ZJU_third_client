"""Local LAZY Server client. Secrets stay in memory and existing credential stores."""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import datetime

import httpx

from domain import MINIMUM_INTERVALS, Task, parse_time


class BackendError(RuntimeError):
    pass


class Backend:
    def __init__(self, url='http://127.0.0.1:8765', credentials=None):
        if url.rstrip('/') != 'http://127.0.0.1:8765':
            raise ValueError('Only the configured local loopback server is supported.')
        self.client = httpx.AsyncClient(base_url=url, timeout=25, trust_env=False)
        self.token = None
        self.credentials = credentials
        self.submission_errors = {}
        self._auth_lock = asyncio.Lock()

    def _load_credentials(self):
        if self.credentials:
            return self.credentials
        from lazy.server.credentials import EncryptedCredentialStore
        store = EncryptedCredentialStore()
        accounts = store.list_users()
        if len(accounts) != 1:
            raise BackendError('Run login_server.py to authorize exactly one local Server account.')
        saved = store.get(accounts[0])
        if not saved or not saved.get('password'):
            raise BackendError('Run login_server.py to authorize the local Server.')
        self.credentials = {'studentid': accounts[0], 'password': saved['password']}
        return self.credentials

    async def authenticate(self, previous_token=None):
        async with self._auth_lock:
            if previous_token is not None and self.token != previous_token:
                return
            credentials = self._load_credentials()
            response = await self.client.post('/api/auth/login', json=credentials)
            if response.status_code == 404:
                response = await self.client.post('/api/auth/register', json=credentials)
            if response.status_code != 200:
                raise BackendError(f'Local Server authentication failed ({response.status_code}).')
            self.token = response.json()['token']

    async def _get(self, path):
        if not self.token:
            await self.authenticate()
        old_token = self.token
        response = await self.client.get(path, params={'token': old_token})
        if response.status_code == 401:
            await self.authenticate(previous_token=old_token)
            response = await self.client.get(path, params={'token': self.token})
        if response.status_code != 200:
            raise BackendError(f'Local Server query failed ({response.status_code}).')
        return response.json()

    async def get_minimums(self):
        payload = await self._get('/api/tasks')
        tasks = {r['task_id']: r for r in payload.get('tasks', [])}
        result = {}
        for key, floor in MINIMUM_INTERVALS.items():
            if key not in tasks or not tasks[key].get('enabled', True):
                raise BackendError('Required Server monitor is disabled or missing.')
            result[key] = max(floor, int(tasks[key]['interval']))
        return result

    async def cache(self, task_id):
        # A newly authenticated monitor may not have completed its first GET yet.
        for attempt in range(8):
            payload = await self._get('/api/data/' + task_id)
            if payload.get('status') != 'pending':
                break
            if attempt < 7:
                await asyncio.sleep(1)
        if payload.get('status') != 'ok' or payload.get('stale', True):
            raise BackendError('Server cache is unavailable or stale.')
        fetched = payload.get('fetched_at')
        if not isinstance(fetched, (int, float)) or fetched > time.time() + 10:
            raise BackendError('Server cache timestamp is invalid.')
        return payload.get('data')

    async def get_tasks(self):
        raw = await self.cache('todo_watch')
        items = raw.get('todo_list') if isinstance(raw, dict) else None
        if not isinstance(items, list):
            raise BackendError('Unexpected todo response.')
        result = []
        for row in items:
            if not isinstance(row, dict) or row.get('id') is None:
                continue
            try:
                deadline = parse_time(row.get('end_time'))
            except (ValueError, TypeError):
                deadline = None
            result.append(Task(str(row['id']), str(row.get('course_name') or '未知课程'),
                               str(row.get('title') or '未命名待办'), deadline))
        return result

    async def checked_tasks(self, now: datetime):
        tasks = await self.get_tasks()
        self.submission_errors = {}
        checked = []
        for task in tasks:
            if not task.deadline or not 0 < (task.deadline - now).total_seconds() < 86400:
                checked.append(task)
                continue
            try:
                status = await self._get('/api/assignments/submission/' + task.id)
                submitted = status.get('submitted')
                if submitted is not True and submitted is not False:
                    submitted = None
                    self.submission_errors[task.id] = str(status.get('last_error') or 'unknown')
            except (BackendError, httpx.HTTPError, ValueError):
                submitted = None
                self.submission_errors[task.id] = 'request_error'
            checked.append(replace(task, submitted=submitted))
        return checked

    async def get_rollcalls(self):
        raw = await self.cache('rollcall_watch')
        rows = raw.get('rollcalls') if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            raise BackendError('Unexpected rollcall response.')
        return rows

    async def close(self):
        await self.client.aclose()
