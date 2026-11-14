# -*- coding: utf-8 -*-
"""E28 · F7 多会话并发与项目树形侧栏卡回归测试。

覆盖核心后端契约与并发写保护：
1. `GET /api/qa/sessions`（不传 `task_id`）返回同一租户下跨项目全部会话，且均包含 `task_id` 与 `updated_at`，
   同时兼容 `?task_id=` 过滤；不同租户互相隔离。
2. 多会话并发 SSE 流式问答 (`POST /api/qa/sessions/{sid}/ask/stream`) + 同步问答 (`/ask`) + 撤回 (`/last-turn`)：
   使用 ThreadPoolExecutor + Barrier 让多路请求同时在途并并发落库到文件型 SQLite，
   断言 0 个 `database is locked` / 500 错误、每条会话的 `messages_json` 完整准确无串线、无漏回合。
3. `_persist_qa_turn_locked` 高并发写锁串行化 + `PRAGMA busy_timeout=5000` 生效验证。
4. 流式生成器中途异常时正确返回 `error` 事件、不泄露 `_QA_DB_WRITE_LOCK`、不污染 `messages_json`。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import sys
import threading
import time
from types import SimpleNamespace

from docx import Document
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.auth import hash_password
import backend.database as db_mod
from backend.main import create_app
from backend.models import Tenant, User
import backend.routers.qa as qa_router_mod


def _make_docx_bytes(lines: list[str]) -> bytes:
    doc = Document()
    for ln in lines:
        doc.add_paragraph(ln)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _parse_sse_events(raw_text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in raw_text.split('\n\n'):
        if not block.strip():
            continue
        ev_name = ''
        ev_data = ''
        for line in block.splitlines():
            if line.startswith('event:'):
                ev_name = line.split(':', 1)[1].strip()
            elif line.startswith('data:'):
                ev_data = line.split(':', 1)[1].strip()
        if ev_name and ev_data:
            events.append((ev_name, json.loads(ev_data)))
    return events


def _restore_db_globals(prev_engine, prev_session_local):
    if db_mod.engine is not None and db_mod.engine is not prev_engine:
        try:
            db_mod.engine.dispose()
        except Exception:
            pass
    db_mod.engine = prev_engine
    db_mod.SessionLocal = prev_session_local


def test_e28_list_all_sessions_across_projects_and_busy_timeout(tmp_path):
    prev_engine, prev_session_local = db_mod.engine, db_mod.SessionLocal
    try:
        db_file = tmp_path / 'test_e28_sessions.db'
        app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

        with db_mod.engine.connect() as conn:
            busy_ms = conn.exec_driver_sql('PRAGMA busy_timeout').scalar()
        assert int(busy_ms) >= 5000

        t_bytes = _make_docx_bytes(['第一章 商务要求', '★1. 质保期不少于24个月。'])
        b_bytes = _make_docx_bytes(['第一章 投标响应', '★1. 我方承诺质保期24个月。'])

        with TestClient(app) as tc:
            r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
            assert r_login.status_code == 200
            hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}

            r_t1 = tc.post(
                '/api/checks',
                headers=hdrs,
                files={
                    'tender_file': ('t1.docx', t_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                    'bid_file': ('b1.docx', b_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                },
                data={'title': 'E28项目A', 'auto_run': 'false'},
            )
            r_t2 = tc.post(
                '/api/checks',
                headers=hdrs,
                files={
                    'tender_file': ('t2.docx', t_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                    'bid_file': ('b2.docx', b_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                },
                data={'title': 'E28项目B', 'auto_run': 'false'},
            )
            tid_a = r_t1.json()['id']
            tid_b = r_t2.json()['id']

            s_a1 = tc.post('/api/qa/sessions', headers=hdrs, json={'task_id': tid_a, 'title': 'A-会话1'}).json()['id']
            s_a2 = tc.post('/api/qa/sessions', headers=hdrs, json={'task_id': tid_a, 'title': 'A-会话2'}).json()['id']
            s_b1 = tc.post('/api/qa/sessions', headers=hdrs, json={'task_id': tid_b, 'title': 'B-会话1'}).json()['id']

            # 不传 task_id 返回跨项目全部会话，且携带 task_id 与 updated_at
            r_all = tc.get('/api/qa/sessions', headers=hdrs)
            assert r_all.status_code == 200
            all_rows = r_all.json()
            returned_ids = {row['id'] for row in all_rows}
            assert {s_a1, s_a2, s_b1}.issubset(returned_ids)
            by_id = {row['id']: row for row in all_rows}
            assert by_id[s_a1]['task_id'] == tid_a
            assert by_id[s_a2]['task_id'] == tid_a
            assert by_id[s_b1]['task_id'] == tid_b
            for sid in (s_a1, s_a2, s_b1):
                assert isinstance(by_id[sid]['updated_at'], str) and len(by_id[sid]['updated_at']) >= 10

            # 传 task_id 仍精确过滤
            r_only_b = tc.get('/api/qa/sessions', headers=hdrs, params={'task_id': tid_b})
            assert r_only_b.status_code == 200
            only_b_ids = {row['id'] for row in r_only_b.json()}
            assert only_b_ids == {s_b1}

            # 不同租户隔离：新建租户2的用户登录后，GET /api/qa/sessions 与单会话详情均不可见租户1会话
            with db_mod.SessionLocal() as s:
                t2 = Tenant(name='E28第二投标部', is_active=True)
                s.add(t2)
                s.flush()
                u2 = User(
                    tenant_id=t2.id,
                    username='e28_tenant2',
                    password_hash=hash_password('Tenant2Pass!'),
                    display_name='租户2成员',
                    role='member',
                    is_active=True,
                )
                s.add(u2)
                s.commit()

            r_login2 = tc.post('/api/auth/login', json={'username': 'e28_tenant2', 'password': 'Tenant2Pass!'})
            assert r_login2.status_code == 200
            hdrs2 = {'Authorization': f"Bearer {r_login2.json()['token']}"}
            r_all_t2 = tc.get('/api/qa/sessions', headers=hdrs2)
            assert r_all_t2.status_code == 200
            assert r_all_t2.json() == []
            assert tc.get(f'/api/qa/sessions/{s_a1}', headers=hdrs2).status_code == 404
    finally:
        _restore_db_globals(prev_engine, prev_session_local)


def test_e28_concurrent_sse_streams_and_sync_ask_without_sqlite_lock(tmp_path, monkeypatch):
    prev_engine, prev_session_local = db_mod.engine, db_mod.SessionLocal
    try:
        db_file = tmp_path / 'test_e28_concurrent.db'
        app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

        t_bytes = _make_docx_bytes(['第一章 技术规范', '★1. 本项目质保期为36个月。'])
        b_bytes = _make_docx_bytes(['第一章 商务响应', '★1. 我方完全响应质保期36个月。'])

        stream_barrier = threading.Barrier(4)

        def _fake_ask_stream(
            *,
            tender_path,
            bid_path,
            question,
            history=None,
            attachments=None,
            client=None,
            max_rounds=6,
            reasoning_effort='low',
        ):
            # 让 4 路并发 SSE 请求在慢工作阶段汇合后再同时落库，真实压测并发 SQLite 写入
            stream_barrier.wait(timeout=5.0)
            time.sleep(0.03)
            yield {
                'type': 'step',
                'data': {'round': 1, 'thought': f'检索[{question}]', 'action': 'search_tender', 'action_input': '质保期'},
            }
            yield {
                'type': 'answer_delta',
                'data': {'delta': f'并发流回答：{question}'},
            }
            yield {
                'type': 'done',
                'data': {
                    'answer': f'并发流回答：{question}',
                    'mode': 'react',
                    'quotes': [{'source': '招标文件', 'quote': '★1. 本项目质保期为36个月。'}],
                    'steps': [{'round': 1, 'thought': '检索完成', 'action': 'search_tender'}],
                },
            }

        def _fake_ask_sync(**kwargs):
            time.sleep(0.05)
            q = kwargs.get('question', '')
            return {
                'answer': f'同步回答：{q}',
                'mode': 'react',
                'quotes': [{'source': '招标文件', 'quote': '★1. 本项目质保期为36个月。'}],
                'steps': [{'round': 1, 'thought': '同步检索', 'action': 'final'}],
            }

        monkeypatch.setattr(qa_router_mod, 'ask_bid_expert_stream', _fake_ask_stream)
        monkeypatch.setattr(qa_router_mod, 'ask_bid_expert', _fake_ask_sync)
        monkeypatch.setattr(qa_router_mod, 'resolve_effective_llm_client', lambda db, uid: (SimpleNamespace(), 'done'))

        with TestClient(app) as tc:
            r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
            assert r_login.status_code == 200
            hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}

            task_ids = []
            for idx in range(2):
                r_task = tc.post(
                    '/api/checks',
                    headers=hdrs,
                    files={
                        'tender_file': (f't{idx}.docx', t_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                        'bid_file': (f'b{idx}.docx', b_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                    },
                    data={'title': f'并发项目{idx}', 'auto_run': 'false'},
                )
                task_ids.append(r_task.json()['id'])

            stream_sids = [
                tc.post('/api/qa/sessions', headers=hdrs, json={'task_id': task_ids[i % 2]}).json()['id']
                for i in range(4)
            ]
            sync_sids = [
                tc.post('/api/qa/sessions', headers=hdrs, json={'task_id': task_ids[i % 2]}).json()['id']
                for i in range(2)
            ]

            def _run_stream(idx: int) -> tuple[int, int, str]:
                sid = stream_sids[idx]
                q = f'并发问题-{idx}'
                resp = tc.post(
                    f'/api/qa/sessions/{sid}/ask/stream',
                    headers=hdrs,
                    json={'question': q, 'max_rounds': 6, 'reasoning_effort': 'low'},
                )
                events = _parse_sse_events(resp.text)
                assert resp.status_code == 200, f'stream {sid} failed: {resp.status_code} {resp.text}'
                assert events and events[-1][0] == 'done'
                return sid, resp.status_code, events[-1][1]['answer']

            def _run_sync(idx: int) -> tuple[int, int, str]:
                sid = sync_sids[idx]
                q = f'同步问题-{idx}'
                resp = tc.post(
                    f'/api/qa/sessions/{sid}/ask',
                    headers=hdrs,
                    json={'question': q, 'max_rounds': 6},
                )
                assert resp.status_code == 200, f'sync {sid} failed: {resp.status_code} {resp.text}'
                return sid, resp.status_code, resp.json()['answer']

            with ThreadPoolExecutor(max_workers=6) as pool:
                futs = [pool.submit(_run_stream, i) for i in range(4)] + [pool.submit(_run_sync, j) for j in range(2)]
                results = [f.result(timeout=15.0) for f in futs]

            assert len(results) == 6
            for i, sid in enumerate(stream_sids):
                r_sess = tc.get(f'/api/qa/sessions/{sid}', headers=hdrs)
                assert r_sess.status_code == 200
                msgs = r_sess.json()['messages_json']
                assert len(msgs) == 2
                assert msgs[0]['content'] == f'并发问题-{i}'
                assert msgs[1]['content'] == f'并发流回答：并发问题-{i}'
                assert msgs[1]['quotes'][0]['quote_verified'] is True

            for j, sid in enumerate(sync_sids):
                r_sess = tc.get(f'/api/qa/sessions/{sid}', headers=hdrs)
                assert r_sess.status_code == 200
                msgs = r_sess.json()['messages_json']
                assert len(msgs) == 2
                assert msgs[0]['content'] == f'同步问题-{j}'
                assert msgs[1]['content'] == f'同步回答：同步问题-{j}'

            # 撤回最后一条会话的末轮后验证仅该会话被清空
            r_wd = tc.delete(f'/api/qa/sessions/{sync_sids[0]}/last-turn', headers=hdrs)
            assert r_wd.status_code == 200
            wd_body = r_wd.json()
            assert wd_body['remaining_messages'] == 0
            assert wd_body['withdrawn_question'] == '同步问题-0'
            assert len(tc.get(f'/api/qa/sessions/{stream_sids[0]}', headers=hdrs).json()['messages_json']) == 2
    finally:
        _restore_db_globals(prev_engine, prev_session_local)


def test_e28_persist_lock_serializes_same_session_and_stream_error_releases_lock(tmp_path, monkeypatch):
    prev_engine, prev_session_local = db_mod.engine, db_mod.SessionLocal
    try:
        db_file = tmp_path / 'test_e28_same_sess.db'
        app = create_app(database_url=f"sqlite:///{db_file.as_posix()}", auto_init_db=True)

        t_bytes = _make_docx_bytes(['第一章 商务', '★1. 交货期30天。'])
        b_bytes = _make_docx_bytes(['第一章 响应', '★1. 交货期30天。'])

        with TestClient(app) as tc:
            r_login = tc.post('/api/auth/login', json={'username': 'admin', 'password': 'BidGuard2026!'})
            hdrs = {'Authorization': f"Bearer {r_login.json()['token']}"}
            r_task = tc.post(
                '/api/checks',
                headers=hdrs,
                files={
                    'tender_file': ('t.docx', t_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                    'bid_file': ('b.docx', b_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                },
                data={'title': '锁序列化压测', 'auto_run': 'false'},
            )
            tid = r_task.json()['id']
            sid = tc.post('/api/qa/sessions', headers=hdrs, json={'task_id': tid}).json()['id']
            me_id = tc.get('/api/auth/me', headers=hdrs).json()['id']
            tenant_id = tc.get('/api/auth/me', headers=hdrs).json()['tenant_id']

            # 直接用 8 线程同时经 _persist_qa_turn_locked 写同一会话，验证既无 sqlite locked 也不丢任何一轮
            write_barrier = threading.Barrier(8)

            def _worker(k: int) -> None:
                write_barrier.wait(timeout=5.0)
                qa_router_mod._persist_qa_turn_locked(
                    db_mod.SessionLocal,
                    is_factory=True,
                    session_id=sid,
                    tenant_id=tenant_id,
                    user_id=me_id,
                    question=f'压测问题-{k}',
                    bubble_atts=[],
                    answer=f'压测回答-{k}',
                    mode='react',
                    quotes_payload=[],
                    steps_payload=[],
                    attachments_count=0,
                    is_stream=True,
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                for f in [pool.submit(_worker, k) for k in range(8)]:
                    f.result(timeout=15.0)

            assert qa_router_mod._QA_DB_WRITE_LOCK.locked() is False
            r_sess = tc.get(f'/api/qa/sessions/{sid}', headers=hdrs)
            assert r_sess.status_code == 200
            msgs = r_sess.json()['messages_json']
            assert len(msgs) == 16  # 8 轮 * 2 条，无任何一轮被覆盖丢失
            questions = {m['content'] for m in msgs if m['role'] == 'user'}
            assert questions == {f'压测问题-{k}' for k in range(8)}

            # 流式中途异常：应返回 error 事件，锁必须释放，且不会写入残缺轮次
            def _broken_stream(**kwargs):
                yield {'type': 'step', 'data': {'round': 1, 'thought': '开始检索', 'action': 'search_tender'}}
                raise RuntimeError('simulated upstream timeout')

            monkeypatch.setattr(qa_router_mod, 'ask_bid_expert_stream', _broken_stream)
            monkeypatch.setattr(qa_router_mod, 'resolve_effective_llm_client', lambda db, uid: (SimpleNamespace(), 'done'))

            r_err = tc.post(
                f'/api/qa/sessions/{sid}/ask/stream',
                headers=hdrs,
                json={'question': '故障问题'},
            )
            assert r_err.status_code == 200
            events = _parse_sse_events(r_err.text)
            assert events[-1][0] == 'error'
            assert qa_router_mod._QA_DB_WRITE_LOCK.locked() is False
            msgs_after = tc.get(f'/api/qa/sessions/{sid}', headers=hdrs).json()['messages_json']
            assert len(msgs_after) == 16
    finally:
        _restore_db_globals(prev_engine, prev_session_local)
