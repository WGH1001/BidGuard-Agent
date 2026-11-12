# -*- coding: utf-8 -*-
"""B2 / D1 · 第 10 组路由：/api/model-config（user_model_configs 读写与两段式测速连通性测试，API Key 永不回显明文）。"""
from __future__ import annotations

import ipaddress
import time
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit
from backend.database import get_db
from backend.models import UserModelConfig, ensure_user_model_configs_columns
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
        supports_vision=bool(getattr(row, 'supports_vision', False)),
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


def _is_models_endpoint_unsupported(exc: Exception) -> bool:
    """判断 /models 异常是否属于上游未实现该端点（HTTP 404 / 405），仅此类情况回退最小 chat 验证。"""
    status_code = getattr(exc, 'status_code', None)
    if status_code is None and getattr(exc, 'response', None) is not None:
        status_code = getattr(exc.response, 'status_code', None)
    if status_code in (404, 405):
        return True
    msg_lower = str(exc).lower()
    if '404' in msg_lower or 'not found' in msg_lower or '405' in msg_lower:
        return True
    return False


def _extract_model_ids(models_page) -> list[str]:
    """从 OpenAI SDK SyncPage[Model] 或 JSON 字典/列表中提取模型 ID 列表。"""
    if models_page is None:
        return []
    raw_list = getattr(models_page, 'data', None)
    if raw_list is None:
        if isinstance(models_page, dict):
            raw_list = models_page.get('data', [])
        elif isinstance(models_page, (list, tuple)):
            raw_list = models_page
        else:
            try:
                raw_list = list(models_page)
            except Exception:
                raw_list = []
    model_ids: list[str] = []
    for item in (raw_list or []):
        if isinstance(item, str) and item.strip():
            model_ids.append(item.strip())
        elif isinstance(item, dict) and item.get('id'):
            model_ids.append(str(item['id']).strip())
        elif getattr(item, 'id', None):
            model_ids.append(str(item.id).strip())
    return model_ids


def _fetch_available_models(client) -> Optional[list[str]]:
    """第一段探活：调用 GET /models 获取可用模型 ID 列表；若上游不支持（404/405）返回 None 触发第二段 chat 回退。"""
    raw_sdk = getattr(client, 'client', None)
    if getattr(client, 'use_openai', False):
        models_resource = getattr(raw_sdk, 'models', None)
        list_fn = getattr(models_resource, 'list', None)
        if not callable(list_fn):
            return None
        try:
            try:
                page = list_fn(timeout=10.0)
            except TypeError:
                page = list_fn()
            return _extract_model_ids(page)
        except Exception as exc:
            if _is_models_endpoint_unsupported(exc):
                return None
            raise
    else:
        get_fn = getattr(raw_sdk, 'get', None)
        if not callable(get_fn):
            return None
        headers = {
            'Authorization': f'Bearer {getattr(client, "api_key", "")}',
            'Content-Type': 'application/json',
        }
        try:
            resp = get_fn(
                f"{str(getattr(client, 'base_url', '') or '').rstrip('/')}/models",
                headers=headers,
                timeout=10.0,
            )
            if getattr(resp, 'status_code', None) in (404, 405):
                return None
            resp.raise_for_status()
            return _extract_model_ids(resp.json())
        except Exception as exc:
            if _is_models_endpoint_unsupported(exc):
                return None
            raise


def _scrub_secret(msg: str, secret_key: str) -> str:
    """从异常消息中抹除可能回显的 API Key 明文。"""
    if not msg:
        return '未知错误'
    if secret_key and secret_key in msg:
        return msg.replace(secret_key, '****')
    return msg


@router.get('', response_model=ModelConfigOut)
@router.get('/my', response_model=ModelConfigOut)
def get_my_model_config(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ModelConfigOut:
    """获取当前登录用户的模型配置（仅回显 configured: true 与尾四位，绝不返回明文 Key）。"""
    ensure_user_model_configs_columns(db.get_bind())
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
    ensure_user_model_configs_columns(db.get_bind())
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
            supports_vision=bool(body.supports_vision),
        )
        db.add(row)
    else:
        row.base_url = base_url
        row.api_key_encrypted = cipher
        row.model_flash = flash
        row.model_flagship = flagship
        row.supports_reasoning_effort = bool(body.supports_reasoning_effort)
        row.supports_vision = bool(body.supports_vision)

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
    """两段式验证模型配置连通性并统计毫秒延迟（优先 GET /models 验鉴权与模型名；若 404/405 则回退最小 chat 请求）。"""
    ensure_user_model_configs_columns(db.get_bind())
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

    target_models = [flash]
    if flagship and flagship != flash:
        target_models.append(flagship)

    t0 = time.perf_counter()
    try:
        client = build_client_from_user_config(
            base_url=base_url,
            api_key=raw_key,
            model_flash=flash,
            model_flagship=flagship,
            supports_reasoning_effort=bool(body.supports_reasoning_effort),
            supports_vision=bool(body.supports_vision),
        )
        available_models = _fetch_available_models(client)
        if available_models is not None:
            latency_ms = int(round((time.perf_counter() - t0) * 1000))
            available_count = len(available_models)
            if available_count > 0:
                missing_models = [m for m in target_models if m not in available_models]
                if not missing_models:
                    model_matched = True
                    models_label = ' / '.join(target_models)
                    message = (
                        f"鉴权通过（{latency_ms}ms，发现 {available_count} 个可用模型，"
                        f"配置的 {models_label} ✓ 在列表中）"
                    )
                else:
                    model_matched = False
                    missing_label = ' / '.join(missing_models)
                    message = (
                        f"连通但模型名可能写错（{latency_ms}ms，发现 {available_count} 个可用模型，"
                        f"未在列表中找到 {missing_label}）"
                    )
            else:
                model_matched = True
                message = f"鉴权通过（{latency_ms}ms，服务端未返回模型列表，配置的模型：{flash}）"
        else:
            client.chat(
                [{'role': 'user', 'content': '请仅回复OK'}],
                model_tier='flash',
                max_tokens=16,
            )
            latency_ms = int(round((time.perf_counter() - t0) * 1000))
            model_matched = True
            message = f"鉴权通过（{latency_ms}ms，模型：{flash}）"
    except Exception as exc:
        safe_err = _scrub_secret(str(exc), raw_key)
        record_audit(
            db,
            action='model_config_test',
            tenant_id=current_user.tenant_id,
            user_id=uid,
            target=f"user:{current_user.username}",
            detail_json={'base_url': base_url, 'model_flash': flash, 'ok': False},
        )
        raise HTTPException(
            status_code=502,
            detail={'code': 'MODEL_TEST_FAILED', 'message': f"模型服务连接测试失败：{safe_err}"},
        ) from exc

    record_audit(
        db,
        action='model_config_test',
        tenant_id=current_user.tenant_id,
        user_id=uid,
        target=f"user:{current_user.username}",
        detail_json={
            'base_url': base_url,
            'model_flash': flash,
            'ok': model_matched,
            'latency_ms': latency_ms,
        },
    )
    return ModelConfigTestOut(
        ok=model_matched,
        latency_ms=latency_ms,
        model=flash,
        message=message,
    )
