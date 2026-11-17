# -*- coding: utf-8 -*-
"""E35 · 管理增强与部署诚实化卡全链路测试。

覆盖 E35 §5 全部 12 条验收测试：
1. SEED_DEMO_DATA=0 生产空库：中性默认部门 + admin 可直接登录 + 无 liming/演示部门/假规则/演示证照 + 重命名后重启幂等；
2. SEED_DEMO_DATA=1（默认）：现状全量保留，依赖 liming 的既有行为不回归；
3. 部门重命名（PATCH /api/auth/tenants/{id}）：happy path + 非法权限 403 + 重名 409 + 空名 422 + rename_tenant 审计；
4. username 改名：唯一性 409、字符集 422、旧 token 401 强制重登、新名登录与 old/new 审计；
5. 前端静态约束：UsersManageView 默认隐藏禁用账号、确认弹框与改自身登出；RulesAndCertsView 无假日期硬编码；ExtractView 长期显示；
6. parse-file 预填标注：OCR 命中 -> cert_name_source='ocr'；未命中 -> 'filename_fallback' 且联动 is_long_term_hint；
7. 长期有效正则 hint：「长期有效」「永久」「无固定期限」「营业期限：至长期」-> is_long_term_hint=True；
8. 长期有效证照全栈闭环（落库 9999-12-31 哨兵、接口与审计均返回 valid_until=None / is_long_term=True、expiring_only/dashboard 不误报警）；
9. SQLite 老库无 is_long_term 列平滑迁移幂等；
10. 证照对账混排边界：同证名既有长期证又有有限期证时，排序无 TypeError 且长期证排前；
11. 安全断链回归：SEED_DEMO_DATA=0 下持未知/已改名 sub 签名 token 调接口严格 401 USER_NOT_FOUND；
12. 营业执照实弹模拟：无有效期的营业执照 PNG/PDF 上传 parse-file -> 文件名兜底标注 + 长期提示 + F3 对账判 valid。
"""
from datetime import date
import os
from pathlib import Path
import sqlite3
import sys

from fastapi.testclient import TestClient
import pymupdf
import pytest
from sqlalchemy import inspect

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.auth import create_token
from backend.database import SEED_TENANTS, get_engine, init_db
from backend.main import create_app
from backend.models import (
    AuditLog,
    CompanyCertificate,
    Tenant,
    User,
    ensure_company_certificates_columns,
    ensure_company_certificates_e35_columns,
)
from backend.routers.rules import _extract_cert_fields_from_text
from core.qualification_scanner import match_required_certificates_with_ledger

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _make_pdf_bytes(text: str) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    font_path = Path(r'C:\Windows\Fonts\msyh.ttc')
    if font_path.is_file():
        page.insert_font(fontname='cjk', fontfile=str(font_path))
        page.insert_text((72, 90), text, fontsize=12, fontname='cjk')
    else:
        page.insert_text((72, 90), text, fontsize=12)
    raw = doc.tobytes()
    doc.close()
    return raw


def _login(tc: TestClient, username: str, password: str = 'BidGuard2026!') -> str:
    resp = tc.post('/api/auth/login', json={'username': username, 'password': password})
    assert resp.status_code == 200, resp.text
    return resp.json()['token']


# ── 1 & 11. SEED_DEMO_DATA=0 生产空库诚实化 + 断链安全阻断 ──────────────────

