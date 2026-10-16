#!/usr/bin/env python3
"""Install/control this user's macOS LaunchAgent; no credentials in the plist."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import time

ROOT = Path(__file__).resolve().parent
LABEL = 'dev.dreamland.lazy-dingtalk'
PLIST = Path.home() / 'Library' / 'LaunchAgents' / (LABEL + '.plist')
TARGET = f'gui/{os.getuid()}'


def loaded():
    return subprocess.run(['launchctl', 'print', TARGET + '/' + LABEL],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def wait_stopped(pid):
    # bootout can return before the old process has finished cancelling its
    # event subscription and releasing the single-instance lock.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        alive = False
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                pass
        if not loaded() and not alive:
            return
        time.sleep(0.1)
    raise RuntimeError('The previous service is still stopping; retry start after it exits.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'start', 'stop', 'restart', 'uninstall', 'status'])
    args = parser.parse_args()
    if args.action == 'status':
        print(json.dumps({'installed': PLIST.exists(), 'loaded': loaded(), 'label': LABEL}))
        return
    if args.action in ('stop', 'restart', 'uninstall', 'install') and loaded():
        status = ROOT / '.runtime/status.json'
        old_pid = json.loads(status.read_text()).get('pid') if status.exists() else None
        subprocess.run(['launchctl', 'bootout', TARGET + '/' + LABEL], check=True)
        wait_stopped(old_pid)
    if args.action == 'uninstall':
        PLIST.unlink(missing_ok=True)
    if args.action == 'install':
        runtime = ROOT / '.runtime'
        runtime.mkdir(exist_ok=True)
        expected = [str(ROOT.parent / '.venv/bin/python'), str(ROOT / 'bridge.py'), '--run']
        if PLIST.exists():
            old = plistlib.loads(PLIST.read_bytes())
            if old.get('ProgramArguments') != expected:
                raise RuntimeError('Existing LaunchAgent belongs to another installation.')
        config = {
            'Label': LABEL, 'ProgramArguments': expected, 'WorkingDirectory': str(ROOT.parent),
            'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 15,
            'EnvironmentVariables': {'PYTHONUNBUFFERED': '1'},
            'StandardOutPath': str(runtime / 'bridge.log'),
            'StandardErrorPath': str(runtime / 'bridge.log'),
        }
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_bytes(plistlib.dumps(config))
        PLIST.chmod(0o600)
    if args.action in ('install', 'start', 'restart') and not loaded():
        if not PLIST.exists():
            parser.error('Run install first.')
        subprocess.run(['launchctl', 'bootstrap', TARGET, str(PLIST)], check=True)
    print(json.dumps({'action': args.action, 'loaded': loaded()}))


if __name__ == '__main__':
    main()
