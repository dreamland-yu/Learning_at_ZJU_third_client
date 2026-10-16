"""Bounded official CLI calls; never interpolate incoming text into a shell."""
from __future__ import annotations

import asyncio
import json
import os
import uuid


class DwsError(RuntimeError):
    def __init__(self, category='unknown', definitive=False):
        super().__init__(category)
        self.category = category
        self.definitive = definitive


def chunks(text, size=2500):
    while len(text) > size:
        at = text.rfind('\n', 0, size)
        at = at if at > size // 2 else size
        yield text[:at]
        text = text[at:].lstrip('\n')
    if text:
        yield text


class Dws:
    def __init__(self, config):
        self.config = config

    def command(self, args):
        return [self.config['dws_path'], '--profile', self.config['profile'], *args]

    def environment(self):
        env = os.environ.copy()
        env['DWS_CONFIG_DIR'] = self.config['dws_config_dir']
        return env

    async def run(self, args, timeout=30):
        process = await asyncio.create_subprocess_exec(
            *self.command(args), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self.environment())
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            process.terminate()
            await process.communicate()
            raise DwsError('timeout') from None
        try:
            data = json.loads(stdout)
        except (ValueError, UnicodeDecodeError):
            raise DwsError('invalid_response') from None
        error = data.get('error')
        if process.returncode or error:
            category = error.get('category', 'unknown') if isinstance(error, dict) else 'unknown'
            raise DwsError(category, definitive=category in {
                'validation', 'permission', 'authentication', 'authorization', 'business'})
        return data

    async def send(self, notice):
        peer = self.config['recipient_open_dingtalk_id']
        if notice['kind'] == 'ding' and self.config.get('ding_enabled', True):
            try:
                data = await self.run(['ding', '+send-personal', '--type', 'app', '--users', peer,
                    '--content', notice['text'], '--uuid', notice['idempotency_key'], '--yes',
                    '--format', 'json', '--timeout', '20'])
                body = data.get('data', {})
                if data.get('ok') is True and body.get('success') is True and body.get('result', {}).get('openDingId'):
                    return 'accepted'
                if body.get('success') is not False or data.get('ok') is not True:
                    return 'unknown'
                # Explicit business rejection: the DING was not accepted.
            except DwsError as exc:
                if not exc.definitive:
                    return 'unknown'
        try:
            parts = list(chunks(notice['text']))
            for index, part in enumerate(parts):
                key = str(uuid.uuid5(uuid.UUID(notice['idempotency_key']), str(index)))
                data = await self.run(['chat', '+messages-send', '--as', 'user',
                    '--open-dingtalk-id', peer, '--text', part, '--ai-tag=false',
                    '--idempotency-key', key, '--yes', '--format', 'json', '--timeout', '20'])
                receipt = data.get('result', {})
                if not (data.get('ok') is True and receipt.get('success') is True
                        and receipt.get('result', {}).get('openTaskId')):
                    return 'unknown'
            return 'accepted'
        except DwsError as exc:
            return 'failed' if exc.definitive else 'unknown'

    async def history(self, start):
        return await self.run(['chat', '+chat-messages', '--open-dingtalk-id',
            self.config['recipient_open_dingtalk_id'], '--start', start,
            '--order', 'asc', '--page-all', '--page-limit', '10', '--max-items', '500',
            '--no-reactions', '--format', 'json', '--timeout', '20'], timeout=45)