def test_e35_prod_bootstrap_honest_no_demo_leak_and_auth_fail_closed(tmp_path: Path, monkeypatch):
    db_file = tmp_path / 'e35_prod.db'
    db_url = f"sqlite:///{db_file.as_posix()}"
    monkeypatch.setenv('SEED_DEMO_DATA', '0')
    monkeypatch.setenv('SHOW_DEMO_LOGIN', '1')
    monkeypatch.setenv('DEFAULT_TENANT_NAME', '华东招投标事业部')
    monkeypatch.setenv('ADMIN_INITIAL_PASSWORD', 'ProdAdmin#2026')

    app = create_app(database_url=db_url, auto_init_db=True)
    with TestClient(app) as tc:
        health = tc.get('/api/auth/health').json()
        assert health['show_demo_login'] is False

        # liming 绝对不存在，不得可登录
        r_liming = tc.post('/api/auth/login', json={'username': 'liming', 'password': 'BidGuard2026!'})
        assert r_liming.status_code == 401

        # 伪造有效签名但库中不存在 sub='liming' 的 token -> 严格 401 USER_NOT_FOUND，不得落入 id=None 兜底
        forged_token = create_token(username='liming', tenant_id=1, role='admin')
        r_forged_me = tc.get('/api/auth/me', headers={'Authorization': f'Bearer {forged_token}'})
        assert r_forged_me.status_code == 401
        assert r_forged_me.json()['detail']['code'] == 'USER_NOT_FOUND'
        r_forged_users = tc.get('/api/auth/users', headers={'Authorization': f'Bearer {forged_token}'})
        assert r_forged_users.status_code == 401

        # 生产 admin 直接可登录且属于中性默认部门
        r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'ProdAdmin#2026'})
        assert r_login.status_code == 200, r_login.text
        login_data = r_login.json()
        assert login_data['user']['tenant_name'] == '华东招投标事业部'
        hdrs = {'Authorization': f"Bearer {login_data['token']}"}

        tenants = tc.get('/api/auth/tenants', headers=hdrs).json()
        assert len(tenants) == 1
        assert tenants[0]['name'] == '华东招投标事业部'
        for demo_name in SEED_TENANTS:
            assert demo_name not in [t['name'] for t in tenants]

        users = tc.get('/api/auth/users', headers=hdrs).json()
        assert [u['username'] for u in users] == ['admin']

        rules = tc.get('/api/rules', headers=hdrs).json()
        assert len(rules) == 1
        assert rules[0]['price_cap'] is None
        assert rules[0]['open_date'] is None
        assert rules[0]['buyer_name'] == ''
        assert rules[0]['required_social_months_json'] == []

        certs = tc.get('/api/rules/certificates', headers=hdrs).json()
        assert certs == []

        # 将默认部门与管理员改名后再次 init_db(seed=False)，必须幂等保持不重建默认项
        tid = tenants[0]['id']
        uid = users[0]['id']
        assert tc.patch(f'/api/auth/tenants/{tid}', headers=hdrs, json={'name': '集团总办'}).status_code == 200
        assert tc.patch(f'/api/auth/users/{uid}', headers=hdrs, json={'username': 'chief_admin'}).status_code == 200

    eng2 = init_db(database_url=db_url, seed=False)
    try:
        from backend.database import get_session_factory
        sess = get_session_factory(eng2)()
        try:
            all_tenants = sess.query(Tenant).all()
            all_users = sess.query(User).all()
            assert [t.name for t in all_tenants] == ['集团总办']
            assert [u.username for u in all_users] == ['chief_admin']
        finally:
            sess.close()
    finally:
        eng2.dispose()


def test_e35_prod_bootstrap_default_tenant_fallback(tmp_path: Path, monkeypatch):
    db_file = tmp_path / 'e35_prod_default_name.db'
    monkeypatch.setenv('SEED_DEMO_DATA', '0')
    monkeypatch.delenv('DEFAULT_TENANT_NAME', raising=False)
    monkeypatch.delenv('ADMIN_INITIAL_PASSWORD', raising=False)

    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)
    with TestClient(app) as tc:
        token = _login(tc, 'admin')
        me = tc.get('/api/auth/me', headers={'Authorization': f'Bearer {token}'}).json()
        assert me['tenant_name'] == '默认机构'


# ── 2. SEED_DEMO_DATA=1 演示模式向后兼容 ──────────────────────────────────────

def test_e35_demo_mode_keeps_full_seed_and_liming(tmp_path: Path, monkeypatch):
    db_file = tmp_path / 'e35_demo.db'
    monkeypatch.setenv('SEED_DEMO_DATA', '1')
    monkeypatch.setenv('SHOW_DEMO_LOGIN', '1')
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)
    with TestClient(app) as tc:
        assert tc.get('/api/auth/health').json()['show_demo_login'] is True
        admin_tok = _login(tc, 'admin')
        liming_tok = _login(tc, 'liming')
        assert liming_tok
        tenants = tc.get('/api/auth/tenants', headers={'Authorization': f'Bearer {admin_tok}'}).json()
        assert [t['name'] for t in tenants] == SEED_TENANTS
        certs = tc.get('/api/rules/certificates', headers={'Authorization': f'Bearer {admin_tok}'}).json()
        assert len(certs) >= 2


# ── 3 & 4. 部门重命名 + 用户 username 重命名 + 401 强制重登 + 审计留痕 ───────

