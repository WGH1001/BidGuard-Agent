# -*- coding: utf-8 -*-
"""B1 · backend 数据层与完整鉴权层单元测试（sqlite:///:memory: 零磁盘污染）。

覆盖任务卡 B1 §4 全部验收标准：
1. 9 张表 create_all 成功、表名与字段名逐字对齐 INTERFACE.md §2、缺必填列即触发 IntegrityError；
2. users 密码哈希 roundtrip（hash -> verify 通过 / 错误密码与空密码拒绝）；
3. HMAC 令牌 roundtrip（签发 -> 校验通过；篡改 payload / 过期令牌 / 错误密钥 各 1 拒绝用例；
   以及 get_current_user 对过期/篡改/禁用用户分别抛 401，require_admin 对非管理员抛 403）；
4. 租户隔离：tenant A 创建的 task，用 tenant B 过滤条件查询结果严格为 0 条；
5. 级联删除：删除 task 后，关联的 bid_findings 与 qa_sessions 同步级联删除归零；
6. api_key 加密 roundtrip（复用 backend/utils/crypto.py 加密落库 -> 读出解密还原，repr 脱敏）；
7. SQLite 相对路径锚定 PROJECT_ROOT，不随 CWD 漂移。
"""
import base64
import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.auth import (
    create_token,
    get_current_user,
    hash_password,
    record_audit,
    require_admin,
    verify_password,
    verify_token,
)
from backend.database import (
    PROJECT_ROOT,
    Base,
    get_engine,
    get_session_factory,
    init_db,
    resolve_database_url,
)
from backend.models import (
    AuditLog,
    BidCheckTask,
    BidFinding,
    BidRuleConfig,
    CompanyCertificate,
    QaSession,
    Tenant,
    User,
    UserModelConfig,
)
from backend.utils.crypto import decrypt_api_key, encrypt_api_key


@pytest.fixture()
def db_session():
    """每个测试函数独立的内存 SQLite 数据库（开启 PRAGMA foreign_keys=ON）。"""
    eng = get_engine('sqlite:///:memory:')
    Base.metadata.create_all(bind=eng)
    SessionMaker = get_session_factory(eng)
    session = SessionMaker()
    try:
        yield eng, session
    finally:
        session.close()
        eng.dispose()


# ── 1. 9 张表 create_all 成功、列契约核对与缺必填列失败用例 ──────────────

