# -*- coding: utf-8 -*-
"""B1 / E4 / E11 · BidGuard-Agent 数据库 11 张核心 ORM 表定义（SQLAlchemy 2.0）。

契约依据：INTERFACE.md §2（既有 9 表一字不动 + E4 新增 regulation_laws / regulation_articles 两表
+ E11 bid_check_tasks 新增 started_at / finished_at / duration_json 耗时可观测性三列）。
双驱动兼容：PostgreSQL 16（生产主库）/ SQLite（开发与单测回退，开启 WAL 与 foreign_keys=ON）。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, relationship


LONG_TERM_SENTINEL_DATE = date(9999, 12, 31)


def _utc_now() -> datetime:
    """返回当前 UTC 时间（naive UTC datetime，兼容 SQLite 与 PostgreSQL）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 ORM 声明式基类。"""
    pass


# ── 1. tenants（多租户投标部门表）────────────────────────────────────────
class Tenant(Base):
    __tablename__ = 'tenants'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, unique=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=_utc_now)
    updated_at = Column(DateTime, nullable=False, default=_utc_now, onupdate=_utc_now)

    users = relationship('User', back_populates='tenant', cascade='all, delete-orphan')
    tasks = relationship('BidCheckTask', back_populates='tenant', cascade='all, delete-orphan')
    rule_configs = relationship('BidRuleConfig', back_populates='tenant', cascade='all, delete-orphan')
    certificates = relationship('CompanyCertificate', back_populates='tenant', cascade='all, delete-orphan')


# ── 2. users（用户与角色鉴权表）──────────────────────────────────────────
class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False, index=True)
    # 全局唯一（INTERFACE.md §2 修订版）：登录入参不带租户标识，租户内唯一无法唯一定位用户
    username = Column(String(64), nullable=False, unique=True, index=True)
    password_hash = Column(String(128), nullable=False)
    display_name = Column(String(128), nullable=False, default='')
    role = Column(String(16), nullable=False, default='member')  # 'admin' | 'member'
    is_active = Column(Boolean, nullable=False, default=True)
    last_login_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utc_now)
    updated_at = Column(DateTime, nullable=False, default=_utc_now, onupdate=_utc_now)

    tenant = relationship('Tenant', back_populates='users')
    tasks = relationship('BidCheckTask', back_populates='user')
    model_config_rel = relationship(
        'UserModelConfig',
        back_populates='user',
        uselist=False,
        cascade='all, delete-orphan',
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<User(id={self.id}, username={self.username!r}, role={self.role!r}, tenant_id={self.tenant_id})>"


# ── 3. bid_check_tasks（封标体检与投前控标任务主表）──────────────────────
class BidCheckTask(Base):
    __tablename__ = 'bid_check_tasks'

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    task_type = Column(String(32), nullable=False, default='pre_check')  # 'pre_check' | 'radar'
    title = Column(String(256), nullable=False)
    # 状态机：created -> extracting -> checking -> grading -> delivered（含 failed / need_manual）
    status = Column(String(32), nullable=False, default='created', index=True)
    progress = Column(Integer, nullable=False, default=0)
    stage_detail = Column(String(256), nullable=True)
    tender_filename = Column(String(256), nullable=True)
    bid_filename = Column(String(256), nullable=True)
    tender_path = Column(String(512), nullable=True)
    bid_path = Column(String(512), nullable=True)
    error_msg = Column(Text, nullable=True)
    stats_json = Column(JSON, nullable=True, default=dict)
    price_manual_json = Column(JSON, nullable=True)
    config_json = Column(JSON, nullable=True, default=dict)
    llm_usage_json = Column(JSON, nullable=True, default=dict)
    # E11 新增：任务启动时间、结束时间与分阶段耗时 JSON 文本（{"f1_ms","f2_ms","f3_ms","f4_ms","grading_ms","total_ms"}）
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    duration_json = Column(Text, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utc_now)
    updated_at = Column(DateTime, nullable=False, default=_utc_now, onupdate=_utc_now)

    tenant = relationship('Tenant', back_populates='tasks')
    user = relationship('User', back_populates='tasks')
    findings = relationship(
        'BidFinding',
        back_populates='task',
        cascade='all, delete-orphan',
        passive_deletes=True,
    )
    qa_sessions = relationship(
        'QaSession',
        back_populates='task',
        cascade='all, delete-orphan',
        passive_deletes=True,
    )


# ── 4. bid_findings（检出风险明细表）─────────────────────────────────────
class BidFinding(Base):
    __tablename__ = 'bid_findings'

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(
        Integer,
        ForeignKey('bid_check_tasks.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    source_module = Column(String(16), nullable=False, default='F1')  # 'F1'|'F2'|'F3'|'F4'|'F6'
    code = Column(String(64), nullable=False, default='')
    severity = Column(String(16), nullable=False, default='error')    # 'error'|'warning'|'info'
    category = Column(String(64), nullable=True)
    clause_no = Column(String(64), nullable=True)
    quote = Column(Text, nullable=True)
    bid_quote = Column(Text, nullable=True)                           # 双书勾稽时保存投标书对应摘录
    quote_offset = Column(Integer, nullable=True)
    quote_verified = Column(Boolean, nullable=False, default=True)
    page = Column(Integer, nullable=True)
    message = Column(Text, nullable=False)
    suggestion = Column(Text, nullable=True)
    consequence = Column(String(32), nullable=True)                   # 'veto'|'deduction'|'bonus'|'none'
    feedback = Column(String(16), nullable=False, default='pending')  # 'pending'|'accepted'|'rejected'
    feedback_note = Column(Text, nullable=True)
    detail_json = Column(JSON, nullable=True)                         # 扩展结构化字段（差额/双偏移/置信度）
    created_at = Column(DateTime, nullable=False, default=_utc_now)
    updated_at = Column(DateTime, nullable=False, default=_utc_now, onupdate=_utc_now)

    task = relationship('BidCheckTask', back_populates='findings')

    @property
    def rule_name(self) -> str:
        """供 core/redline.py 批注格式化器直接点属性访问。"""
        return self.category or self.code or self.source_module or '合规核查项'

    @property
    def legal_basis(self) -> str:
        """供 core/redline.py 批注格式化器直接点属性访问。"""
        if self.clause_no:
            return f"招标文件条款 {self.clause_no}"
        return self.consequence or '招标文件实质性要求'


# ── 5. bid_rule_configs（核查参数与自定义红线配置表）──────────────────────
class BidRuleConfig(Base):
    __tablename__ = 'bid_rule_configs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False, index=True)
    name = Column(String(128), nullable=False, default='默认核查规则')
    price_cap = Column(Numeric(14, 2), nullable=True)                 # 金额一律 Numeric(14, 2) 对应 Decimal
    open_date = Column(Date, nullable=True)
    buyer_name = Column(String(256), nullable=True)
    required_social_months_json = Column(JSON, nullable=False, default=list)
    custom_redlines_json = Column(JSON, nullable=False, default=list) # [{'keyword','severity','consequence'}]
    is_default = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=_utc_now)
    updated_at = Column(DateTime, nullable=False, default=_utc_now, onupdate=_utc_now)

    tenant = relationship('Tenant', back_populates='rule_configs')


# ── 6. company_certificates（企业投标证照效期台账表）──────────────────────
# ── 6. company_certificates（企业投标证照效期台账表）──────────────────────
class CompanyCertificate(Base):
    __tablename__ = 'company_certificates'

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False, index=True)
    cert_name = Column(String(256), nullable=False)
    cert_no = Column(String(128), nullable=True)
    valid_until = Column(Date, nullable=False)
    is_long_term = Column(Boolean, nullable=False, default=False)
    warn_days = Column(Integer, nullable=False, default=30)
    remark = Column(Text, nullable=True)
    # E25 §2.6：企业证照可选上传原件留档字段
    attachment_name = Column(String(256), nullable=True)
    attachment_path = Column(String(512), nullable=True)
    attachment_size = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utc_now)
    updated_at = Column(DateTime, nullable=False, default=_utc_now, onupdate=_utc_now)

    tenant = relationship('Tenant', back_populates='certificates')


# ── 7. qa_sessions（F7 标书专家追问会话持久化表）──────────────────────────
class QaSession(Base):
    __tablename__ = 'qa_sessions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    task_id = Column(
        Integer,
        ForeignKey('bid_check_tasks.id', ondelete='CASCADE'),
        nullable=True,
        index=True,
    )
    title = Column(String(256), nullable=False, default='新对话')
    tender_path = Column(String(512), nullable=True)
    bid_path = Column(String(512), nullable=True)
    messages_json = Column(JSON, nullable=False, default=list)        # [{'role','content','quotes','steps'}]
    attachments_json = Column(JSON, nullable=False, default=list)  # E24：会话级上传附件元数据列表
    created_at = Column(DateTime, nullable=False, default=_utc_now)
    updated_at = Column(DateTime, nullable=False, default=_utc_now, onupdate=_utc_now)

    task = relationship('BidCheckTask', back_populates='qa_sessions')


# ── 8. audit_logs（安全审计留痕表）────────────────────────────────────────
class AuditLog(Base):
    __tablename__ = 'audit_logs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete='SET NULL'), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    action = Column(String(64), nullable=False, index=True)
    target = Column(String(256), nullable=True)
    detail_json = Column(JSON, nullable=True)
    ip = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utc_now)


# ── 9. user_model_configs（用户级自定义大模型配置表）──────────────────────
class UserModelConfig(Base):
    __tablename__ = 'user_model_configs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        unique=True,
        index=True,
    )
    base_url = Column(String(256), nullable=False)
    api_key_encrypted = Column(Text, nullable=False)
    model_flash = Column(String(128), nullable=False)
    model_flagship = Column(String(128), nullable=True)
    supports_reasoning_effort = Column(Boolean, nullable=False, default=False)
    supports_vision = Column(Boolean, nullable=False, default=False)  # E24：是否支持原生 VLM 视觉传图
    created_at = Column(DateTime, nullable=False, default=_utc_now)
    updated_at = Column(DateTime, nullable=False, default=_utc_now, onupdate=_utc_now)

    user = relationship('User', back_populates='model_config_rel')

    def __repr__(self) -> str:
        return (
            f"<UserModelConfig(id={self.id}, user_id={self.user_id}, "
            f"base_url={self.base_url!r}, model_flash={self.model_flash!r}, api_key='***REDACTED***')>"
        )


# ── 10. regulation_laws（E4 招投标法律法规主表）───────────────────────────
class RegulationLaw(Base):
    __tablename__ = 'regulation_laws'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(256), nullable=False, unique=True, index=True)  # 《...》全称，唯一
    issuing_body = Column(String(128), nullable=True)
    effective_date = Column(String(64), nullable=True)
    source_url = Column(String(512), nullable=True)
    article_count = Column(Integer, nullable=False, default=0)
    content_hash = Column(String(64), nullable=False, default='', index=True)
    fetched_at = Column(DateTime, nullable=True)
    is_seed = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=_utc_now)
    updated_at = Column(DateTime, nullable=False, default=_utc_now, onupdate=_utc_now)

    articles = relationship(
        'RegulationArticle',
        back_populates='law',
        cascade='all, delete-orphan',
        passive_deletes=True,
    )


# ── 11. regulation_articles（E4 招投标法规条文表）─────────────────────────
class RegulationArticle(Base):
    __tablename__ = 'regulation_articles'
    __table_args__ = (
        UniqueConstraint('law_id', 'article_no', name='uq_regulation_law_article_no'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    law_id = Column(
        Integer,
        ForeignKey('regulation_laws.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    article_no = Column(String(64), nullable=False, index=True)          # 如 "第一条"
    article_title = Column(String(256), nullable=True)
    content = Column(Text, nullable=False)
    content_hash = Column(String(64), nullable=False, default='', index=True)
    created_at = Column(DateTime, nullable=False, default=_utc_now)
    updated_at = Column(DateTime, nullable=False, default=_utc_now, onupdate=_utc_now)

    law = relationship('RegulationLaw', back_populates='articles')


# ── E11 老库平滑加列守卫（无 Alembic 环境下自动探测缺列并执行 ALTER TABLE）────
_E11_TASK_NEW_COLS = (
    ('started_at', 'TIMESTAMP', 'DATETIME'),
    ('finished_at', 'TIMESTAMP', 'DATETIME'),
    ('duration_json', 'TEXT', 'TEXT'),
)


def _ensure_table_columns_generic(
    bind_or_conn: Any,
    table_name: str,
    col_specs: tuple[tuple[str, str, str], ...],
    *,
    auto_commit: bool = True,
) -> list[str]:
    """通用老库平滑加列辅助函数（兼容 PostgreSQL 与 SQLite）。"""
    added: list[str] = []
    if bind_or_conn is None:
        return added
    try:
        insp = inspect(bind_or_conn)
        if not insp.has_table(table_name):
            return added
        existing_cols = {c['name'] for c in insp.get_columns(table_name)}
        dialect_name = getattr(getattr(bind_or_conn, 'dialect', None), 'name', 'sqlite')

        missing_specs = [
            (col_name, pg_ddl if dialect_name == 'postgresql' else sq_ddl)
            for col_name, pg_ddl, sq_ddl in col_specs
            if col_name not in existing_cols
        ]
        if not missing_specs:
            return added

        if hasattr(bind_or_conn, 'begin') and not hasattr(bind_or_conn, 'in_transaction'):
            with bind_or_conn.begin() as conn:
                for col_name, col_ddl in missing_specs:
                    conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_ddl}"))
                    added.append(col_name)
        else:
            for col_name, col_ddl in missing_specs:
                bind_or_conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_ddl}"))
                added.append(col_name)
            if auto_commit and hasattr(bind_or_conn, 'commit') and getattr(bind_or_conn, 'in_transaction', lambda: False)():
                bind_or_conn.commit()
    except Exception:
        pass
    return added


def ensure_bid_check_tasks_columns(bind_or_conn: Any, *, auto_commit: bool = True) -> list[str]:
    """E11 §2.1：探测已有数据库的 bid_check_tasks 表，若缺少 started_at / finished_at / duration_json
    则自动执行 ALTER TABLE ADD COLUMN 平滑补齐（兼容 PostgreSQL 与 SQLite，已有行默认 NULL）。
    """
    return _ensure_table_columns_generic(
        bind_or_conn,
        'bid_check_tasks',
        _E11_TASK_NEW_COLS,
        auto_commit=auto_commit,
    )


def ensure_qa_sessions_columns(bind_or_conn: Any, *, auto_commit: bool = True) -> list[str]:
    """E24 §2.2：探测老库 qa_sessions 表，若缺少 attachments_json 列则自动补齐（默认值 '[]'）。"""
    return _ensure_table_columns_generic(
        bind_or_conn,
        'qa_sessions',
        (("attachments_json", "JSON NOT NULL DEFAULT '[]'", "JSON NOT NULL DEFAULT '[]'"),),
        auto_commit=auto_commit,
    )


def ensure_user_model_configs_columns(bind_or_conn: Any, *, auto_commit: bool = True) -> list[str]:
    """E24 §2.2：探测老库 user_model_configs 表，若缺少 supports_vision 列则自动补齐（默认值 FALSE/0）。"""
    return _ensure_table_columns_generic(
        bind_or_conn,
        'user_model_configs',
        (('supports_vision', 'BOOLEAN NOT NULL DEFAULT FALSE', 'BOOLEAN NOT NULL DEFAULT 0'),),
        auto_commit=auto_commit,
    )


_E25_CERT_NEW_COLS = (
    ('attachment_name', 'VARCHAR(256)', 'VARCHAR(256)'),
    ('attachment_path', 'VARCHAR(512)', 'VARCHAR(512)'),
    ('attachment_size', 'INTEGER', 'INTEGER'),
)

_E35_CERT_NEW_COLS = (
    ('is_long_term', 'BOOLEAN NOT NULL DEFAULT FALSE', 'BOOLEAN NOT NULL DEFAULT 0'),
)


def ensure_company_certificates_columns(bind_or_conn: Any, *, auto_commit: bool = True) -> list[str]:
    """E25 §2.6：探测老库 company_certificates 表，若缺少 attachment_name / attachment_path / attachment_size 则自动补齐。"""
    return _ensure_table_columns_generic(
        bind_or_conn,
        'company_certificates',
        _E25_CERT_NEW_COLS,
        auto_commit=auto_commit,
    )


def ensure_company_certificates_e35_columns(bind_or_conn: Any, *, auto_commit: bool = True) -> list[str]:
    """E35 §3.5：探测老库 company_certificates 表，若缺少 is_long_term 列则自动补齐（默认值 FALSE/0）。"""
    return _ensure_table_columns_generic(
        bind_or_conn,
        'company_certificates',
        _E35_CERT_NEW_COLS,
        auto_commit=auto_commit,
    )


@event.listens_for(Base.metadata, 'after_create')
def _auto_migrate_bid_check_tasks_after_create(target, connection, **kwargs):
    """在 Base.metadata.create_all（含 init_db 启动时）完成后自动探测老库并补齐 E11 / E24 / E25 / E35 新列。"""
    ensure_bid_check_tasks_columns(connection, auto_commit=False)
    ensure_qa_sessions_columns(connection, auto_commit=False)
    ensure_user_model_configs_columns(connection, auto_commit=False)
    ensure_company_certificates_columns(connection, auto_commit=False)
    ensure_company_certificates_e35_columns(connection, auto_commit=False)


def apply_sqlite_pragmas(engine: Any) -> None:
    """为 SQLite 引擎注册连接级 PRAGMA（开启 foreign_keys=ON 级联删除与 WAL 并发模式）。
    仅对 sqlite 方言生效；内存库 (:memory:) 开启 foreign_keys=ON。
    """
    if engine is None or getattr(engine.dialect, 'name', '') != 'sqlite':
        return

    @event.listens_for(engine, 'connect')
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute('PRAGMA foreign_keys=ON')
        try:
            cursor.execute('PRAGMA journal_mode=WAL')
            cursor.execute('PRAGMA busy_timeout=5000')
            # E11：对已有本地 SQLite 老库连接时顺带探测表结构并平滑补齐耗时列
            cursor.execute("PRAGMA table_info(bid_check_tasks)")
            cols = {row[1] for row in cursor.fetchall()}
            if cols:
                for col_name, _pg_t, sq_t in _E11_TASK_NEW_COLS:
                    if col_name not in cols:
                        cursor.execute(f"ALTER TABLE bid_check_tasks ADD COLUMN {col_name} {sq_t}")
            # E24：同步探测补齐 qa_sessions.attachments_json 与 user_model_configs.supports_vision
            cursor.execute("PRAGMA table_info(qa_sessions)")
            qa_cols = {row[1] for row in cursor.fetchall()}
            if qa_cols and 'attachments_json' not in qa_cols:
                cursor.execute("ALTER TABLE qa_sessions ADD COLUMN attachments_json JSON NOT NULL DEFAULT '[]'")
            cursor.execute("PRAGMA table_info(user_model_configs)")
            mc_cols = {row[1] for row in cursor.fetchall()}
            if mc_cols and 'supports_vision' not in mc_cols:
                cursor.execute("ALTER TABLE user_model_configs ADD COLUMN supports_vision BOOLEAN NOT NULL DEFAULT 0")
            # E25 / E35：同步探测补齐 company_certificates 的附件列与 is_long_term
            cursor.execute("PRAGMA table_info(company_certificates)")
            cc_cols = {row[1] for row in cursor.fetchall()}
            if cc_cols:
                for col_name, _pg_t, sq_t in (_E25_CERT_NEW_COLS + _E35_CERT_NEW_COLS):
                    if col_name not in cc_cols:
                        cursor.execute(f"ALTER TABLE company_certificates ADD COLUMN {col_name} {sq_t}")
        except Exception:
            pass
        cursor.close()
