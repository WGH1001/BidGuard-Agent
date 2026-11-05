# -*- coding: utf-8 -*-
"""B2 · 第 10 组路由：/api/model-config（user_model_configs 读写与连通性测试，API Key 永不回显明文）。"""
from __future__ import annotations

import ipaddress
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit
from backend.database import get_db
from backend.models import UserModelConfig
from backend.schemas import (
    ModelConfigOut,
    ModelConfigTestOut,
    ModelConfigUpsertRequest,
    OkResponse,
)
from backend.utils.crypto import decrypt_api_key, encrypt_api_key, mask_api_key_last4
from core.llm_gateway import build_client_from_user_config

router = APIRouter(prefix='/api/model-config', tags=['model-config'])


def _validate_base_url(raw_url: str) -> str:
    cleaned = (raw_url or '').strip()
    if not cleaned:
        raise HTTPException(
            status_code=422,
            detail={'code': 'INVALID_BASE_URL', 'message': 'base_url 不能为空'},
        )
    parsed = urlparse(cleaned)
    if (parsed.scheme or '').lower() not in ('http', 'https') or not parsed.hostname:
        raise HTTPException(
            status_code=422,
            detail={'code': 'INVALID_BASE_URL', 'message': 'base_url 必须为合法的 http/https 地址'},
        )
    host = parsed.hostname.lower()
    if host in ('localhost', '0.0.0.0', '::1') or host.startswith('127.') or host.startswith('169.254.'):
        raise HTTPException(
            status_code=422,
            detail={'code': 'FORBIDDEN_LOOPBACK_URL', 'message': '禁止将模型接口指向本机回环或链路本地地址'},
        )
    try:
        ip_obj = ipaddress.ip_address(host)
        if ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_unspecified:
            raise HTTPException(
                status_code=422,
                detail={'code': 'FORBIDDEN_LOOPBACK_URL', 'message': '禁止指向回环或未指定 IP 地址'},
            )
    except ValueError:
        pass
    return cleaned.rstrip('/')


def _row_to_out(row: Optional[UserModelConfig]) -> ModelConfigOut:
    if row is None:
        return ModelConfigOut(configured=False)
    try:
        plain_key = decrypt_api_key(row.api_key_encrypted)
    except Exception:
        return ModelConfigOut(configured=False)

    clean_k = plain_key.strip()
    last4 = clean_k[-4:] if len(clean_k) >= 4 else clean_k
    return ModelConfigOut(
        configured=True,
        base_url=row.base_url or '',
        api_key_masked=mask_api_key_last4(clean_k),
        api_key_last4=last4,
        model_flash=row.model_flash or '',
        model_flagship=row.model_flagship or row.model_flash or '',
        supports_reasoning_effort=bool(row.supports_reasoning_effort),
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


@router.get('', response_model=ModelConfigOut)
@router.get('/my', response_model=ModelConfigOut)
def get_my_model_config(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ModelConfigOut:
    """获取当前登录用户的模型配置（仅回显 configured: true 与尾四位，绝不返回明文 Key）。"""
    uid = current_user.get('id')
    if uid is None:
        return ModelConfigOut(configured=False)
    row = db.query(UserModelConfig).filter_by(user_id=int(uid)).first()
    return _row_to_out(row)


@router.put('', response_model=ModelConfigOut)
@router.put('/my', response_model=ModelConfigOut)
@router.post('', response_model=ModelConfigOut, include_in_schema=False)
def save_my_model_config(
    body: ModelConfigUpsertRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ModelConfigOut:
    """保存或更新当前用户的模型配置（经 backend/utils/crypto.py 加密落库）。"""
    uid = int(current_user.id)
    base_url = _validate_base_url(body.base_url)
    flash = body.model_flash.strip()
    flagship = (body.model_flagship or '').strip() or flash
    raw_key = (body.api_key or '').strip()

    row = db.query(UserModelConfig).filter_by(user_id=uid).first()
    if raw_key:
        cipher = encrypt_api_key(raw_key)
    else:
        if row is None or not row.api_key_encrypted:
            raise HTTPException(
                status_code=422,
                detail={'code': 'API_KEY_REQUIRED', 'message': '首次配置必须提供有效的 api_key'},
            )
        cipher = row.api_key_encrypted

    if row is None:
        row = UserModelConfig(
            user_id=uid,
            base_url=base_url,
            api_key_encrypted=cipher,
            model_flash=flash,
            model_flagship=flagship,
            supports_reasoning_effort=bool(body.supports_reasoning_effort),
        )
        db.add(row)
    else:
        row.base_url = base_url
        row.api_key_encrypted = cipher
        row.model_flash = flash
        row.model_flagship = flagship
        row.supports_reasoning_effort = bool(body.supports_reasoning_effort)

    db.commit()
    db.refresh(row)

    record_audit(
        db,
        action='model_config_save',
        tenant_id=current_user.tenant_id,
        user_id=uid,
        target=f"user:{current_user.username}",
        detail_json={'base_url': base_url, 'model_flash': flash},
    )
    return _row_to_out(row)


@router.delete('', response_model=OkResponse)
@router.delete('/my', response_model=OkResponse)
def delete_my_model_config(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OkResponse:
    """清除当前用户的自定义模型配置。"""
    uid = int(current_user.id)
    row = db.query(UserModelConfig).filter_by(user_id=uid).first()
    if row is not None:
        db.delete(row)
        db.commit()
    record_audit(
        db,
        action='model_config_delete',
        tenant_id=current_user.tenant_id,
        user_id=uid,
        target=f"user:{current_user.username}",
    )
    return OkResponse(ok=True, message='个人模型配置已清除')


@router.post('/test', response_model=ModelConfigTestOut)
def test_model_config_connection(
    body: ModelConfigUpsertRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ModelConfigTestOut:
    """使用 build_client_from_user_config 构造客户端并验证配置合法性。"""
    uid = int(current_user.id)
    base_url = _validate_base_url(body.base_url)
    flash = body.model_flash.strip()
    flagship = (body.model_flagship or '').strip() or flash
    raw_key = (body.api_key or '').strip()

    if not raw_key:
        row = db.query(UserModelConfig).filter_by(user_id=uid).first()
        if row and row.api_key_encrypted:
            try:
                raw_key = decrypt_api_key(row.api_key_encrypted)
            except Exception:
                raw_key = ''
    if not raw_key:
        raise HTTPException(
            status_code=422,
            detail={'code': 'API_KEY_REQUIRED', 'message': '请提供有效的 API Key'},
        )

    client = build_client_from_user_config(
        base_url=base_url,
        api_key=raw_key,
        model_flash=flash,
        model_flagship=flagship,
        supports_reasoning_effort=bool(body.supports_reasoning_effort),
    )
    return ModelConfigTestOut(
        ok=(client is not None),
        model=flash,
        message=f"模型客户端（{flash}）配置校验通过",
    )