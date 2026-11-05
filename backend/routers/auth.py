# -*- coding: utf-8 -*-
"""B2 · 第 1 组路由：/api/auth（login / me / change_password / health）。"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.auth import (
    AuthUser,
    create_token,
    get_current_user,
    hash_password,
    record_audit,
    verify_password,
)
from backend.database import get_db
from backend.models import User
from backend.schemas import (
    ChangePasswordRequest,
    HealthOut,
    LoginRequest,
    LoginResponse,
    OkResponse,
    UserProfileOut,
)

router = APIRouter(prefix='/api/auth', tags=['auth'])


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.get('/health', response_model=HealthOut)
def auth_health() -> HealthOut:
    """公开健康探针（无需登录）。"""
    return HealthOut(status='ok', service='bidguard-agent', version='1.0.0')


@router.post('/login', response_model=LoginResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    """用户登录（username 全局唯一，校验 bcrypt 密码并签发 HMAC 令牌）。"""
    username = body.username.strip()
    user = db.query(User).filter_by(username=username).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=401,
            detail={'code': 'INVALID_CREDENTIALS', 'message': '用户名或密码错误'},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=401,
            detail={'code': 'USER_DISABLED', 'message': '该账号已被禁用'},
        )

    user.last_login_at = _utc_now()
    db.commit()
    db.refresh(user)

    token = create_token(
        username=user.username,
        tenant_id=user.tenant_id,
        role=user.role,
    )
    record_audit(
        db,
        action='login',
        tenant_id=user.tenant_id,
        user_id=user.id,
        target=f"user:{user.username}",
        detail_json={'username': user.username, 'role': user.role},
    )
    return LoginResponse(
        token=token,
        user=UserProfileOut(
            id=user.id,
            username=user.username,
            display_name=user.display_name or user.username,
            role=user.role,
            tenant_id=user.tenant_id,
            is_active=user.is_active,
        ),
    )


@router.get('/me', response_model=UserProfileOut)
def get_me(current_user: AuthUser = Depends(get_current_user)) -> UserProfileOut:
    """获取当前登录用户身份信息。"""
    return UserProfileOut(
        id=current_user.get('id'),
        username=str(current_user.get('username') or current_user.get('sub') or ''),
        display_name=str(current_user.get('display_name') or current_user.get('sub') or ''),
        role=str(current_user.get('role') or 'member'),
        tenant_id=current_user.get('tenant_id'),
        is_active=bool(current_user.get('is_active', True)),
    )


@router.post('/change_password', response_model=OkResponse)
@router.post('/password', response_model=OkResponse, include_in_schema=False)
def change_password(
    body: ChangePasswordRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OkResponse:
    """修改当前登录用户密码。"""
    username = str(current_user.get('username') or current_user.get('sub') or '')
    user = db.query(User).filter_by(username=username).first()
    if user is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'USER_NOT_FOUND', 'message': '当前登录用户不存在'},
        )
    if user.password_hash:
        if not body.old_password or not verify_password(body.old_password, user.password_hash):
            raise HTTPException(
                status_code=401,
                detail={'code': 'WRONG_OLD_PASSWORD', 'message': '原密码校验失败'},
            )
    user.password_hash = hash_password(body.new_password.strip())
    db.commit()

    record_audit(
        db,
        action='change_password',
        tenant_id=user.tenant_id,
        user_id=user.id,
        target=f"user:{user.username}",
    )
    return OkResponse(ok=True, message='密码修改成功')