def test_nine_tables_created_and_schema_matches_interface(db_session):
    eng, session = db_session
    inspector = inspect(eng)
    actual_tables = set(inspector.get_table_names())
    # E4 起扩容为 11 张表，E36 扩容为 12 张表（similarity_reports），E37 扩容为 13 张表（bid_compose_drafts）
    expected_tables = {
        'tenants',
        'users',
        'bid_check_tasks',
        'bid_findings',
        'bid_rule_configs',
        'company_certificates',
        'qa_sessions',
        'audit_logs',
        'user_model_configs',
        'regulation_laws',
        'regulation_articles',
        'similarity_reports',
        'bid_compose_drafts',
    }
    assert actual_tables == expected_tables
    assert len(actual_tables) == 13

    # 逐表核对 INTERFACE.md §2 关键字段
    expected_cols = {
        'tenants': {'id', 'name', 'is_active', 'created_at', 'updated_at'},
        'users': {
            'id', 'tenant_id', 'username', 'password_hash', 'display_name',
            'role', 'is_active', 'last_login_at', 'created_at', 'updated_at',
        },
        'bid_check_tasks': {
            'id', 'tenant_id', 'user_id', 'task_type', 'title', 'status',
            'progress', 'stage_detail', 'tender_filename', 'bid_filename',
            'tender_path', 'bid_path', 'error_msg', 'stats_json',
            'price_manual_json', 'config_json', 'llm_usage_json',
            'delivered_at', 'created_at', 'updated_at',
        },
        'bid_findings': {
            'id', 'task_id', 'source_module', 'code', 'severity', 'category',
            'clause_no', 'quote', 'bid_quote', 'detail_json', 'quote_offset',
            'quote_verified', 'page', 'message', 'suggestion', 'consequence',
            'feedback', 'feedback_note', 'created_at', 'updated_at',
        },
        'bid_rule_configs': {
            'id', 'tenant_id', 'name', 'price_cap', 'open_date', 'buyer_name',
            'required_social_months_json', 'custom_redlines_json', 'is_default',
            'created_at', 'updated_at',
        },
        'company_certificates': {
            'id', 'tenant_id', 'cert_name', 'cert_no', 'valid_until',
            'warn_days', 'remark', 'created_at', 'updated_at',
        },
        'qa_sessions': {
            'id', 'tenant_id', 'user_id', 'task_id', 'title',
            'tender_path', 'bid_path', 'messages_json', 'created_at', 'updated_at',
        },
        'audit_logs': {
            'id', 'tenant_id', 'user_id', 'action', 'target',
            'detail_json', 'ip', 'created_at',
        },
        'user_model_configs': {
            'id', 'user_id', 'base_url', 'api_key_encrypted', 'model_flash',
            'model_flagship', 'supports_reasoning_effort', 'created_at', 'updated_at',
        },
        'similarity_reports': {
            'id', 'tenant_id', 'user_id', 'title', 'tender_filename', 'tender_path',
            'bidders_json', 'pair_matrix_json', 'findings_json', 'max_similarity_pct',
            'risk_level', 'created_at', 'updated_at',
        },
        'bid_compose_drafts': {
            'id', 'tenant_id', 'user_id', 'title', 'tender_filename', 'tender_path',
            'compose_mode', 'blueprint_json', 'output_path', 'stats_json',
            'created_at', 'updated_at',
        },
    }
    for tbl_name, req_cols in expected_cols.items():
        cols_in_db = {c['name'] for c in inspector.get_columns(tbl_name)}
        assert req_cols.issubset(cols_in_db), f"{tbl_name} 缺少字段: {req_cols - cols_in_db}"

    # 缺必填列（如 Tenant.name=None）必须触发 IntegrityError
    bad_tenant = Tenant(name=None)
    session.add(bad_tenant)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ── 2. users 密码哈希 roundtrip（hash -> verify 通过 / 错误密码拒绝）──────

def test_user_password_bcrypt_roundtrip(db_session):
    _, session = db_session
    tenant = Tenant(name='政企系统集成投标部')
    session.add(tenant)
    session.flush()

    raw_pwd = 'BidGuard#2026!Secret'
    hashed = hash_password(raw_pwd)
    assert hashed.startswith('$2b$') or hashed.startswith('$2a$')
    assert raw_pwd not in hashed

    u = User(
        tenant_id=tenant.id,
        username='liming',
        password_hash=hashed,
        display_name='李明',
        role='member',
    )
    session.add(u)
    session.commit()

    saved_u = session.query(User).filter_by(username='liming').one()
    assert verify_password(raw_pwd, saved_u.password_hash) is True
    assert verify_password('WrongPassword123', saved_u.password_hash) is False
    assert verify_password('', saved_u.password_hash) is False
    assert verify_password(None, saved_u.password_hash) is False


def test_username_globally_unique_across_tenants(db_session):
    """username 全局唯一（INTERFACE.md §2 修订版）：跨租户同名注册必须触发 IntegrityError。"""
    _, session = db_session
    t_a = Tenant(name='租户甲')
    t_b = Tenant(name='租户乙')
    session.add_all([t_a, t_b])
    session.flush()

    session.add(User(tenant_id=t_a.id, username='admin',
                     password_hash=hash_password('Pass1234!'), role='admin'))
    session.commit()

    session.add(User(tenant_id=t_b.id, username='admin',
                     password_hash=hash_password('Pass5678!'), role='member'))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    assert session.query(User).filter_by(username='admin').count() == 1


# ── 3. HMAC 令牌签发与拒绝用例（篡改 payload / 过期令牌 / 错误密钥 / 禁用用户）──

