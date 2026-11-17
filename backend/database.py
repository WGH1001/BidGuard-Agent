# -*- coding: utf-8 -*-
"""B1 / E4 · SQLAlchemy 数据库基础设施：Engine / SessionLocal / Base / get_db 依赖 / 法规库种子幂等导入。

设计约束（对应任务卡 B1 & E4）：
1. DATABASE_URL 读取环境变量，缺省为 sqlite:///./data/bidguard.db；
2. SQLite 相对路径必须锚定项目根目录（Path(__file__).resolve().parents[1]），严禁跟随 CWD；
3. 模块导入阶段零写盘副作用，建表仅在 Base.metadata.create_all 或 init_db 被显式调用时发生；
4. E4 增量：init_db 时自动创建 regulation_laws / regulation_articles 新表，
   并在 regulation_laws 为空且 data/corpus/regulations/*.json 存在时幂等导入 4 部种子法律
   （按 content_hash 跳过重复，已有数据绝不覆盖）。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
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
    RegulationArticle,
    RegulationLaw,
    Tenant,
    User,
    UserModelConfig,
    apply_sqlite_pragmas,
)

__all__ = [
    'PROJECT_ROOT',
    'DEFAULT_SQLITE_REL_URL',
    'REGULATION_SEED_DIR',
    'resolve_database_url',
    'get_engine',
    'get_session_factory',
    'engine',
    'SessionLocal',
    'get_db',
    'init_db',
    'ensure_bootstrap_admin',
    'seed_demo_data',
    'seed_regulation_laws',
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
    'RegulationLaw',
    'RegulationArticle',
]

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
DEFAULT_SQLITE_REL_URL: str = 'sqlite:///./data/bidguard.db'
REGULATION_SEED_DIR: Path = PROJECT_ROOT / 'data' / 'corpus' / 'regulations'

DEFAULT_TENANT_NAME_FALLBACK: str = '默认机构'

SEED_TENANTS = [
    '政企系统集成投标部',
    '弱电智能化工程部',
    '智能装备销售中心',
]


def resolve_default_tenant_name() -> str:
    """读取生产基线默认部门名称（环境变量 DEFAULT_TENANT_NAME，缺省 '默认机构'）。"""
    return os.environ.get('DEFAULT_TENANT_NAME', '').strip() or DEFAULT_TENANT_NAME_FALLBACK


def resolve_initial_admin_password() -> str:
    """读取初始管理员密码（环境变量 ADMIN_INITIAL_PASSWORD，缺省 'BidGuard2026!'）。"""
    return os.environ.get('ADMIN_INITIAL_PASSWORD', '').strip() or 'BidGuard2026!'


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    """构建 SQLAlchemy Engine（支持 SQLite 与 PostgreSQL 双驱动）。"""
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
            # SSE 流 + 多任务并发流水线的实测教训（2026-11-09 全站 TimeoutError）：
            # 默认 pool_size=5+overflow=10 会被并发的流水线工作线程与前端多页签 SSE 打满
            pool_size=20,
            max_overflow=20,
        )

    return create_engine(resolved_url, echo=False)


def get_session_factory(bind_engine=None):
    """返回 sessionmaker 工厂（expire_on_commit=False 适配后台线程与 SSE）。"""
    target_engine = bind_engine if bind_engine is not None else get_engine()
    return sessionmaker(bind=target_engine, autocommit=False, autoflush=False, expire_on_commit=False)


engine = get_engine(ensure_dir=False)
SessionLocal = get_session_factory(engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 路由依赖注入生成器：每个请求独立 Session，退出自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _format_law_book_title(raw_name: str) -> str:
    """确保法律全称以《...》包裹。"""
    cleaned = (raw_name or '').strip()
    if not cleaned:
        return '《未命名法规》'
    if not cleaned.startswith('《'):
        cleaned = f"《{cleaned}"
    if not cleaned.endswith('》'):
        cleaned = f"{cleaned}》"
    return cleaned


def seed_regulation_laws(session: Session, seed_dir: Optional[Path] = None) -> dict[str, int]:
    """E4 法规库种子幂等导入：
    当 regulation_laws 表为空且 data/corpus/regulations/*.json 存在时自动导入；
    按法律名与 content_hash 判重，已有数据绝不覆盖。
    """
    target_dir = Path(seed_dir) if seed_dir is not None else REGULATION_SEED_DIR
    stats = {'laws_imported': 0, 'articles_imported': 0}

    if not target_dir.is_dir():
        return stats

    # 若表中已有法规数据且未指定自定义 seed_dir，直接跳过保持幂等
    existing_count = session.query(RegulationLaw).count()
    if existing_count > 0 and seed_dir is None:
        return stats

    json_files = sorted(target_dir.glob('*.json'))
    for jf in json_files:
        try:
            raw_data = json.loads(jf.read_text(encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(raw_data, dict):
            continue

        law_name = _format_law_book_title(str(raw_data.get('law') or jf.stem))
        raw_articles = raw_data.get('articles')
        if not isinstance(raw_articles, list) or not raw_articles:
            continue

        valid_articles: list[tuple[str, Optional[str], str, str]] = []
        seen_art_nos: set[str] = set()
        for item in raw_articles:
            if not isinstance(item, dict):
                continue
            art_no = str(item.get('article') or item.get('article_no') or '').strip()
            content = str(item.get('content') or '').strip()
            art_title = str(item.get('title') or item.get('article_title') or '').strip() or None
            if not art_no or not content or art_no in seen_art_nos:
                continue
            seen_art_nos.add(art_no)
            art_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
            valid_articles.append((art_no, art_title, content, art_hash))

        if not valid_articles:
            continue

        combined_str = '\n'.join(f"{a[0]}:{a[3]}" for a in valid_articles)
        law_hash = hashlib.sha256(combined_str.encode('utf-8')).hexdigest()

        # 按 name 或 content_hash 检查是否已存在（已有数据不覆盖）
        existing_law = (
            session.query(RegulationLaw)
            .filter((RegulationLaw.name == law_name) | (RegulationLaw.content_hash == law_hash))
            .first()
        )
        if existing_law is not None:
            continue

        now = _utc_now()
        law_obj = RegulationLaw(
            name=law_name,
            issuing_body=str(raw_data.get('issuing_body') or '全国人民代表大会常务委员会 / 国务院'),
            effective_date=str(raw_data.get('effective_date') or '') or None,
            source_url=str(raw_data.get('source_url') or f"seed://{jf.name}"),
            article_count=len(valid_articles),
            content_hash=law_hash,
            fetched_at=now,
            is_seed=True,
            created_at=now,
            updated_at=now,
        )
        session.add(law_obj)
        session.flush()

        for art_no, art_title, content, art_hash in valid_articles:
            session.add(
                RegulationArticle(
                    law_id=law_obj.id,
                    article_no=art_no,
                    article_title=art_title,
                    content=content,
                    content_hash=art_hash,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.flush()
        stats['laws_imported'] += 1
        stats['articles_imported'] += len(valid_articles)

    return stats


def ensure_bootstrap_admin(session: Session) -> dict[str, int]:
    """E35 底线初始化层（无条件幂等）：无论 SEED_DEMO_DATA 为何值，库无租户/管理员时创建中性默认部门 + admin + 中性空规则。"""
    from backend.auth import hash_password

    counts = {
        'tenants': 0,
        'users': 0,
        'rule_configs': 0,
    }

    tenant_count = session.query(Tenant).count()
    if tenant_count == 0:
        t_obj = Tenant(name=resolve_default_tenant_name(), is_active=True)
        session.add(t_obj)
        session.flush()
        counts['tenants'] += 1

    primary_tenant = (
        session.query(Tenant).filter_by(is_active=True).order_by(Tenant.id.asc()).first()
        or session.query(Tenant).order_by(Tenant.id.asc()).first()
    )

    admin_count = session.query(User).filter(User.role == 'admin').count()
    if admin_count == 0 and primary_tenant is not None:
        base_username = 'admin'
        if session.query(User).filter_by(username=base_username).first() is not None:
            base_username = 'sysadmin'
        admin_user = User(
            tenant_id=primary_tenant.id,
            username=base_username,
            password_hash=hash_password(resolve_initial_admin_password()),
            display_name='系统管理员',
            role='admin',
            is_active=True,
        )
        session.add(admin_user)
        session.flush()
        counts['users'] += 1

    if primary_tenant is not None and session.query(BidRuleConfig).filter_by(tenant_id=primary_tenant.id).count() == 0:
        neutral_rule = BidRuleConfig(
            tenant_id=primary_tenant.id,
            name='默认核查规则',
            price_cap=None,
            open_date=None,
            buyer_name='',
            required_social_months_json=[],
            custom_redlines_json=[],
            is_default=True,
        )
        session.add(neutral_rule)
        session.flush()
        counts['rule_configs'] += 1

    return counts


def seed_demo_data(session: Session) -> dict[str, int]:
    """E35 演示数据层：写入多演示部门、admin/liming 账号、演示核查规则与演示证照台账。"""
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
    init_pwd = resolve_initial_admin_password()

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
    """显式初始化数据库：创建全部 11 张表，幂等导入 4 部种子法规；seed=True 写入演示数据，否则仅保底创建中性部门与 admin。"""
    eng = get_engine(database_url, ensure_dir=True)
    Base.metadata.create_all(bind=eng)

    factory = get_session_factory(eng)
    session = factory()
    try:
        # E4：无论是否开启演示任务，只要法规表为空且本地存在 4 部种子 JSON，即幂等导入法规库底座
        seed_regulation_laws(session)
        if seed:
            seed_demo_data(session)
        ensure_bootstrap_admin(session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return eng