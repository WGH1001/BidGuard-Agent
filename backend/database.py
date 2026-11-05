# -*- coding: utf-8 -*-
"""B1 · SQLAlchemy 数据库基础设施：Engine / SessionLocal / Base / get_db 依赖。

设计约束（对应任务卡 B1 §2 & §3）：
1. DATABASE_URL 读取环境变量，缺省为 sqlite:///./data/bidguard.db；
2. SQLite 相对路径必须锚定项目根目录（Path(__file__).resolve().parents[1]），严禁跟随 CWD；
3. 模块导入阶段零写盘副作用，建表仅在 Base.metadata.create_all 或 init_db 被显式调用时发生；
4. SQLite 连接默认启用 check_same_thread=False 与 PRAGMA foreign_keys=ON（保证级联删除生效）。
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Generator, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.models import (
    AuditLog,
    Base,
    BidCheckTask,
    BidFinding,
    BidRuleConfig,
    CompanyCertificate,
    QaSession,
    Tenant,
    User,
    UserModelConfig,
    apply_sqlite_pragmas,
)

__all__ = [
    'PROJECT_ROOT',
    'DEFAULT_SQLITE_REL_URL',
    'resolve_database_url',
    'get_engine',
    'get_session_factory',
    'engine',
    'SessionLocal',
    'get_db',
    'init_db',
    'seed_demo_data',
    'Base',
    'Tenant',
    'User',
    'BidCheckTask',
    'BidFinding',
    'BidRuleConfig',
    'CompanyCertificate',
    'QaSession',
    'AuditLog',
    'UserModelConfig',
]

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
DEFAULT_SQLITE_REL_URL: str = 'sqlite:///./data/bidguard.db'

SEED_TENANTS = [
    '政企系统集成投标部',
    '弱电智能化工程部',
    '智能装备销售中心',
]


def resolve_database_url(database_url: Optional[str] = None) -> str:
    """解析数据库连接 URL，将相对路径的 SQLite URL 锚定至 PROJECT_ROOT（禁止跟随 CWD）。"""
    raw_url = (
        database_url.strip()
        if (isinstance(database_url, str) and database_url.strip())
        else os.environ.get('DATABASE_URL', '').strip()
    )
    if not raw_url:
        raw_url = DEFAULT_SQLITE_REL_URL

    if not raw_url.startswith('sqlite:///'):
        return raw_url

    if ':memory:' in raw_url or 'mode=memory' in raw_url:
        return raw_url

    path_part, sep, query_part = raw_url[len('sqlite:///'):].partition('?')
    if not path_part:
        return raw_url

    # 判断是否为相对路径（如 ./data/bidguard.db 或 data/bidguard.db）
    candidate_path = Path(path_part)
    is_windows_abs = len(path_part) >= 2 and path_part[1] == ':'
    is_posix_abs = path_part.startswith('/')

    if not is_windows_abs and not is_posix_abs and not candidate_path.is_absolute():
        clean_rel = path_part[2:] if path_part.startswith('./') or path_part.startswith('.\\') else path_part
        anchored = (PROJECT_ROOT / clean_rel).resolve()
    else:
        anchored = candidate_path.resolve()

    resolved = f"sqlite:///{anchored.as_posix()}"
    return f"{resolved}?{query_part}" if sep else resolved


def get_engine(database_url: Optional[str] = None, ensure_dir: bool = False):
    """构建 SQLAlchemy Engine（支持 SQLite 与 PostgreSQL 双驱动）。

    注意：默认 ensure_dir=False 以保证 import 阶段零磁盘副作用；
    当显式调用 init_db() 时传入 ensure_dir=True 创建 data/ 目录。
    """
    resolved_url = resolve_database_url(database_url)

    if resolved_url.startswith('sqlite'):
        if ensure_dir and resolved_url.startswith('sqlite:///') and ':memory:' not in resolved_url:
            file_part = resolved_url[len('sqlite:///'):].split('?')[0]
            if file_part:
                Path(file_part).parent.mkdir(parents=True, exist_ok=True)
        eng = create_engine(
            resolved_url,
            connect_args={'check_same_thread': False},
            echo=False,
        )
        apply_sqlite_pragmas(eng)
        return eng

    if resolved_url.startswith('postgresql'):
        return create_engine(
            resolved_url,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=3600,
        )

    return create_engine(resolved_url, echo=False)


def get_session_factory(bind_engine=None):
    """返回 sessionmaker 工厂（expire_on_commit=False 适配后台线程与 SSE）。"""
    target_engine = bind_engine if bind_engine is not None else get_engine()
    return sessionmaker(bind=target_engine, autocommit=False, autoflush=False, expire_on_commit=False)


# 默认模块级 engine 与 SessionLocal（惰性连接，导入时不触发写盘）
engine = get_engine(ensure_dir=False)
SessionLocal = get_session_factory(engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 路由依赖注入生成器：每个请求独立 Session，退出自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def seed_demo_data(session: Session) -> dict[str, int]:
    """幂等写入基础租户、默认管理员与投标专员账号、默认核查规则与证照台账。"""
    from backend.auth import hash_password

    counts = {
        'tenants': 0,
        'users': 0,
        'rule_configs': 0,
        'certificates': 0,
    }

    tenant_map: dict[str, Tenant] = {}
    for t_name in SEED_TENANTS:
        t_obj = session.query(Tenant).filter_by(name=t_name).first()
        if t_obj is None:
            t_obj = Tenant(name=t_name, is_active=True)
            session.add(t_obj)
            session.flush()
            counts['tenants'] += 1
        tenant_map[t_name] = t_obj

    primary_tenant = tenant_map[SEED_TENANTS[0]]
    init_pwd = os.environ.get('ADMIN_INITIAL_PASSWORD', '').strip() or 'BidGuard2026!'

    seed_users_spec = [
        {'username': 'admin', 'display_name': '系统管理员', 'role': 'admin', 'tenant': primary_tenant},
        {'username': 'liming', 'display_name': '李明（投标商务经理）', 'role': 'member', 'tenant': primary_tenant},
    ]
    for u_spec in seed_users_spec:
        u_obj = session.query(User).filter_by(
            tenant_id=u_spec['tenant'].id,
            username=u_spec['username'],
        ).first()
        if u_obj is None:
            u_obj = User(
                tenant_id=u_spec['tenant'].id,
                username=u_spec['username'],
                password_hash=hash_password(init_pwd),
                display_name=u_spec['display_name'],
                role=u_spec['role'],
                is_active=True,
            )
            session.add(u_obj)
            session.flush()
            counts['users'] += 1

    for t_name, t_obj in tenant_map.items():
        rc_obj = session.query(BidRuleConfig).filter_by(tenant_id=t_obj.id, is_default=True).first()
        if rc_obj is None:
            rc_obj = BidRuleConfig(
                tenant_id=t_obj.id,
                name=f"{t_name}默认核查规则",
                price_cap=Decimal('1450000.00'),
                open_date=date(2026, 11, 5),
                buyer_name='某省政务云管理中心',
                required_social_months_json=['2026-08', '2026-09', '2026-10'],
                custom_redlines_json=[],
                is_default=True,
            )
            session.add(rc_obj)
            session.flush()
            counts['rule_configs'] += 1

    raw_seed_demo = os.environ.get('SEED_DEMO_DATA', '1').strip().lower()
    if raw_seed_demo in ('0', 'false', 'no', 'off'):
        return counts

    existing_cert = session.query(CompanyCertificate).filter_by(tenant_id=primary_tenant.id).first()
    if existing_cert is None:
        certs = [
            CompanyCertificate(
                tenant_id=primary_tenant.id,
                cert_name='ISO/IEC 27001 信息安全管理体系认证证书',
                cert_no='ISO27001-CN-2024089',
                valid_until=date(2027, 10, 28),
                warn_days=30,
                remark='政企安全项目必附资质',
            ),
            CompanyCertificate(
                tenant_id=primary_tenant.id,
                cert_name='银河麒麟高级服务器操作系统 V10 兼容性互认证书',
                cert_no='KYLIN-CERT-2026031',
                valid_until=date(2028, 6, 30),
                warn_days=30,
                remark='信创 ★ 条款高频佐证材料',
            ),
        ]
        session.add_all(certs)
        session.flush()
        counts['certificates'] += len(certs)

    return counts


def init_db(database_url: Optional[str] = None, seed: bool = False):
    """显式初始化数据库：创建全部 9 张表，并在 seed=True 时幂等写入种子数据。"""
    eng = get_engine(database_url, ensure_dir=True)
    Base.metadata.create_all(bind=eng)

    if seed:
        factory = get_session_factory(eng)
        session = factory()
        try:
            seed_demo_data(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return eng