# -*- coding: utf-8 -*-
"""B2 · 第 1 组路由：/api/auth（login / me / change_password / health / 管理员用户与部门管理）。"""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.auth import (
    AuthUser,
    create_token,
    get_current_user,
    hash_password,
    record_audit,
    require_admin,
    resolve_show_demo_login,
    verify_password,
)
from backend.database import get_db
from backend.models import Tenant, User
from backend.schemas import (
    AdminUserCreateRequest,
    AdminUserOut,
    AdminUserResetPasswordRequest,
    AdminUserUpdateRequest,
    ChangePasswordRequest,
    HealthOut,
    LoginRequest,
    LoginResponse,
    OkResponse,
    TenantCreateRequest,
    TenantOut,
    TenantUpdateRequest,
    UserProfileOut,
)

router = APIRouter(prefix='/api/auth', tags=['auth'])

_VALID_USER_ROLES = frozenset({'admin', 'member'})
_USERNAME_RE = re.compile(r'^[A-Za-z0-9_.\-]{1,64}$')


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _validate_user_role(role: str) -> str:
    """严格校验角色白名单（不做 strip，含空格或未知角色一律 422 INVALID_ROLE）。"""
    if role not in _VALID_USER_ROLES:
        raise HTTPException(
            status_code=422,
            detail={'code': 'INVALID_ROLE', 'message': "角色仅允许 'admin' 或 'member'"},
        )
    return role


def _validate_password(raw_password: str) -> str:
    """校验密码强度（去除首尾空白后至少 6 位）。"""
    pw = raw_password.strip()
    if len(pw) < 6:
        raise HTTPException(
            status_code=422,
            detail={'code': 'INVALID_PASSWORD', 'message': '密码长度不能少于 6 位'},
        )
    return pw


def _validate_username(raw_username: str) -> str:
    """校验用户名格式（允许 1~64 位字母、数字、下划线、点或短横线）。"""
    cleaned = raw_username.strip()
    if not _USERNAME_RE.match(cleaned):
        raise HTTPException(
            status_code=422,
            detail={
                'code': 'INVALID_USERNAME',
                'message': '用户名仅允许 1~64 位字母、数字、下划线、点或短横线',
            },
        )
    return cleaned


def _tenant_to_out(tenant: Tenant) -> TenantOut:
    return TenantOut(
        id=tenant.id,
        name=tenant.name,
        is_active=bool(tenant.is_active),
        created_at=tenant.created_at.isoformat() if tenant.created_at else None,
    )


def _user_to_admin_out(user: User, tenant_names: Dict[int, str]) -> AdminUserOut:
    return AdminUserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name or user.username,
        role=user.role,
        tenant_id=user.tenant_id,
        tenant_name=tenant_names.get(user.tenant_id, ''),
        is_active=bool(user.is_active),
        last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
        created_at=user.created_at.isoformat() if user.created_at else None,
    )


@router.get('/health', response_model=HealthOut)
def auth_health() -> HealthOut:
    """公开健康探针（无需登录，含演示入口显隐开关）。"""
    return HealthOut(
        status='ok',
        service='bidguard-agent',
        version='1.0.0',
        show_demo_login=resolve_show_demo_login(),
    )


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

    tenant = db.query(Tenant).filter_by(id=user.tenant_id).first()
    if tenant is not None and not tenant.is_active:
        raise HTTPException(
            status_code=401,
            detail={'code': 'TENANT_DISABLED', 'message': '所属部门租户已被禁用'},
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
            tenant_name=tenant.name if tenant is not None else '',
            is_active=user.is_active,
        ),
    )