def test_hmac_token_roundtrip_and_rejections(db_session):
    _, session = db_session
    tenant = Tenant(name='弱电智能化工程部')
    session.add(tenant)
    session.flush()

    user = User(
        tenant_id=tenant.id,
        username='zhangwei',
        password_hash=hash_password('Pass1234!'),
        display_name='张伟',
        role='admin',
        is_active=True,
    )
    session.add(user)
    session.commit()

    # (a) 正常签发与校验 roundtrip；payload 权威键锁死为 sub/tenant_id/role/exp 四键
    token = create_token(uid=user.id, tenant=tenant.id, role=user.role, username=user.username)
    payload = verify_token(token)
    assert payload is not None
    assert set(payload.keys()) == {'sub', 'tenant_id', 'role', 'exp'}
    assert payload['sub'] == user.username
    assert payload['tenant_id'] == tenant.id
    assert payload['role'] == 'admin'
    assert payload['exp'] > 0

    # (b) 拒绝用例 1：篡改 payload（冒用他人身份但签名不变）
    b64_part, sig_part = token.split('.', 1)
    tampered_data = json.loads(base64.urlsafe_b64decode(b64_part + '==').decode('utf-8'))
    tampered_data['sub'] = 'attacker'
    tampered_b64 = base64.urlsafe_b64encode(
        json.dumps(tampered_data).encode('utf-8')
    ).rstrip(b'=').decode('ascii')
    tampered_token = f"{tampered_b64}.{sig_part}"
    assert verify_token(tampered_token) is None
    with pytest.raises(HTTPException) as exc_sig:
        get_current_user(authorization=f"Bearer {tampered_token}", db=session)
    assert exc_sig.value.status_code == 401

    # (c) 拒绝用例 2：过期令牌（expires_seconds=-10）
    expired_token = create_token(uid=user.id, tenant=tenant.id, role='admin', expires_seconds=-10)
    assert verify_token(expired_token) is None
    with pytest.raises(HTTPException) as exc_exp:
        get_current_user(authorization=f"Bearer {expired_token}", db=session)
    assert exc_exp.value.status_code == 401

    # (d) 拒绝用例 3：错误密钥签发/校验
    wrong_key_token = create_token(
        uid=user.id, tenant=tenant.id, role='admin', secret='attacker-wrong-secret-key'
    )
    assert verify_token(wrong_key_token) is None

    # (e) get_current_user 通过与用户禁用 401 阻断
    cur_user = get_current_user(authorization=f"Bearer {token}", db=session)
    assert cur_user['uid'] == user.id
    assert cur_user.tenant_id == tenant.id
    assert require_admin(cur_user)['role'] == 'admin'

    user.is_active = False
    session.commit()
    with pytest.raises(HTTPException) as exc_disabled:
        get_current_user(authorization=f"Bearer {token}", db=session)
    assert exc_disabled.value.status_code == 401


# ── 4. 多租户严格隔离：tenant A 创建的 task，用 tenant B 过滤查不到 ─────────

def test_tenant_isolation_between_departments(db_session):
    _, session = db_session
    tenant_a = Tenant(name='租户A-系统集成部')
    tenant_b = Tenant(name='租户B-智能装备部')
    session.add_all([tenant_a, tenant_b])
    session.flush()

    task_a = BidCheckTask(
        tenant_id=tenant_a.id,
        task_type='pre_check',
        title='某省政务云态势感知项目封标体检',
        status='delivered',
        progress=100,
    )
    session.add(task_a)
    session.commit()

    rows_a = session.query(BidCheckTask).filter_by(tenant_id=tenant_a.id).all()
    rows_b = session.query(BidCheckTask).filter_by(tenant_id=tenant_b.id).all()
    assert len(rows_a) == 1
    assert rows_a[0].title == '某省政务云态势感知项目封标体检'
    assert len(rows_b) == 0


# ── 5. 级联删除：删除 task 后其 bid_findings 与 qa_sessions 同步消失 ───────

