# -*- coding: utf-8 -*-
"""B1 · BidGuard-Agent 数据库 9 张核心 ORM 表定义（SQLAlchemy 2.0）。

契约依据：INTERFACE.md §2（表名、字段名、枚举值逐字对齐）。
双驱动兼容：PostgreSQL 16（生产主库）/ SQLite（开发与单测回退，开启 WAL 与 foreign_keys=ON）。
"""
from __future__ import annotations

from datetime import datetime, timezone
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
    event,
)
from sqlalchemy.orm import DeclarativeBase, relationship


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
class CompanyCertificate(Base):
    __tablename__ = 'company_certificates'

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False, index=True)
    cert_name = Column(String(256), nullable=False)
    cert_no = Column(String(128), nullable=True)
    valid_until = Column(Date, nullable=False)
    warn_days = Column(Integer, nullable=False, default=30)
    remark = Column(Text, nullable=True)
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
    created_at = Column(DateTime, nullable=False, default=_utc_now)
    updated_at = Column(DateTime, nullable=False, default=_utc_now, onupdate=_utc_now)

    user = relationship('User', back_populates='model_config_rel')

    def __repr__(self) -> str:
        # 铁律 2：明文与密文 key 严禁出现在 repr 或日志中
        return (
            f"<UserModelConfig(id={self.id}, user_id={self.user_id}, "
            f"base_url={self.base_url!r}, model_flash={self.model_flash!r}, api_key='***REDACTED***')>"
        )


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
        except Exception:
            pass
        cursor.close()