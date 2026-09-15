#!/usr/bin/env python3
"""交互式登录，生成并加密保存 session。

在服务器上跑一次即可，之后永久无人值守：

    python login.py

会依次问手机号、验证码、两步验证密码。
验证码请从【其它设备】的 Telegram 里读，不要在本机的 Telegram 里复制粘贴。

安全提示：session 等同于你账号的完整读写权限。它会用 .env 里的
SECRET_KEY 加密后存进数据库，密钥本身不入库。请确保：
    chmod 600 .env tgsaver.db
"""
from __future__ import annotations

import asyncio
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

import crypto
import db
from config import CFG


async def main() -> None:
    await db.init()

    row = await db.get_user(CFG.owner_id)
    if row and row["session_status"] == "ok":
        ans = input("已存在有效登录凭据，覆盖重新登录？[y/N] ").strip().lower()
        if ans != "y":
            print("已取消。")
            return

    print("\n即将登录 Telegram。验证码请在其它设备上查看。\n")
    client = TelegramClient(StringSession(), CFG.api_id, CFG.api_hash)
    await client.start()

    me = await client.get_me()
    session_str = client.session.save()
    await client.disconnect()

    await db.upsert_user(
        CFG.owner_id,
        username=me.username,
        session_enc=crypto.encrypt(session_str),
        session_status="ok",
        status="active",
        role="owner",
    )

    print(f"\n登录成功：{me.first_name or ''} "
          f"(@{me.username or '无用户名'}, id={me.id})")

    if me.id != CFG.owner_id:
        print(f"\n⚠️  注意：登录的账号 id 是 {me.id}，"
              f"但 .env 里的 OWNER_ID 写的是 {CFG.owner_id}。")
        print("   如果这两个不是同一个账号，bot 会拒绝你的请求。请核对。")

    print("\n凭据已加密写入数据库。现在可以启动服务了：")
    print("    sudo systemctl start tgsaver")
    await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(1)
