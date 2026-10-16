#!/usr/bin/env python3
"""Run, inspect, or smoke-test the local LAZY/DingTalk bridge."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
import fcntl
import json
import logging
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parent


def smoke():
    from commands import Commands
    from domain import MINIMUM_INTERVALS, TZ, Task
    from logic import Reminders
    from state_store import Store

    class SmokeBackend:
        # Offline fixture confined to the explicitly labelled smoke path.
        async def get_tasks(self):
            return []

        async def get_minimums(self):
            return dict(MINIMUM_INTERVALS)

    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / 'smoke.sqlite3')
        now = datetime.now(TZ)
        asyncio.run(Commands(store, SmokeBackend()).handle('/help', 'smoke-command', now))
        Reminders(store).queue_deadlines([
            Task('smoke-fixture', '手动待办', 'SMOKE ONLY', now + timedelta(hours=2),
                 source='manual', submitted=False)], now)
        assert len(store.outbox_pending()) == 2
        data = {'mode': 'offline_smoke', 'sent': False, 'queued': 2,
                'persistence_and_commands': True, 'real_network_test': False}
        store.close()
    target = ROOT / '.runtime' / 'spine-smoke.json'
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(data) + '\n')
    print(json.dumps(data))


async def check(config):
    from backend import Backend
    from runtime import LocalServer
    backend = Backend(config.get('server_url', 'http://127.0.0.1:8765'))
    server = LocalServer()
    try:
        await server.start()
        await backend.authenticate()
        minimums = await backend.get_minimums()
        tasks = await backend.get_tasks()
        rollcalls = await backend.get_rollcalls()
        probe = await backend._get('/api/assignments/submission/' + tasks[0].id) if tasks else {}
        print(json.dumps({'server': 'ready', 'todo_count': len(tasks), 'rollcall_count': len(rollcalls),
                          'minimums': minimums, 'submission_probe': {
                              'status': probe.get('status'), 'submitted': probe.get('submitted'),
                              'type': probe.get('type'), 'last_error': probe.get('last_error')}},
                         ensure_ascii=False))
    finally:
        await backend.close()
        await server.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='LAZY Server → DingTalk private-chat reminders')
    parser.add_argument('--smoke', action='store_true', help='Offline smoke only; no remote messages')
    parser.add_argument('--check', action='store_true', help='Read-only local Server integration check')
    parser.add_argument('--status', action='store_true', help='Show private runtime health summary')
    parser.add_argument('--run', action='store_true', help='Run the authorized bridge continuously')
    parser.add_argument('--config', default=str(ROOT / 'bridge.private.json'))
    args = parser.parse_args()
    if args.smoke:
        smoke()
        return
    if args.status:
        target = ROOT / '.runtime' / 'status.json'
        data = json.loads(target.read_text()) if target.exists() else {'running': False}
        if 'updated_at' in data:
            from domain import TZ, parse_time
            age = (datetime.now(TZ) - parse_time(data['updated_at'])).total_seconds()
            data['status_age_seconds'] = round(age, 1)
            data['running'] = bool(data.get('running') and age < 45)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    config = json.loads(Path(args.config).read_text())
    if args.check:
        asyncio.run(check(config))
        return
    if not args.run:
        parser.error('Choose --smoke, --check, --status, or --run.')
    runtime = ROOT / '.runtime'
    runtime.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    with (runtime / 'bridge.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('The bridge is already running.')
        from runtime import Bridge
        asyncio.run(Bridge(config).run())


if __name__ == '__main__':
    main()
