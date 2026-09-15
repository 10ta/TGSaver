#!/usr/bin/env python3
"""生成 SECRET_KEY。把输出整行粘进 .env 的 SECRET_KEY= 后面。"""
from cryptography.fernet import Fernet

print(Fernet.generate_key().decode())