def test_e35_tenant_rename_and_username_rename_audit_chain(tmp_path: Path, monkeypatch):
    db_file = tmp_path / 'e35_renames.db'
    monkeypatch.setenv('SEED_DEMO_DATA', '1')
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    with TestClient(app) as tc:
        admin_hdrs = {'Authorization': f"Bearer {_login(tc, 'admin')}"}
        liming_tok = _login(tc, 'liming')
        liming_hdrs = {'Authorization': f'Bearer {liming_tok}'}

        # 非管理员无权重命名部门 -> 403
        assert tc.patch('/api/auth/tenants/1', headers=liming_hdrs, json={'name': '非法部'}).status_code == 403

        # 空名称 -> 422；不存在部门 -> 404；撞第 2 个部门重名 -> 409
        assert tc.patch('/api/auth/tenants/1', headers=admin_hdrs, json={'name': '   '}).status_code == 422
        assert tc.patch('/api/auth/tenants/999', headers=admin_hdrs, json={'name': '不存在部'}).status_code == 404
        assert tc.patch('/api/auth/tenants/1', headers=admin_hdrs, json={'name': SEED_TENANTS[1]}).status_code == 409

        # 重命名为自身原名允许幂等通过，改新名成功并带审计
        r_ren = tc.patch('/api/auth/tenants/1', headers=admin_hdrs, json={'name': '政企大客户投标一部'})
        assert r_ren.status_code == 200, r_ren.text
        assert r_ren.json()['name'] == '政企大客户投标一部'
        assert tc.get('/api/auth/me', headers=admin_hdrs).json()['tenant_name'] == '政企大客户投标一部'

        # 查找 liming 用户 id
        users = tc.get('/api/auth/users', headers=admin_hdrs).json()
        liming_id = next(u['id'] for u in users if u['username'] == 'liming')

        # 改名冲突（撞 admin）-> 409 USERNAME_EXISTS
        r_dup = tc.patch(f'/api/auth/users/{liming_id}', headers=admin_hdrs, json={'username': 'admin'})
        assert r_dup.status_code == 409
        assert r_dup.json()['detail']['code'] == 'USERNAME_EXISTS'

        # 非法字符集 -> 422 INVALID_USERNAME
        for bad_u in ('', 'bad name', '张伟', 'bad@name'):
            r_bad = tc.patch(f'/api/auth/users/{liming_id}', headers=admin_hdrs, json={'username': bad_u})
            assert r_bad.status_code == 422, f'bad username={bad_u!r}'

        # 成功修改 liming -> liming.chen
        r_u_ren = tc.patch(
            f'/api/auth/users/{liming_id}',
            headers=admin_hdrs,
            json={'username': 'liming.chen', 'display_name': '陈李明'},
        )
        assert r_u_ren.status_code == 200, r_u_ren.text
        assert r_u_ren.json()['username'] == 'liming.chen'
        assert r_u_ren.json()['display_name'] == '陈李明'

        # 旧 token (sub='liming') 必须立即 401 USER_NOT_FOUND
        r_old_me = tc.get('/api/auth/me', headers=liming_hdrs)
        assert r_old_me.status_code == 401
        assert r_old_me.json()['detail']['code'] == 'USER_NOT_FOUND'

        # 新用户名可直接登录
        new_tok = _login(tc, 'liming.chen')
        me_new = tc.get('/api/auth/me', headers={'Authorization': f'Bearer {new_tok}'}).json()
        assert me_new['username'] == 'liming.chen'
        assert me_new['tenant_name'] == '政企大客户投标一部'

        # 验证 rename_tenant 与 admin_update_user 审计明细
        logs = tc.get('/api/reports/audit-logs', headers=admin_hdrs).json()['items']
        ren_t_log = next(l for l in logs if l['action'] == 'rename_tenant')
        assert ren_t_log['detail_json']['old_name'] == SEED_TENANTS[0]
        assert ren_t_log['detail_json']['new_name'] == '政企大客户投标一部'
        ren_u_log = next(l for l in logs if l['action'] == 'admin_update_user' and 'old_username' in (l['detail_json'] or {}))
        assert ren_u_log['detail_json']['old_username'] == 'liming'
        assert ren_u_log['detail_json']['new_username'] == 'liming.chen'


# ── 5 & 8. 前端行为与诚实预填静态约束核验 ────────────────────────────────────

