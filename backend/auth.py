# -*- coding: utf-8 -*-
"""backend 鉴权层：密钥守卫（前段，逻辑零改动）+ B1 完整鉴权（bcrypt / HMAC 令牌 / 依赖注入 / 审计）。

前段 get_auth_secret 密钥守卫摘自 Contract-Review-Agent backend/auth.py（T23/T42/T53，逻辑零改动，
仅开发默认密钥字面量改为本项目名）。B1 按任务卡向下追加完整鉴权层：
bcrypt 密码哈希、HMAC-SHA256 令牌签发校验（payload 权威键 sub/tenant_id/role/exp）、
get_current_user / require_admin 依赖、record_audit 审计留痕。
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Optional
import warnings

import bcrypt
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_DEV_DEFAULT_SECRET = 'dev-secret-bidguard-agent'
_SECRET = os.environ.get('AUTH_SECRET', _DEV_DEFAULT_SECRET)


def _check_startup_secret_warning() -> None:
    """模块加载时检查 AUTH_SECRET 环境变量，缺失时打印强安全警告。"""
    env_sec = os.environ.get('AUTH_SECRET', '').strip()
    if not env_sec:
        msg = (
            "[SECURITY WARNING] 未配置环境变量 AUTH_SECRET，当前使用开发默认密钥！"
            "生产环境部署请务必配置独立的 AUTH_SECRET（开发默认密钥仅允许在 DEBUG=1 时启用）。"
        )
        logger.warning(msg)
        warnings.warn(msg, UserWarning, stacklevel=2)


_check_startup_secret_warning()


def get_auth_secret() -> str:
    """获取当前生效的 HMAC 签名密钥。

    规则：
        1. 若环境变量 AUTH_SECRET 非空，直接采用；
        2. 若模块级 _SECRET 被外部显式覆盖为非默认值，采用 _SECRET；
        3. 若未设置 AUTH_SECRET：检查环境变量 DEBUG（缺省 '1' 兼容本地与单测），
           若 DEBUG 显式设为 '0'/'false'/'no'/'off' 则抛出 RuntimeError 拒绝启动/签发；
           否则返回开发默认密钥 _DEV_DEFAULT_SECRET。
    """
    env_sec = os.environ.get('AUTH_SECRET', '').strip()
    if env_sec:
        # T53 强化：显式把 AUTH_SECRET 设为开发默认值同样视为未配置——
        # 生产态（DEBUG=0）下阻断，防止"照抄文档默认值"的伪安全配置。
        if env_sec == _DEV_DEFAULT_SECRET:
            debug_flag0 = os.environ.get('DEBUG', '1').strip().lower()
            if debug_flag0 in ('0', 'false', 'no', 'off'):
                raise RuntimeError(
                    "AUTH_SECRET 被显式设置为开发默认值，且 DEBUG 已关闭——"
                    "生产环境严禁使用开发默认密钥！"
                )
        return env_sec
    if _SECRET and _SECRET != _DEV_DEFAULT_SECRET:
        return _SECRET
    debug_flag = os.environ.get('DEBUG', '1').strip().lower()
    if debug_flag in ('0', 'false', 'no', 'off'):
        raise RuntimeError(
            "未配置环境变量 AUTH_SECRET，且 DEBUG 已关闭（非 DEBUG=1 模式），"
            "生产环境严禁使用开发默认密钥！"
        )
    return _DEV_DEFAULT_SECRET


def resolve_show_demo_login() -> bool:
    """E32：判断登录页是否允许显示预置演示账号凭证卡片（SHOW_DEMO_LOGIN 与 SEED_DEMO_DATA 须同时开启）。"""
    raw_seed = os.environ.get('SEED_DEMO_DATA', '1').strip().lower()
    raw_demo = os.environ.get('SHOW_DEMO_LOGIN', '1').strip().lower()
    off_vals = ('0', 'false', 'no', 'off')
    return (raw_seed not in off_vals) and (raw_demo not in off_vals)


# ============================================================================
# B1 向下追加：完整鉴权层（bcrypt 密码哈希 / HMAC-SHA256 令牌 / FastAPI 依赖 / 审计）
# ============================================================================

_TOKEN_TTL_SECONDS = 12 * 3600  # 默认令牌有效期 12 小时


class AuthUser(dict):
    """兼容字典下标（user['uid'] / user['tenant']）与对象属性（user.id / user.tenant_id）的当前用户视图。"""

    def __getattr__(self, item: str) -> Any:
        if item == 'id' and 'uid' in self:
            return self['uid']
        if item in self:
            return self[item]
        raise AttributeError(f"'AuthUser' object has no attribute {item!r}")


def hash_password(plain_password: str) -> str:
    """使用 bcrypt 对明文密码进行加盐哈希，返回 UTF-8 字符串（$2b$...）。"""
    if not isinstance(plain_password, str) or not plain_password.strip():
        raise ValueError("密码不能为空白字符串")
    hashed = bcrypt.hashpw(plain_password.encode('utf-8'), bcrypt.gensalt())
    return hashed.decode('utf-8')


def verify_password(plain_password: Optional[str], password_hash: Optional[str]) -> bool:
    """使用 bcrypt.checkpw 校验明文密码与数据库 password_hash 是否匹配。"""
    if not plain_password or not isinstance(plain_password, str):
        return False
    if not password_hash or not isinstance(password_hash, str):
        return False
    try:
        return bool(
            bcrypt.checkpw(
                plain_password.encode('utf-8'),
                password_hash.encode('utf-8'),
            )
        )
    except Exception:
        return False


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _b64url_decode(data: str) -> bytes:
    padding = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign(payload_b64: str, secret: Optional[str] = None) -> str:
    key = (secret if secret is not None else get_auth_secret()).encode('utf-8')
    return hmac.new(key, payload_b64.encode('utf-8'), hashlib.sha256).hexdigest()


def create_token(
    uid_or_user: Any = None,
    tenant: Optional[int] = None,
    role: str = 'member',
    *,
    uid: Optional[int] = None,
    tenant_id: Optional[int] = None,
    username: Optional[str] = None,
    expires_hours: Optional[float] = None,
    expires_seconds: Optional[int] = None,
    secret: Optional[str] = None,
) -> str:
    """签发 HMAC-SHA256 身份令牌：base64url(payload_json) + '.' + hmac_hex。

    契约锁死（INTERFACE.md §3.2 修订版）：payload 权威键仅为
    sub（用户名，全局唯一）/ tenant_id / role / exp 四键，禁止任何别名键。
    支持位置传参 create_token(uid, tenant, role)、传入 User ORM 实例 create_token(user_obj)、
    或关键字传参 create_token(uid=1, tenant=1, role='admin', username='admin')。
    """
    resolved_uid = uid
    resolved_tenant = tenant if tenant is not None else tenant_id
    resolved_role = role
    resolved_sub = username

    if uid_or_user is not None:
        if hasattr(uid_or_user, 'id') and hasattr(uid_or_user, 'tenant_id'):
            resolved_uid = int(uid_or_user.id)
            resolved_tenant = int(uid_or_user.tenant_id)
            resolved_role = str(getattr(uid_or_user, 'role', 'member'))
            resolved_sub = str(getattr(uid_or_user, 'username', resolved_uid))
        elif isinstance(uid_or_user, int):
            resolved_uid = uid_or_user
        elif isinstance(uid_or_user, str):
            resolved_sub = uid_or_user

    if resolved_sub is None:
        resolved_sub = str(resolved_uid if resolved_uid is not None else '')

    if expires_seconds is not None:
        ttl = int(expires_seconds)
    elif expires_hours is not None:
        ttl = int(expires_hours * 3600)
    else:
        ttl = _TOKEN_TTL_SECONDS

    exp_ts = int(time.time()) + ttl
    payload = {
        'sub': str(resolved_sub),
        'tenant_id': int(resolved_tenant) if resolved_tenant is not None else None,
        'role': str(resolved_role),
        'exp': exp_ts,
    }
    payload_b64 = _b64url_encode(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    sig = _sign(payload_b64, secret=secret)
    return f"{payload_b64}.{sig}"


def _inspect_token(token: str, secret: Optional[str] = None) -> tuple[Optional[dict], Optional[str]]:
    """内部令牌核验，返回 (payload, error_code)。
    error_code 取值：None（通过）| 'MALFORMED' | 'BAD_SIGNATURE' | 'EXPIRED'
    """
    if not token or not isinstance(token, str) or token.count('.') != 1:
        return None, 'MALFORMED'
    payload_b64, sig = token.split('.', 1)
    if not payload_b64 or not sig:
        return None, 'MALFORMED'

    expected_sig = _sign(payload_b64, secret=secret)
    if not hmac.compare_digest(expected_sig, sig):
        return None, 'BAD_SIGNATURE'

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode('utf-8'))
    except Exception:
        return None, 'MALFORMED'

    if not isinstance(payload, dict) or 'exp' not in payload or 'role' not in payload:
        return None, 'MALFORMED'

    if int(payload.get('exp', 0)) < int(time.time()):
        return None, 'EXPIRED'

    return payload, None


def verify_token(token: str, secret: Optional[str] = None) -> Optional[dict]:
    """校验 HMAC 令牌：签名恒时比对 + 过期检查。

    有效时返回 payload dict（含 uid/tenant/role/exp 等）；
    被篡改、签名错误、已过期或格式非法时一律返回 None。
    """
    payload, err = _inspect_token(token, secret=secret)
    if err is not None:
        return None
    return payload


def _extract_bearer_token(authorization: Optional[str]) -> str:
    if not authorization or not isinstance(authorization, str):
        raise HTTPException(
            status_code=401,
            detail={'code': 'MISSING_TOKEN', 'message': '缺少身份凭据（Authorization: Bearer <token>）'},
        )
    if not authorization.lower().startswith('bearer '):
        raise HTTPException(
            status_code=401,
            detail={'code': 'INVALID_AUTH_HEADER', 'message': '认证头格式非法，须为 Bearer <token>'},
        )
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail={'code': 'MISSING_TOKEN', 'message': 'Bearer 令牌为空'},
        )
    return token


def _get_db_dep():
    from backend.database import get_db
    yield from get_db()


def get_current_user(
    authorization: Optional[str] = Header(None),
    db: Optional[Session] = Depends(_get_db_dep),
) -> AuthUser:
    """FastAPI 强鉴权依赖：校验 Bearer 令牌并核查数据库用户激活状态。

    按任务卡 B1 要求，对以下情形分别抛出 401 HTTPException：
    1. 令牌缺失或格式非法 -> 401 (MISSING_TOKEN / MALFORMED_TOKEN)
    2. 签名不符或载荷被篡改 -> 401 (INVALID_SIGNATURE)
    3. 令牌已过期 -> 401 (TOKEN_EXPIRED)
    4. 数据库中对应用户已禁用 (is_active=False) 或所属租户已禁用 -> 401 (USER_DISABLED)
    """
    token = _extract_bearer_token(authorization)
    payload, err = _inspect_token(token)

    if err == 'BAD_SIGNATURE':
        raise HTTPException(
            status_code=401,
            detail={'code': 'INVALID_SIGNATURE', 'message': '令牌签名不符或载荷已被篡改'},
        )
    if err == 'EXPIRED':
        raise HTTPException(
            status_code=401,
            detail={'code': 'TOKEN_EXPIRED', 'message': '登录令牌已过期，请重新登录'},
        )
    if err is not None or payload is None:
        raise HTTPException(
            status_code=401,
            detail={'code': 'MALFORMED_TOKEN', 'message': '无效的身份令牌'},
        )

    # 契约锁死：payload 权威键仅 sub / tenant_id / role / exp（username 全局唯一，按 sub 定位）
    sub = payload.get('sub')
    tenant_id = payload.get('tenant_id')

    if db is not None and isinstance(db, Session):
        from backend.models import Tenant, User

        user_obj: Optional[User] = None
        if sub:
            user_obj = db.query(User).filter_by(username=str(sub)).first()

        if user_obj is not None:
            if not user_obj.is_active:
                raise HTTPException(
                    status_code=401,
                    detail={'code': 'USER_DISABLED', 'message': '该用户账号已被禁用'},
                )
            tenant_obj = db.query(Tenant).filter_by(id=user_obj.tenant_id).first()
            if tenant_obj is not None and not tenant_obj.is_active:
                raise HTTPException(
                    status_code=401,
                    detail={'code': 'TENANT_DISABLED', 'message': '所属部门租户已被禁用'},
                )
            return AuthUser({
                'id': user_obj.id,
                'uid': user_obj.id,
                'user_id': user_obj.id,
                'username': user_obj.username,
                'sub': user_obj.username,
                'display_name': user_obj.display_name,
                'role': user_obj.role,
                'tenant': user_obj.tenant_id,
                'tenant_id': user_obj.tenant_id,
                'tenant_name': tenant_obj.name if tenant_obj is not None else '',
                'is_active': user_obj.is_active,
                'exp': payload.get('exp'),
            })

    return AuthUser({
        'id': None,
        'uid': None,
        'user_id': None,
        'username': sub or '',
        'sub': sub or '',
        'display_name': sub or '',
        'role': payload.get('role', 'member'),
        'tenant': tenant_id,
        'tenant_id': tenant_id,
        'tenant_name': '',
        'is_active': True,
        'exp': payload.get('exp'),
    })


def require_admin(current_user: AuthUser = Depends(get_current_user)) -> AuthUser:
    """管理员权限守卫：未登录抛 401，非 admin 角色抛 403。"""
    if current_user is None:
        raise HTTPException(
            status_code=401,
            detail={'code': 'UNAUTHORIZED', 'message': '需要登录后访问'},
        )
    if current_user.get('role') != 'admin':
        raise HTTPException(
            status_code=403,
            detail={'code': 'FORBIDDEN', 'message': '该操作仅管理员可执行'},
        )
    return current_user


def record_audit(
    db_or_factory: Any,
    *,
    action: str,
    tenant_id: Optional[int] = None,
    user_id: Optional[int] = None,
    target: Optional[str] = None,
    detail_json: Optional[dict] = None,
    ip: Optional[str] = None,
) -> Optional[int]:
    """写入一条安全审计日志（INTERFACE.md §2 表 8 audit_logs）。

    支持传入现有 SQLAlchemy Session 实例或 sessionmaker 工厂；
    异常静默回滚，确保审计留痕绝不阻断主业务流程。
    """
    from backend.models import AuditLog

    own_session = False
    session: Optional[Session] = None
    try:
        if isinstance(db_or_factory, Session):
            session = db_or_factory
        elif callable(db_or_factory):
            session = db_or_factory()
            own_session = True
        else:
            from backend.database import SessionLocal
            session = SessionLocal()
            own_session = True

        entry = AuditLog(
            tenant_id=int(tenant_id) if tenant_id is not None else None,
            user_id=int(user_id) if user_id is not None else None,
            action=str(action),
            target=str(target) if target is not None else None,
            detail_json=detail_json,
            ip=str(ip) if ip is not None else None,
        )
        session.add(entry)
        session.commit()
        return entry.id
    except Exception:
        if session is not None:
            try:
                session.rollback()
            except Exception:
                pass
        return None
    finally:
        if own_session and session is not None:
            try:
                session.close()
            except Exception:
                pass