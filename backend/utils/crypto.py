# -*- coding: utf-8 -*-
"""T60 · 用户级 API Key 对称加解密与脱敏工具（标准库实现，零新增第三方依赖）。

安全级别说明（必读）：
    本模块采用从 AUTH_SECRET 经 HMAC-SHA256 派生的主密钥 + 16 字节随机 Nonce +
    HMAC-SHA256 计数器流（CTR-like keystream）XOR 对称加密方案。
    该实现属于【应用层混淆级加密（Obfuscation-grade Symmetric Encryption）】，
    旨在严格满足「数据库不落 API Key 明文、零新增第三方依赖」红线；
    生产环境须确保环境变量 AUTH_SECRET 独立强随机且妥善保管。
"""
import base64
import hashlib
import hmac
import os
import struct
from typing import Optional

from backend.auth import get_auth_secret

_CIPHER_PREFIX = 'v1:'
_NONCE_BYTES = 16
_KEY_DERIVE_CONTEXT = b'contract-review-agent:t60:user-model-key:v1'


def _derive_master_key(secret: Optional[str] = None) -> bytes:
    """从当前生效的 AUTH_SECRET 派生 32 字节对称主密钥。"""
    raw_secret = (secret if secret is not None else get_auth_secret()).encode('utf-8')
    return hmac.new(raw_secret, _KEY_DERIVE_CONTEXT, hashlib.sha256).digest()


def _keystream_xor(data: bytes, master_key: bytes, nonce: bytes) -> bytes:
    """使用 HMAC-SHA256(master_key, nonce || counter_uint32_be) 计数器流与 data 逐字节 XOR。"""
    out = bytearray(len(data))
    offset = 0
    counter = 0
    while offset < len(data):
        block = hmac.new(
            master_key,
            nonce + struct.pack('>I', counter),
            hashlib.sha256,
        ).digest()
        chunk = data[offset:offset + 32]
        for i, b in enumerate(chunk):
            out[offset + i] = b ^ block[i]
        offset += len(chunk)
        counter += 1
    return bytes(out)


def encrypt_api_key(plain_key: str, secret: Optional[str] = None) -> str:
    """加密明文 API Key，返回带 'v1:' 前缀的 URL-safe Base64 密文串。"""
    if not isinstance(plain_key, str) or not plain_key.strip():
        raise ValueError("API Key 不能为空")
    raw_bytes = plain_key.strip().encode('utf-8')
    master_key = _derive_master_key(secret)
    nonce = os.urandom(_NONCE_BYTES)
    cipher_bytes = _keystream_xor(raw_bytes, master_key, nonce)
    token = base64.urlsafe_b64encode(nonce + cipher_bytes).rstrip(b'=').decode('ascii')
    return f"{_CIPHER_PREFIX}{token}"


def decrypt_api_key(cipher_text: str, secret: Optional[str] = None) -> str:
    """解密 'v1:' 密文串还原明文 API Key；格式非法或解密异常抛 ValueError。"""
    if not isinstance(cipher_text, str) or not cipher_text.startswith(_CIPHER_PREFIX):
        raise ValueError("密文格式非法")
    b64_part = cipher_text[len(_CIPHER_PREFIX):]
    padding = '=' * (-len(b64_part) % 4)
    try:
        raw = base64.urlsafe_b64decode(b64_part + padding)
    except Exception as exc:
        raise ValueError("密文 Base64 解码失败") from exc
    if len(raw) <= _NONCE_BYTES:
        raise ValueError("密文长度不足")
    nonce = raw[:_NONCE_BYTES]
    cipher_bytes = raw[_NONCE_BYTES:]
    master_key = _derive_master_key(secret)
    plain_bytes = _keystream_xor(cipher_bytes, master_key, nonce)
    try:
        return plain_bytes.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError("密文解密失败（密钥不匹配或数据损坏）") from exc


def mask_api_key_last4(plain_key: Optional[str]) -> str:
    """脱敏回显函数：仅保留末尾 4 位，前置 '****'（长度不足 4 位时全保留或加掩码）。"""
    if not plain_key or not isinstance(plain_key, str):
        return ''
    cleaned = plain_key.strip()
    if not cleaned:
        return ''
    if len(cleaned) <= 4:
        return f"****{cleaned}"
    return f"****{cleaned[-4:]}"