def test_cascade_delete_task_removes_findings_and_qa_sessions(db_session):
    _, session = db_session
    tenant = Tenant(name='级联测试投标部')
    session.add(tenant)
    session.flush()

    task = BidCheckTask(
        tenant_id=tenant.id,
        task_type='pre_check',
        title='市智慧园区弱电工程体检任务',
        status='checking',
    )
    session.add(task)
    session.flush()

    f1 = BidFinding(
        task_id=task.id,
        source_module='F2',
        code='BID_TOTAL_MISMATCH',
        severity='error',
        message='分项累加与一览表总价差 500 元',
        consequence='veto',
    )
    f2 = BidFinding(
        task_id=task.id,
        source_module='F4',
        code='MISSING_RESPONSE',
        severity='error',
        message='漏应答 ★4.2.8 银河麒麟适配要求',
        consequence='veto',
    )
    qa = QaSession(
        tenant_id=tenant.id,
        task_id=task.id,
        title='关于质保期的追问',
        messages_json=[{'role': 'user', 'content': '质保期几年？'}],
    )
    session.add_all([f1, f2, qa])
    session.commit()

    assert len(session.query(BidFinding).filter_by(task_id=task.id).all()) == 2
    assert len(session.query(QaSession).filter_by(task_id=task.id).all()) == 1

    # 删除父任务，验证子表级联清空
    session.delete(task)
    session.commit()

    assert len(session.query(BidFinding).all()) == 0
    assert len(session.query(QaSession).all()) == 0


# ── 6. API Key 加密落库与解密还原 roundtrip（复用 backend/utils/crypto.py）──

def test_user_model_config_api_key_encryption_roundtrip(db_session):
    _, session = db_session
    tenant = Tenant(name='加密测试部')
    session.add(tenant)
    session.flush()

    user = User(
        tenant_id=tenant.id,
        username='sec_user',
        password_hash=hash_password('SecPass123!'),
        display_name='安全测试员',
        role='member',
    )
    session.add(user)
    session.flush()

    plain_api_key = 'sk-bidguard-live-secret-9988776655'
    cipher_text = encrypt_api_key(plain_api_key)
    assert cipher_text.startswith('v1:')
    assert plain_api_key not in cipher_text

    cfg = UserModelConfig(
        user_id=user.id,
        base_url='http://10.0.2.81:3000/v1',
        api_key_encrypted=cipher_text,
        model_flash='GLM-5.3-Flash',
        model_flagship='GLM-5.3',
        supports_reasoning_effort=True,
    )
    session.add(cfg)
    session.commit()

    loaded_cfg = session.query(UserModelConfig).filter_by(user_id=user.id).one()
    assert loaded_cfg.api_key_encrypted == cipher_text
    assert decrypt_api_key(loaded_cfg.api_key_encrypted) == plain_api_key
    # repr 严禁泄露明文或密文
    assert plain_api_key not in repr(loaded_cfg)
    assert cipher_text not in repr(loaded_cfg)


# ── 7. 金额 Numeric(14,2) 精度、审计留痕与 SQLite 路径根锚定核验 ──────────

def test_numeric_price_cap_audit_and_sqlite_path_anchoring(db_session, tmp_path):
    _, session = db_session
    tenant = Tenant(name='精度测试部')
    session.add(tenant)
    session.flush()

    rc = BidRuleConfig(
        tenant_id=tenant.id,
        name='政采145万限价规则',
        price_cap=Decimal('1450000.55'),
        open_date=date(2026, 11, 5),
        buyer_name='某省大数据局',
        required_social_months_json=['2026-08', '2026-09', '2026-10'],
        custom_redlines_json=[{'keyword': '转包', 'severity': 'error', 'consequence': 'veto'}],
    )
    session.add(rc)
    session.commit()

    loaded_rc = session.query(BidRuleConfig).filter_by(tenant_id=tenant.id).one()
    assert loaded_rc.price_cap == Decimal('1450000.55')
    assert isinstance(loaded_rc.price_cap, Decimal)

    # 审计留痕辅助函数核验
    audit_id = record_audit(
        session,
        action='update_rule_config',
        tenant_id=tenant.id,
        target='BidRuleConfig:1',
        detail_json={'price_cap': '1450000.55'},
        ip='127.0.0.1',
    )
    assert audit_id is not None
    logs = session.query(AuditLog).filter_by(tenant_id=tenant.id).all()
    assert len(logs) == 1
    assert logs[0].action == 'update_rule_config'

    # 验证相对路径 SQLite URL 锚定在 PROJECT_ROOT，不随 CWD 改变
    orig_cwd = os.getcwd()
    try:
        os.chdir(str(tmp_path))
        resolved = resolve_database_url('sqlite:///./data/bidguard.db')
        expected_path = (PROJECT_ROOT / 'data' / 'bidguard.db').resolve().as_posix()
        assert resolved == f"sqlite:///{expected_path}"
    finally:
        os.chdir(orig_cwd)