@router.get('/me', response_model=UserProfileOut)
def get_me(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserProfileOut:
    """获取当前登录用户身份信息。"""
    tenant_id = current_user.get('tenant_id')
    tenant_name = current_user.get('tenant_name')
    if not tenant_name and tenant_id is not None:
        tenant_obj = db.query(Tenant).filter_by(id=int(tenant_id)).first()
        if tenant_obj is not None:
            tenant_name = tenant_obj.name
    return UserProfileOut(
        id=current_user.get('id'),
        username=str(current_user.get('username') or current_user.get('sub') or ''),
        display_name=str(current_user.get('display_name') or current_user.get('sub') or ''),
        role=str(current_user.get('role') or 'member'),
        tenant_id=tenant_id,
        tenant_name=str(tenant_name or ''),
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


@router.get('/tenants', response_model=List[TenantOut])
def list_tenants(
    admin: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> List[TenantOut]:
    """[Admin] 列出所有业务部门租户。"""
    _ = admin
    tenants = db.query(Tenant).order_by(Tenant.id.asc()).all()
    return [_tenant_to_out(t) for t in tenants]


@router.post('/tenants', response_model=TenantOut)
def create_tenant(
    body: TenantCreateRequest,
    admin: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> TenantOut:
    """[Admin] 新建业务部门租户。"""
    name = body.name.strip()
    if not name:
        raise HTTPException(
            status_code=422,
            detail={'code': 'INVALID_TENANT_NAME', 'message': '部门名称不能为空'},
        )
    existing = db.query(Tenant).filter_by(name=name).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={'code': 'TENANT_EXISTS', 'message': f'部门租户「{name}」已存在'},
        )

    tenant = Tenant(name=name, is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)

    record_audit(
        db,
        action='admin_create_tenant',
        tenant_id=admin.get('tenant_id'),
        user_id=admin.get('id'),
        target=f"tenant:{tenant.id}",
        detail_json={'tenant_id': tenant.id, 'name': tenant.name},
    )
    return _tenant_to_out(tenant)


@router.patch('/tenants/{tenant_id}', response_model=TenantOut)
def update_tenant(
    tenant_id: int,
    body: TenantUpdateRequest,
    admin: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> TenantOut:
    """[Admin] 重命名业务部门租户（全局唯一性排除自身 + 审计留痕）。"""
    tenant = db.query(Tenant).filter_by(id=tenant_id).first()
    if tenant is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'TENANT_NOT_FOUND', 'message': '目标部门不存在'},
        )

    new_name = body.name.strip()
    if not new_name:
        raise HTTPException(
            status_code=422,
            detail={'code': 'INVALID_TENANT_NAME', 'message': '部门名称不能为空'},
        )
    existing = db.query(Tenant).filter(Tenant.name == new_name, Tenant.id != tenant_id).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={'code': 'TENANT_EXISTS', 'message': f'部门租户「{new_name}」已存在'},
        )

    old_name = tenant.name
    tenant.name = new_name
    db.commit()
    db.refresh(tenant)

    record_audit(
        db,
        action='rename_tenant',
        tenant_id=admin.get('tenant_id'),
        user_id=admin.get('id'),
        target=f"tenant:{tenant.id}",
        detail_json={'tenant_id': tenant.id, 'old_name': old_name, 'new_name': tenant.name},
    )
    return _tenant_to_out(tenant)


@router.get('/users', response_model=List[AdminUserOut])
def list_users(
    tenant_id: Optional[int] = None,
    admin: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> List[AdminUserOut]:
    """[Admin] 列出全部平台账号（含所属部门名称与启停状态）。"""
    _ = admin
    tenant_names = {t.id: t.name for t in db.query(Tenant).all()}
    query = db.query(User)
    if tenant_id is not None:
        query = query.filter(User.tenant_id == tenant_id)
    users = query.order_by(User.id.asc()).all()
    return [_user_to_admin_out(u, tenant_names) for u in users]


@router.post('/users', response_model=AdminUserOut)
def create_user(
    body: AdminUserCreateRequest,
    admin: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AdminUserOut:
    """[Admin] 创建新用户账号并归属指定部门与角色。"""
    role = _validate_user_role(body.role)
    username = _validate_username(body.username)
    password = _validate_password(body.password)

    tenant = db.query(Tenant).filter_by(id=body.tenant_id).first()
    if tenant is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'TENANT_NOT_FOUND', 'message': '目标所属部门不存在'},
        )
    if not tenant.is_active:
        raise HTTPException(
            status_code=409,
            detail={'code': 'TENANT_DISABLED', 'message': '目标所属部门已禁用'},
        )

    existing = db.query(User).filter_by(username=username).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={'code': 'USERNAME_EXISTS', 'message': f'用户名「{username}」已存在'},
        )

    user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=body.display_name.strip() or username,
        role=role,
        tenant_id=tenant.id,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    record_audit(
        db,
        action='admin_create_user',
        tenant_id=admin.get('tenant_id'),
        user_id=admin.get('id'),
        target=f"user:{user.username}",
        detail_json={
            'user_id': user.id,
            'username': user.username,
            'role': user.role,
            'tenant_id': user.tenant_id,
        },
    )
    return _user_to_admin_out(user, {tenant.id: tenant.name})