def test_e35_frontend_honest_defaults_and_ui_guards():
    users_vue = (PROJECT_ROOT / 'frontend' / 'src' / 'views' / 'UsersManageView.vue').read_text(encoding='utf-8')
    assert 'showDisabledUsers = ref(false)' in users_vue
    assert 'if (!showDisabledUsers.value && !u.is_active)' in users_vue
    assert 'authStore.logout()' in users_vue
    assert '本地置顶会话需重新置顶' in users_vue

    rules_vue = (PROJECT_ROOT / 'frontend' / 'src' / 'views' / 'RulesAndCertsView.vue').read_text(encoding='utf-8')
    assert "'2026-11-20'" not in rules_vue
    assert "'2027-06-30'" not in rules_vue
    assert "'2026-11-25'" not in rules_vue
    assert "valid_until: ''" in rules_vue
    assert '名称来自文件名兜底，请人工核对' in rules_vue
    assert 'certForm.is_long_term' in rules_vue

    extract_vue = (PROJECT_ROOT / 'frontend' / 'src' / 'views' / 'ExtractView.vue').read_text(encoding='utf-8')
    assert '台账有效 · 长期' in extract_vue
    assert '有效期：长期有效' in extract_vue


# ── 6 & 7 & 12. 证照正则长期提示 + parse-file 来源标注 + 营业执照模拟 ─────────

@pytest.mark.parametrize(
    'sample_text',
    [
        '有效期：长期有效',
        '有效期限：永久',
        '营业期限：2018年05月01日至无固定期限',
        '营业期限：2016年03月10日至长期',
    ],
)
def test_e35_long_term_regex_detection(sample_text: str):
    res = _extract_cert_fields_from_text(sample_text)
    assert res['is_long_term_hint'] is True
    assert res['valid_until_hint'] is None


def test_e35_long_term_regex_negative_with_explicit_date():
    res = _extract_cert_fields_from_text('上海永久器材有限公司\n质量体系认证证书\n有效期至：2028年12月31日')
    assert res['valid_until_hint'] == '2028-12-31'
    assert res['is_long_term_hint'] is False


def test_e35_parse_file_source_labels_and_business_license_honest_prefill(tmp_path: Path, monkeypatch):
    db_file = tmp_path / 'e35_parse.db'
    monkeypatch.setenv('SEED_DEMO_DATA', '1')
    app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

    with TestClient(app) as tc:
        hdrs = {'Authorization': f"Bearer {_login(tc, 'admin')}"}

        # Case A: OCR 可提取出证照名称与截止日 -> cert_name_source='ocr'
        iso_pdf = _make_pdf_bytes('ISO/IEC 27001 信息安全管理体系认证证书\n证书编号：ISO-2026-009\n有效期至：2028年12月31日')
        r_ocr = tc.post(
            '/api/rules/certificates/parse-file',
            headers=hdrs,
            files={'file': ('scan_doc_001.pdf', iso_pdf, 'application/pdf')},
        )
        assert r_ocr.status_code == 200, r_ocr.text
        d_ocr = r_ocr.json()
        assert d_ocr['cert_name_source'] == 'ocr'
        assert '管理体系认证证书' in (d_ocr['cert_name_hint'] or '')
        assert d_ocr['valid_until_hint'] == '2028-12-31'
        assert d_ocr['is_long_term_hint'] is False

        # Case B: 无有效期的新版营业执照 -> 文件名兜底标注 + 无假日期 + is_long_term_hint=True
        # 直接模拟 parse_qa_attachment 返回典型营业执照短文本（含无前后缀“营业执照”字样）
        monkeypatch.setattr(
            'backend.routers.rules.parse_qa_attachment',
            lambda _p: {
                'text': '统一社会信用代码：91310000123456789X\n营业期限：2015年03月12日至长期',
                'is_image': True,
                'ocr_chars': 42,
            },
        )
        r_lic = tc.post(
            '/api/rules/certificates/parse-file',
            headers=hdrs,
            files={'file': ('公司新版营业执照扫描件.png', b'\x89PNG\r\n\x1a\nfakepng', 'image/png')},
        )
        assert r_lic.status_code == 200, r_lic.text
        d_lic = r_lic.json()
        assert d_lic['cert_name_hint'] == '公司新版营业执照扫描件'
        assert d_lic['cert_name_source'] == 'filename_fallback'
        assert d_lic['cert_no_hint'] == '91310000123456789X'
        assert d_lic['valid_until_hint'] is None
        assert d_lic['is_long_term_hint'] is True


# ── 8 & 9 & 10. 长期有效全链路 CRUD / 老库迁移 / 混排对账 ─────────────────────

