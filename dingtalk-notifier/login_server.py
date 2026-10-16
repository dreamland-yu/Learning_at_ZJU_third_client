#!/usr/bin/env python3
"""Operator-only terminal login; no password in shell history, config, or stdout."""
import asyncio
import getpass
import logging
import sys

from lazy.core.login.login import ZjuAsyncClient
from lazy.server.credentials import EncryptedCredentialStore


async def login():
    if not sys.stdin.isatty():
        raise SystemExit('请在你自己的终端运行此命令，交互输入学号与密码。')
    studentid = input('请输入学号：').strip()
    password = getpass.getpass('请输入统一身份认证密码（输入时不显示）：')
    if not studentid or not password:
        raise SystemExit('学号或密码为空，未保存。')
    logging.disable(logging.CRITICAL)
    async with ZjuAsyncClient(trust_env=False) as client:
        if not await client.login(studentid, password):
            raise SystemExit('登录验证失败，未保存，请核对账号后重试。')
        store = EncryptedCredentialStore()
        store.save(studentid, password, dict(client.session.cookies))
    print('LAZY Server 登录验证成功，凭据已加密保存在本机。')


if __name__ == '__main__':
    asyncio.run(login())