@router.patch('/users/{user_id}', response_model=AdminUserOut)
def update_user(
    user_id: int,
    body: AdminUserUpdateRequest,
    admin: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AdminUserOut:
    """[Admin] 修改用户姓名、部门、角色或启停状态（含最后管理员与禁改自身护栏）。"""
    target_user = db.query(User).filter_by(id=user_id).first()
    if target_user is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'USER_NOT_FOUND', 'message': '目标用户不存在'},
        )

    new_role = target_user.role if body.role is None else _validate_user_role(body.role)
    new_active = target_user.is_active if body.is_active is None else bool(body.is_active)

    new_tenant_id = target_user.tenant_id
    if body.tenant_id is not None:
        tenant = db.query(Tenant).filter_by(id=body.tenant_id).first()
        if tenant is None:
            raise HTTPException(
                status_code=404,
                detail={'code': 'TENANT_NOT_FOUND', 'message': '目标所属部门不存在'},
            )
        new_tenant_id = tenant.id

    # 先校验 LAST_ADMIN_PROTECTED，再校验 CANNOT_MODIFY_SELF，保证双护栏正交且均可触发
    was_active_admin = target_user.role == 'admin' and bool(target_user.is_active)
    will_lose_active_admin = (not new_active) or (new_role != 'admin')
    if was_active_admin and will_lose_active_admin:
        active_admin_count = (
            db.query(User)
            .filter(User.role == 'admin', User.is_active.is_(True))
            .count()
        )
        if active_admin_count <= 1:
            raise HTTPException(
                status_code=409,
                detail={
                    'code': 'LAST_ADMIN_PROTECTED',
                    'message': '系统至少需保留 1 名活跃管理员，无法禁用或降级最后一名管理员',
                },
            )

    admin_id = admin.get('id')
    if admin_id is not None and target_user.id == admin_id and (
        not new_active or new_role != target_user.role
    ):
        raise HTTPException(
            status_code=409,
            detail={
                'code': 'CANNOT_MODIFY_SELF',
                'message': '不能禁用自己或降级/更改自己的管理员角色',
            },
        )

    new_username = target_user.username
    if body.username is not None:
        new_username = _validate_username(body.username)
        if new_username != target_user.username:
            dup_user = (
                db.query(User)
                .filter(User.username == new_username, User.id != target_user.id)
                .first()
            )
            if dup_user is not None:
                raise HTTPException(
                    status_code=409,
                    detail={'code': 'USERNAME_EXISTS', 'message': f'用户名「{new_username}」已存在'},
                )

    old_username = target_user.username
    if new_username != old_username:
        target_user.username = new_username
    if body.display_name is not None:
        cleaned_name = body.display_name.strip()
        target_user.display_name = cleaned_name or target_user.username
    target_user.role = new_role
    target_user.tenant_id = new_tenant_id
    target_user.is_active = new_active

    db.commit()
    db.refresh(target_user)

    tenant_names = {t.id: t.name for t in db.query(Tenant).all()}
    audit_detail: dict = {
        'user_id': target_user.id,
        'username': target_user.username,
        'role': target_user.role,
        'tenant_id': target_user.tenant_id,
        'is_active': bool(target_user.is_active),
    }
    if new_username != old_username:
        audit_detail['old_username'] = old_username
        audit_detail['new_username'] = target_user.username
    record_audit(
        db,
        action='admin_update_user',
        tenant_id=admin.get('tenant_id'),
        user_id=admin.get('id'),
        target=f"user:{target_user.username}",
        detail_json=audit_detail,
    )
    return _user_to_admin_out(target_user, tenant_names)


@router.post('/users/{user_id}/reset-password', response_model=OkResponse)
def reset_user_password(
    user_id: int,
    body: AdminUserResetPasswordRequest,
    admin: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> OkResponse:
    """[Admin] 管理员为指定账号重置登录密码（审计绝不记录明文或哈希）。"""
    target_user = db.query(User).filter_by(id=user_id).first()
    if target_user is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'USER_NOT_FOUND', 'message': '目标用户不存在'},
        )
    new_pw = _validate_password(body.new_password)
    target_user.password_hash = hash_password(new_pw)
    db.commit()

    record_audit(
        db,
        action='admin_reset_password',
        tenant_id=admin.get('tenant_id'),
        user_id=admin.get('id'),
        target=f"user:{target_user.username}",
        detail_json={'user_id': target_user.id, 'username': target_user.username},
    )
    return OkResponse(ok=True, message=f'已重置账号「{target_user.username}」的登录密码')