def test_e35_long_term_cert_crud_sentinel_shield_and_legacy_migration(tmp_path: Path, monkeypatch):
    # 9. 老库无 is_long_term 与 attachment_* 列时平滑迁移且幂等
    legacy_db = tmp_path / 'legacy_certs.db'
    con = sqlite3.connect(str(legacy_db))
    con.execute('CREATE TABLE tenants (id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL)')
    con.execute(
        'CREATE TABLE company_certificates ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id INTEGER NOT NULL, '
        'cert_name VARCHAR(256) NOT NULL, cert_no VARCHAR(128), valid_until DATE NOT NULL, '
        'warn_days INTEGER NOT NULL DEFAULT 30, remark TEXT, created_at DATETIME, updated_at DATETIME)'
    )
    con.execute("INSERT INTO tenants (id, name) VALUES (1, '默认机构')")
    con.execute(
        "INSERT INTO company_certificates (tenant_id, cert_name, valid_until) "
        "VALUES (1, '历史证书', '2027-08-01')"
    )
    con.commit()
    con.close()

    # (a) 验证 ensure_company_certificates_columns 本身（模拟非 pragma 驱动如 PostgreSQL）首次补列、二次幂等
    from sqlalchemy import create_engine as _sa_create_engine
    raw_eng = _sa_create_engine(f"sqlite:///{legacy_db.as_posix()}")
    try:
        added_e25 = ensure_company_certificates_columns(raw_eng)
        assert set(added_e25) == {'attachment_name', 'attachment_path', 'attachment_size'}
        added_e35_1 = ensure_company_certificates_e35_columns(raw_eng)
        assert added_e35_1 == ['is_long_term']
        added_e35_2 = ensure_company_certificates_e35_columns(raw_eng)
        assert added_e35_2 == []
        cols = {c['name'] for c in inspect(raw_eng).get_columns('company_certificates')}
        assert 'is_long_term' in cols
    finally:
        raw_eng.dispose()

    # (b) 验证 Base.metadata.create_all 触发 after_create 事务安全补全（模拟 PostgreSQL 无 PRAGMA 钩子同时缺 E25+E35 列）
    pg_sim_db = tmp_path / 'legacy_pg_sim.db'
    con2 = sqlite3.connect(str(pg_sim_db))
    con2.execute('CREATE TABLE tenants (id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL)')
    con2.execute(
        'CREATE TABLE company_certificates ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id INTEGER NOT NULL, '
        'cert_name VARCHAR(256) NOT NULL, cert_no VARCHAR(128), valid_until DATE NOT NULL, '
        'warn_days INTEGER NOT NULL DEFAULT 30, remark TEXT, created_at DATETIME, updated_at DATETIME)'
    )
    con2.commit()
    con2.close()
    raw_pg_eng = _sa_create_engine(f"sqlite:///{pg_sim_db.as_posix()}")
    try:
        from backend.models import Base as _ModelBase
        _ModelBase.metadata.create_all(bind=raw_pg_eng)
        cols2 = {c['name'] for c in inspect(raw_pg_eng).get_columns('company_certificates')}
        assert {'attachment_name', 'attachment_path', 'attachment_size', 'is_long_term'}.issubset(cols2)
    finally:
        raw_pg_eng.dispose()

    # (c) 验证 get_engine 连接即可读出历史行且默认 is_long_term=False
    eng = get_engine(f"sqlite:///{legacy_db.as_posix()}")
    try:
        from backend.database import get_session_factory
        s_leg = get_session_factory(eng)()
        try:
            legacy_row = s_leg.query(CompanyCertificate).filter_by(cert_name='历史证书').one()
            assert bool(legacy_row.is_long_term) is False
        finally:
            s_leg.close()
    finally:
        eng.dispose()

    # 8. 全栈 API + DB 哨兵屏蔽 + Dashboard / expiring_only / 审计隔离 + F3 长期与有限混排
    api_db = tmp_path / 'e35_long_term_api.db'
    monkeypatch.setenv('SEED_DEMO_DATA', '0')
    app = create_app(database_url=f"sqlite:///{api_db.as_posix()}", auto_init_db=True)

    with TestClient(app) as tc:
        hdrs = {'Authorization': f"Bearer {_login(tc, 'admin')}"}

        # 非长期证照缺 valid_until 必须 422
        r_bad = tc.post(
            '/api/rules/certificates',
            headers=hdrs,
            json={'cert_name': '测试缺失日期证书', 'is_long_term': False, 'valid_until': None},
        )
        assert r_bad.status_code == 422
        assert r_bad.json()['detail']['code'] == 'MISSING_VALID_UNTIL'

        # 新增长期有效营业执照
        r_lt = tc.post(
            '/api/rules/certificates',
            headers=hdrs,
            json={
                'cert_name': '企业法人营业执照',
                'cert_no': '91310000123456789X',
                'is_long_term': True,
                'valid_until': None,
            },
        )
        assert r_lt.status_code == 200, r_lt.text
        lt_data = r_lt.json()
        assert lt_data['is_long_term'] is True
        assert lt_data['valid_until'] is None
        assert lt_data['days_remaining'] is None
        assert lt_data['is_expired'] is False
        assert lt_data['is_expiring_soon'] is False
        assert '9999-12-31' not in r_lt.text

        # 同类新增一张有限期营业执照，用于测试混排与 expiring_only/dashboard
        r_fin = tc.post(
            '/api/rules/certificates',
            headers=hdrs,
            json={
                'cert_name': '营业执照（旧副本）',
                'cert_no': 'OLD-001',
                'is_long_term': False,
                'valid_until': date.today().isoformat(),
                'warn_days': 30,
            },
        )
        assert r_fin.status_code == 200

        # expiring_only=True 只能返回临期旧证，不得返回长期证
        exp_list = tc.get('/api/rules/certificates?expiring_only=true', headers=hdrs).json()
        assert [c['cert_name'] for c in exp_list] == ['营业执照（旧副本）']

        # Dashboard 只统计 1 张临期旧证，不把长期证算进临期
        dash = tc.get('/api/dashboard', headers=hdrs).json()
        assert dash['expiring_certs_count'] == 1

        # 审计日志 detail_json 中绝不能外泄 9999-12-31 哨兵日
        logs = tc.get('/api/reports/audit-logs?action=create_certificate', headers=hdrs).json()['items']
        lt_audit = next(l for l in logs if l['target'] == f"cert:{lt_data['id']}")
        assert lt_audit['detail_json']['is_long_term'] is True
        assert lt_audit['detail_json']['valid_until'] is None

        # F3 证照混排对账验证：同时传 1 长期有效 + 1 有限期（正常有效 2029）-> 不抛 TypeError，且长期排前
        matched, summary = match_required_certificates_with_ledger(
            [
                {
                    'cert_name': '企业法人营业执照',
                    'category': 'enterprise_qualification',
                    'requirement_level': 'mandatory',
                    'source_section': '资格审查',
                    'quote': '须提供有效的营业执照复印件',
                    'quote_verified': True,
                }
            ],
            [
                {
                    'id': 10,
                    'cert_name': '营业执照',
                    'cert_no': 'FIN-2029',
                    'valid_until': date(2029, 12, 31),
                    'is_long_term': False,
                    'warn_days': 30,
                },
                {
                    'id': 20,
                    'cert_name': '企业法人营业执照',
                    'cert_no': '91310000123456789X',
                    'valid_until': None,
                    'is_long_term': True,
                    'warn_days': 30,
                },
            ],
            reference_date='2026-11-20',
        )
        assert summary['valid_count'] == 1
        assert summary['not_in_ledger_count'] == 0
        m0 = matched[0]
        assert m0['ledger_status'] == 'valid'
        assert m0['matched_count'] == 2
        assert m0['matched_cert']['id'] == 20
        assert m0['matched_cert']['is_long_term'] is True
        assert m0['matched_cert']['valid_until'] is None
        assert m0['matched_cert']['days_remaining'] is None

        # 也验证数据库里存的哨兵行直接传入 match_required_certificates_with_ledger 不泄密且判 valid
        from backend.database import get_session_factory
        eng_check = get_engine(f"sqlite:///{api_db.as_posix()}")
        try:
            s_chk = get_session_factory(eng_check)()
            try:
                orm_lt = s_chk.query(CompanyCertificate).filter_by(id=lt_data['id']).one()
                assert orm_lt.valid_until == date(9999, 12, 31)
                orm_matched, orm_sum = match_required_certificates_with_ledger(
                    [
                        {
                            'cert_name': '营业执照',
                            'category': 'enterprise_qualification',
                            'requirement_level': 'mandatory',
                            'source_section': '资格审查',
                            'quote': '营业执照',
                            'quote_verified': True,
                        }
                    ],
                    [orm_lt],
                    reference_date='2026-11-20',
                )
                assert orm_sum['valid_count'] == 1
                assert orm_matched[0]['matched_cert']['valid_until'] is None
                assert orm_matched[0]['matched_cert']['is_long_term'] is True
            finally:
                s_chk.close()
        finally:
            eng_check.dispose()
