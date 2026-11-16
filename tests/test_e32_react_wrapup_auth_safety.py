# -*- coding: utf-8 -*-
"""E32 · F7 轮数耗尽强制收口、登录页演示开关脱敏与轻量管理员用户管理全量单测。"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import tempfile

from docx import Document
from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import backend.database as db_mod
from backend.main import create_app
from backend.models import AuditLog, Tenant
from core.bid_react_agent import ask_bid_expert, ask_bid_expert_stream


@dataclass
class _MockResp:
    content: str


class _SeqMockClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = 0
        self.captured_messages = []

    def chat(self, messages, model_tier='flash', **kwargs):
        self.calls += 1
        self.captured_messages.append(messages)
        return _MockResp(content=self.handler(messages, self.calls))


def _save_temp_docx(paragraphs: list[str]) -> Path:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    fd, path = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    doc.save(path)
    return Path(path)


@pytest.fixture()
def client_env(tmp_path, monkeypatch):
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    monkeypatch.setenv('SHOW_DEMO_LOGIN', '1')
    monkeypatch.setenv('SEED_DEMO_DATA', '1')
    db_file = tmp_path / 'test_e32_auth.db'
    db_url = f"sqlite:///{db_file.as_posix()}"

    app = create_app(database_url=db_url, auto_init_db=True)
    with TestClient(app) as tc:
        yield tc


def _login_headers(tc: TestClient, username: str, password: str = 'BidGuard2026!') -> dict[str, str]:
    resp = tc.post('/api/auth/login', json={'username': username, 'password': password})
    assert resp.status_code == 200, resp.text
    return {'Authorization': f"Bearer {resp.json()['token']}"}


# ── 1. F7 轮数耗尽强制收口（含 02 包质保期实弹场景与流式事件验证）────────────

def test_e32_max_rounds_forced_wrapup_02pkg_warranty_sync_and_stream():
    tender_paras = [
        '第五章 商务及技术要求',
        '★1. 质保期要求：硬件设备自终验合格之日起免费质保 1 年，软件系统免费质保 2 年。',
        '以上为本项目售后维保硬性红线。',
    ]
    bid_paras = [
        '一、商务偏离响应说明',
        '我公司响应招标文件总体服务周期要求。',
        '二、历史业绩合同扫描件（湖州项目）',
        '湖州智慧水务二期项目合同摘要：整体质保期 3 年，验收日期 2024-05。',
    ]
    tender_p = _save_temp_docx(tender_paras)
    bid_p = _save_temp_docx(bid_paras)

    expected_answer = (
        '招标文件要求硬件免费质保 1 年、软件免费质保 2 年；'
        '投标书内未检出针对本项目的质保期明确响应（检索到的“质保期 3 年”来自过往湖州项目业绩合同，'
        '不属于本项目响应，存在漏应答废标风险）。'
    )

    def _handler(messages, call_idx):
        if call_idx == 1:
            return json.dumps(
                {'thought': '检索招标文件质保要求', 'action': 'search_tender', 'action_input': '质保期'},
                ensure_ascii=False,
            )
        if call_idx == 2:
            return json.dumps(
                {'thought': '检索投标文件质保响应', 'action': 'search_bid', 'action_input': '质保期'},
                ensure_ascii=False,
            )
        # 第 3 轮：强制收口轮，提示词必须明确禁止再调工具且警示历史合同张冠李戴
        joined = '\n'.join(str(m.get('content', '')) for m in messages)
        assert '严禁再调用' in joined
        assert '历史合同' in joined
        return json.dumps(
            {
                'thought': '结合两轮证据直接收口回答',
                'action': 'final',
                'action_input': {
                    'answer': expected_answer,
                    'quotes': [
                        '★1. 质保期要求：硬件设备自终验合格之日起免费质保 1 年，软件免费质保 2 年。',
                        '湖州智慧水务二期项目合同摘要：整体质保期 3 年，验收日期 2024-05。',
                    ],
                },
            },
            ensure_ascii=False,
        )

    try:
        # 同步包装器验证
        client = _SeqMockClient(_handler)
        res = ask_bid_expert(
            tender_p,
            bid_p,
            '投标文件里质保期响应了几年？是否满足招标要求？',
            client=client,
            max_rounds=2,
        )
        assert client.calls == 3
        assert res['rounds_used'] == 2
        assert res['terminated_by_max_rounds'] is True
        assert res['forced_final'] is True
        assert '未检出针对本项目的质保期明确响应' in res['answer']
        assert len(res['steps']) == 3
        assert res['steps'][-1]['round'] == 3
        assert res['steps'][-1]['action'] == 'final'
        assert any(q.get('quote_verified') is True for q in res['quotes'])

        # SSE 流式事件验证
        stream_client = _SeqMockClient(_handler)
        events = list(
            ask_bid_expert_stream(
                tender_p,
                bid_p,
                '投标文件里质保期响应了几年？是否满足招标要求？',
                client=stream_client,
                max_rounds=2,
            )
        )
        step_events = [e for e in events if e.get('type') == 'step']
        delta_events = [e for e in events if e.get('type') == 'answer_delta']
        done_events = [e for e in events if e.get('type') == 'done']

        assert any(
            e['data'].get('round') == 3 and e['data'].get('action') == 'final'
            for e in step_events
        )
        assert len(delta_events) >= 1
        assert len(done_events) == 1
        done_payload = done_events[0]['data']
        assert done_payload['terminated_by_max_rounds'] is True
        assert done_payload['forced_final'] is True
        assert done_payload['rounds_used'] == 2
        assert '未检出针对本项目的质保期明确响应' in done_payload['answer']
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)


def test_e32_forced_wrapup_prose_fallback_error_degrade_and_base_exception():
    tender_p = _save_temp_docx(['★1. 交付周期：合同签订后 30 日内完成交付。'])
    bid_p = _save_temp_docx(['我方严格响应交付期要求，承诺 30 日内交付。'])

    # Case A: 收口轮返回纯自然语言文本（未包裹 final JSON），也应采信为 forced_final=True
    def _prose_handler(messages, call_idx):
        if call_idx == 1:
            return json.dumps({'thought': '查招标', 'action': 'search_tender', 'action_input': '交付'})
        return '根据已检索条款，招标要求 30 日内交付，我方投标书承诺 30 日内完成。'

    # Case B: 收口轮抛 Exception 时不崩流，优雅回退并保留 collected_tool_quotes
    def _error_handler(messages, call_idx):
        if call_idx == 1:
            return json.dumps({'thought': '查招标', 'action': 'search_tender', 'action_input': '交付'})
        raise RuntimeError('mocked gateway 502 in wrapup')

    class _CustomBaseCancel(BaseException):
        pass

    # Case C: BaseException（如 _PipelineCancelled）必须透传，不得被吞
    def _base_cancel_handler(messages, call_idx):
        if call_idx == 1:
            return json.dumps({'thought': '查招标', 'action': 'search_tender', 'action_input': '交付'})
        raise _CustomBaseCancel('cancelled during forced wrapup')

    try:
        res_a = ask_bid_expert(tender_p, bid_p, '交付期几天？', client=_SeqMockClient(_prose_handler), max_rounds=1)
        assert res_a['terminated_by_max_rounds'] is True
        assert res_a['forced_final'] is True
        assert '30 日内' in res_a['answer']

        res_b = ask_bid_expert(tender_p, bid_p, '交付期几天？', client=_SeqMockClient(_error_handler), max_rounds=1)
        assert res_b['terminated_by_max_rounds'] is True
        assert res_b['forced_final'] is False
        assert '已达到最大工具检索轮数上限' in res_b['answer']

        with pytest.raises(_CustomBaseCancel):
            ask_bid_expert(tender_p, bid_p, '交付期几天？', client=_SeqMockClient(_base_cancel_handler), max_rounds=1)
    finally:
        tender_p.unlink(missing_ok=True)
        bid_p.unlink(missing_ok=True)


# ── 2. 登录页演示账号开关 SHOW_DEMO_LOGIN 与 SEED_DEMO_DATA 双开联动 ──────────

@pytest.mark.parametrize(
    ('show_val', 'seed_val', 'expected'),
    [
        ('1', '1', True),
        ('0', '1', False),
        ('false', '1', False),
        ('off', '1', False),
        (None, '1', True),
        ('1', '0', False),
        ('1', 'false', False),
    ],
)
def test_e32_show_demo_login_health_gating(client_env: TestClient, monkeypatch, show_val, seed_val, expected):
    if show_val is None:
        monkeypatch.delenv('SHOW_DEMO_LOGIN', raising=False)
    else:
        monkeypatch.setenv('SHOW_DEMO_LOGIN', show_val)
    monkeypatch.setenv('SEED_DEMO_DATA', seed_val)

    for path in ('/api/health', '/api/auth/health'):
        r = client_env.get(path)
        assert r.status_code == 200
        assert r.json()['show_demo_login'] is expected


# ── 3. 角色鉴权、参数枚举校验 (INVALID_ROLE)、防自锁双护栏与全流程管理 ─────────

def test_e32_member_forbidden_and_invalid_role_422(client_env: TestClient):
    member_h = _login_headers(client_env, 'liming')
    admin_h = _login_headers(client_env, 'admin')

    # 普通成员访问 6 个管理员接口均 403
    assert client_env.get('/api/auth/tenants', headers=member_h).status_code == 403
    assert client_env.post('/api/auth/tenants', headers=member_h, json={'name': '非法部'}).status_code == 403
    assert client_env.get('/api/auth/users', headers=member_h).status_code == 403
    assert client_env.post(
        '/api/auth/users',
        headers=member_h,
        json={'username': 'u1', 'password': 'Password123!', 'tenant_id': 1},
    ).status_code == 403
    assert client_env.patch('/api/auth/users/1', headers=member_h, json={'display_name': 'x'}).status_code == 403
    assert client_env.post(
        '/api/auth/users/1/reset-password',
        headers=member_h,
        json={'new_password': 'Password123!'},
    ).status_code == 403

    # Workbuddy 必改 1：非法 role（含首尾空格与越权名）均严格返回 422 INVALID_ROLE
    users = client_env.get('/api/auth/users', headers=admin_h).json()
    liming_id = next(u['id'] for u in users if u['username'] == 'liming')
    for bad_role in ('superadmin', 'admin ', ' member', '', 'ADMIN'):
        r_create = client_env.post(
            '/api/auth/users',
            headers=admin_h,
            json={
                'username': 'badrole_user',
                'password': 'Password123!',
                'role': bad_role,
                'tenant_id': 1,
            },
        )
        assert r_create.status_code == 422, f'create with bad_role={bad_role!r}: {r_create.text}'
        assert r_create.json()['detail']['code'] == 'INVALID_ROLE'

        r_patch = client_env.patch(
            f'/api/auth/users/{liming_id}',
            headers=admin_h,
            json={'role': bad_role},
        )
        assert r_patch.status_code == 422, f'patch with bad_role={bad_role!r}: {r_patch.text}'
        assert r_patch.json()['detail']['code'] == 'INVALID_ROLE'


def test_e32_last_admin_protected_and_cannot_modify_self_guardrails(client_env: TestClient):
    admin_h = _login_headers(client_env, 'admin')
    users = client_env.get('/api/auth/users', headers=admin_h).json()
    admin_row = next(u for u in users if u['username'] == 'admin')

    # 1. 系统当前仅有 1 名活跃 admin 时，禁用或降级自己必须命中 LAST_ADMIN_PROTECTED (409)
    r_disable_sole = client_env.patch(
        f"/api/auth/users/{admin_row['id']}",
        headers=admin_h,
        json={'is_active': False},
    )
    assert r_disable_sole.status_code == 409
    assert r_disable_sole.json()['detail']['code'] == 'LAST_ADMIN_PROTECTED'

    r_demote_sole = client_env.patch(
        f"/api/auth/users/{admin_row['id']}",
        headers=admin_h,
        json={'role': 'member'},
    )
    assert r_demote_sole.status_code == 409
    assert r_demote_sole.json()['detail']['code'] == 'LAST_ADMIN_PROTECTED'

    # 2. 新建第 2 名活跃 admin 后，系统不再只有 1 名活跃 admin -> 管理员尝试禁用/降级自己命中 CANNOT_MODIFY_SELF
    r_add_admin2 = client_env.post(
        '/api/auth/users',
        headers=admin_h,
        json={
            'username': 'admin2',
            'display_name': '副管理员',
            'password': 'Admin2Pass!123',
            'role': 'admin',
            'tenant_id': 1,
        },
    )
    assert r_add_admin2.status_code == 200, r_add_admin2.text
    admin2_id = r_add_admin2.json()['id']

    r_disable_self = client_env.patch(
        f"/api/auth/users/{admin_row['id']}",
        headers=admin_h,
        json={'is_active': False},
    )
    assert r_disable_self.status_code == 409
    assert r_disable_self.json()['detail']['code'] == 'CANNOT_MODIFY_SELF'

    r_demote_self = client_env.patch(
        f"/api/auth/users/{admin_row['id']}",
        headers=admin_h,
        json={'role': 'member'},
    )
    assert r_demote_self.status_code == 409
    assert r_demote_self.json()['detail']['code'] == 'CANNOT_MODIFY_SELF'

    # 3. 管理员可以合法禁用副管理员 admin2；admin2 既有 token 立即失效；仅剩 1 人后再次触发 LAST_ADMIN_PROTECTED
    admin2_h = _login_headers(client_env, 'admin2', 'Admin2Pass!123')
    assert client_env.get('/api/auth/me', headers=admin2_h).status_code == 200

    r_disable_other = client_env.patch(
        f'/api/auth/users/{admin2_id}',
        headers=admin_h,
        json={'is_active': False},
    )
    assert r_disable_other.status_code == 200
    assert r_disable_other.json()['is_active'] is False

    r_admin2_me_after = client_env.get('/api/auth/me', headers=admin2_h)
    assert r_admin2_me_after.status_code == 401
    assert r_admin2_me_after.json()['detail']['code'] == 'USER_DISABLED'

    r_demote_sole_again = client_env.patch(
        f"/api/auth/users/{admin_row['id']}",
        headers=admin_h,
        json={'role': 'member'},
    )
    assert r_demote_sole_again.status_code == 409
    assert r_demote_sole_again.json()['detail']['code'] == 'LAST_ADMIN_PROTECTED'


def test_e32_admin_tenant_user_lifecycle_dynamic_tenant_name_and_audit(client_env: TestClient):
    admin_h = _login_headers(client_env, 'admin')

    # 1. 新建独立业务部门
    r_tenant = client_env.post('/api/auth/tenants', headers=admin_h, json={'name': '西南政企集成中心'})
    assert r_tenant.status_code == 200, r_tenant.text
    tenant_id = r_tenant.json()['id']

    # 重复建同部门 409
    assert client_env.post('/api/auth/tenants', headers=admin_h, json={'name': '西南政企集成中心'}).status_code == 409

    # 2. 在新部门下建账号
    init_secret = 'InitZhangSan888!'
    r_user = client_env.post(
        '/api/auth/users',
        headers=admin_h,
        json={
            'username': 'zhangsan_sw',
            'display_name': '张三（西南）',
            'password': init_secret,
            'role': 'member',
            'tenant_id': tenant_id,
        },
    )
    assert r_user.status_code == 200, r_user.text
    u_data = r_user.json()
    assert u_data['tenant_name'] == '西南政企集成中心'
    uid = u_data['id']

    # 重复用户名 409
    assert client_env.post(
        '/api/auth/users',
        headers=admin_h,
        json={'username': 'zhangsan_sw', 'password': 'AnotherPass!1', 'tenant_id': tenant_id},
    ).status_code == 409

    # 3. 新账号登录，login 和 /me 返回真实动态 tenant_name（不再写死 tenant 1/2/3）
    r_login = client_env.post('/api/auth/login', json={'username': 'zhangsan_sw', 'password': init_secret})
    assert r_login.status_code == 200
    assert r_login.json()['user']['tenant_name'] == '西南政企集成中心'
    zs_h = {'Authorization': f"Bearer {r_login.json()['token']}"}

    r_me = client_env.get('/api/auth/me', headers=zs_h)
    assert r_me.status_code == 200
    assert r_me.json()['tenant_name'] == '西南政企集成中心'

    # 4. 管理员重置其密码
    reset_secret = 'ResetZhangSan999!'
    r_reset = client_env.post(
        f'/api/auth/users/{uid}/reset-password',
        headers=admin_h,
        json={'new_password': reset_secret},
    )
    assert r_reset.status_code == 200

    # 旧密码失效，新密码生效
    assert client_env.post('/api/auth/login', json={'username': 'zhangsan_sw', 'password': init_secret}).status_code == 401
    new_zs_h = _login_headers(client_env, 'zhangsan_sw', reset_secret)
    assert client_env.get('/api/auth/me', headers=new_zs_h).status_code == 200

    # 5. 禁用后既有令牌立即失效
    assert client_env.patch(
        f'/api/auth/users/{uid}',
        headers=admin_h,
        json={'is_active': False},
    ).status_code == 200
    assert client_env.get('/api/auth/me', headers=new_zs_h).status_code == 401

    # 6. 审计日志落库且绝不泄露明文密码/哈希
    session = db_mod.SessionLocal()
    try:
        logs = (
            session.query(AuditLog)
            .filter(
                AuditLog.action.in_(
                    ['admin_create_tenant', 'admin_create_user', 'admin_update_user', 'admin_reset_password']
                )
            )
            .all()
        )
        actions = {log.action for log in logs}
        assert actions == {'admin_create_tenant', 'admin_create_user', 'admin_update_user', 'admin_reset_password'}
        serialized = json.dumps([log.detail_json for log in logs], ensure_ascii=False)
        assert init_secret not in serialized
        assert reset_secret not in serialized
        assert 'password_hash' not in serialized
    finally:
        session.close()
