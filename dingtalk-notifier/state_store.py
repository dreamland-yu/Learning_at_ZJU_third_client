"""Private, durable state for the local reminder bridge."""
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from domain import Task, parse_time


class Store:
    def __init__(self, path):
        database = str(path)
        if database != ':memory:':
            target = Path(path).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            database = str(target)
        self._db = sqlite3.connect(database, timeout=30)
        self._db.row_factory = sqlite3.Row
        # DELETE journals inherit the private database's permissions; no persistent
        # WAL sidecars are needed for this small, single-process service.
        self._db.execute('PRAGMA journal_mode=DELETE')
        with self._db:
            self._db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS manual_tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    deadline TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    text TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    detail TEXT NOT NULL DEFAULT ''
                );
            ''')

    def get_setting(self, key, default=None):
        row = self._db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row is not None else default

    def set_setting(self, key, value):
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        with self._db:
            self._db.execute(
                'INSERT INTO settings(key, value) VALUES(?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (key, encoded),
            )

    @staticmethod
    def _manual_task(row):
        return Task(
            id=row['id'], course='手动待办', title=row['title'],
            deadline=parse_time(row['deadline']), source='manual', submitted=False,
        )

    def manual_tasks(self):
        rows = self._db.execute('SELECT id, title, deadline FROM manual_tasks ORDER BY rowid')
        return [self._manual_task(row) for row in rows]

    def add_manual(self, task_id, title, deadline):
        parsed = parse_time(deadline)
        stamp = parsed.isoformat() if parsed is not None else None
        with self._db:
            self._db.execute(
                'INSERT OR IGNORE INTO manual_tasks(id, title, deadline) VALUES(?, ?, ?)',
                (str(task_id), title, stamp),
            )
            row = self._db.execute(
                'SELECT id, title, deadline FROM manual_tasks WHERE id=?', (str(task_id),),
            ).fetchone()
        return self._manual_task(row)

    def pending_add(self, now):
        until = parse_time(self.get_setting('pending_add_until'))
        return until is not None and parse_time(now) < until

    def set_pending_add(self, until):
        parsed = parse_time(until)
        if parsed is None:
            raise ValueError('A pending input deadline is required.')
        self.set_setting('pending_add_until', parsed.isoformat())

    def clear_pending_add(self):
        with self._db:
            self._db.execute('DELETE FROM settings WHERE key=?', ('pending_add_until',))

    def seen_message(self, id):
        return self._db.execute('SELECT 1 FROM messages WHERE id=?', (str(id),)).fetchone() is not None

    def mark_message(self, id):
        with self._db:
            self._db.execute('INSERT OR IGNORE INTO messages(id) VALUES(?)', (str(id),))

    def enqueue(self, key, text, kind='chat', status='pending'):
        with self._db:
            cursor = self._db.execute(
                'INSERT OR IGNORE INTO outbox(key, text, kind, status, idempotency_key) '
                'VALUES(?, ?, ?, ?, ?)',
                (key, text, kind, status, str(uuid4())),
            )
        return cursor.rowcount == 1

    def has_notice(self, key):
        return self._db.execute('SELECT 1 FROM outbox WHERE key=?', (key,)).fetchone() is not None

    def outbox_pending(self, limit=20):
        if limit <= 0:
            return []
        rows = self._db.execute(
            'SELECT key, text, kind, idempotency_key FROM outbox '
            'WHERE status=? ORDER BY sequence LIMIT ?', ('pending', limit),
        )
        return [dict(row) for row in rows]

    def outbox_sendable(self, allowed_deadlines, limit=20, allowed_rollcalls=None):
        keys = list(allowed_deadlines)
        eligible = 'key IN (' + ','.join('?' for _ in keys) + ')' if keys else '0'
        rollcalls = list(allowed_rollcalls) if allowed_rollcalls is not None else []
        rollcall_filter = ''
        if allowed_rollcalls is not None:
            active = 'key IN (' + ','.join('?' for _ in rollcalls) + ')' if rollcalls else '0'
            rollcall_filter = f" AND (key NOT LIKE 'rollcall:%' OR {active})"
        rows = self._db.execute(
            "SELECT key, text, kind, idempotency_key FROM outbox WHERE status='pending' "
            f"AND (key NOT LIKE 'deadline:%' OR {eligible}){rollcall_filter} ORDER BY sequence LIMIT ?",
            [*keys, *rollcalls, limit],
        )
        return [dict(row) for row in rows]

    def retire_ended_rollcalls(self, active_keys):
        keys = list(active_keys)
        missing = 'key NOT IN (' + ','.join('?' for _ in keys) + ')' if keys else '1'
        with self._db:
            self._db.execute(
                "UPDATE outbox SET status='skipped', detail='Rollcall is no longer active.' "
                f"WHERE status='pending' AND key LIKE 'rollcall:%' AND {missing}", keys)

    def finish_notice(self, key, status, detail=''):
        with self._db:
            self._db.execute(
                'UPDATE outbox SET status=?, detail=? WHERE key=?', (status, detail, key),
            )

    def recover_inflight(self):
        with self._db:
            self._db.execute(
                "UPDATE outbox SET status='unknown', detail='Interrupted remote call; not resent.' "
                "WHERE status='sending'"
            )

    def outbox_stats(self):
        rows = self._db.execute('SELECT status, COUNT(*) AS count FROM outbox GROUP BY status')
        return {row['status']: row['count'] for row in rows}

    def _skip_pending(self, keys):
        """Retire superseded deadline reminders without changing sent outcomes."""
        with self._db:
            self._db.executemany(
                'UPDATE outbox SET status=?, detail=? WHERE key=? AND status=?',
                [('skipped', 'A more urgent deadline threshold is current.', key, 'pending')
                 for key in keys],
            )

    def close(self):
        self._db.close()
