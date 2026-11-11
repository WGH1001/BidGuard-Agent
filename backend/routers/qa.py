# -*- coding: utf-8 -*-
"""B2 / E21 · 第 7 组路由：/api/qa（会话 CRUD + 同步 POST /{session_id}/ask + 流式 POST /{session_id}/ask/stream，均注入多轮会话 history）。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit
import backend.database as db_mod
from backend.database import get_db
from backend.models import BidCheckTask, QaSession
from backend.schemas import (
    OkResponse,
    QaAskRequest,
    QaAskResponseOut,
    QaQuoteOut,
    QaSessionCreateRequest,
    QaSessionOut,
)
from backend.worker import resolve_effective_llm_client, resolve_project_path
from core.bid_react_agent import ask_bid_expert, ask_bid_expert_stream

router = APIRouter(prefix='/api/qa', tags=['qa'])


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _session_to_out(s: QaSession) -> QaSessionOut:
    return QaSessionOut(
        id=s.id,
        tenant_id=s.tenant_id,
        user_id=s.user_id,
        task_id=s.task_id,
        title=s.title,
        tender_path=s.tender_path,
        bid_path=s.bid_path,
        messages_json=list(s.messages_json or []),
        created_at=s.created_at.isoformat() if s.created_at else None,
        updated_at=s.updated_at.isoformat() if s.updated_at else None,
    )


def _get_tenant_session_or_404(db: Session, session_id: int, tenant_id: int) -> QaSession:
    row = db.query(QaSession).filter_by(id=session_id, tenant_id=tenant_id).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'QA_SESSION_NOT_FOUND', 'message': '问答会话不存在或无权访问'},
        )
    return row


def _extract_session_history(messages_json: Any) -> list[dict[str, Any]]:
    """E21 §2.2：提取会话已存的历史问答列表供 ask_bid_expert / ask_bid_expert_stream 注入上下文。"""
    if not messages_json or not isinstance(messages_json, list):
        return []
    return [dict(m) for m in messages_json if isinstance(m, dict)]


@router.post('/sessions', response_model=QaSessionOut)
@router.post('', response_model=QaSessionOut, include_in_schema=False)
def create_qa_session(
    body: QaSessionCreateRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QaSessionOut:
    """创建新的 F7 标书专家问答会话（可绑定已有 task_id 自动继承双书路径）。"""
    tenant_id = int(current_user.tenant_id)
    tender_path = body.tender_path
    bid_path = body.bid_path
    title = (body.title or '').strip() or '标书专家追问会话'

    if body.task_id is not None:
        task = db.query(BidCheckTask).filter_by(id=body.task_id, tenant_id=tenant_id).first()
        if task is None:
            raise HTTPException(
                status_code=404,
                detail={'code': 'TASK_NOT_FOUND', 'message': '关联任务不存在或无权访问'},
            )
        tender_path = tender_path or task.tender_path
        bid_path = bid_path or task.bid_path
        if not body.title:
            title = f"{task.title} · 专家追问"

    qa_sess = QaSession(
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        task_id=body.task_id,
        title=title,
        tender_path=tender_path,
        bid_path=bid_path,
        messages_json=[],
    )
    db.add(qa_sess)
    db.commit()
    db.refresh(qa_sess)
    return _session_to_out(qa_sess)


@router.get('/sessions', response_model=list[QaSessionOut])
@router.get('', response_model=list[QaSessionOut], include_in_schema=False)
def list_qa_sessions(
    task_id: Optional[int] = Query(default=None),
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[QaSessionOut]:
    """查询本租户下的问答会话列表。"""
    tenant_id = int(current_user.tenant_id)
    q = db.query(QaSession).filter_by(tenant_id=tenant_id)
    if task_id is not None:
        q = q.filter_by(task_id=task_id)
    rows = q.order_by(QaSession.id.desc()).all()
    return [_session_to_out(r) for r in rows]


@router.get('/sessions/{session_id}', response_model=QaSessionOut)
@router.get('/{session_id}', response_model=QaSessionOut, include_in_schema=False)
def get_qa_session(
    session_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QaSessionOut:
    """获取单条问答会话完整历史消息。"""
    tenant_id = int(current_user.tenant_id)
    row = _get_tenant_session_or_404(db, session_id, tenant_id)
    return _session_to_out(row)


@router.delete('/sessions/{session_id}', response_model=OkResponse)
@router.delete('/{session_id}', response_model=OkResponse, include_in_schema=False)
def delete_qa_session(
    session_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OkResponse:
    """删除问答会话。"""
    tenant_id = int(current_user.tenant_id)
    row = _get_tenant_session_or_404(db, session_id, tenant_id)
    db.delete(row)
    db.commit()
    return OkResponse(ok=True, message='会话已删除')


@router.post('/{session_id}/ask', response_model=QaAskResponseOut)
@router.post('/sessions/{session_id}/ask', response_model=QaAskResponseOut, include_in_schema=False)
def ask_in_session(
    session_id: int,
    body: QaAskRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QaAskResponseOut:
    """在指定会话中调用 F7 ask_bid_expert 提问（自动带入会话多轮历史），并将整轮 Q&A 追加进 messages_json。"""
    tenant_id = int(current_user.tenant_id)
    qa_sess = _get_tenant_session_or_404(db, session_id, tenant_id)

    tender_rel = qa_sess.tender_path
    bid_rel = qa_sess.bid_path
    if (not tender_rel or not bid_rel) and qa_sess.task_id is not None:
        task = db.query(BidCheckTask).filter_by(id=qa_sess.task_id, tenant_id=tenant_id).first()
        if task is not None:
            tender_rel = tender_rel or task.tender_path
            bid_rel = bid_rel or task.bid_path

    tender_abs = resolve_project_path(tender_rel)
    bid_abs = resolve_project_path(bid_rel)
    if not tender_abs.is_file() or not bid_abs.is_file():
        raise HTTPException(
            status_code=422,
            detail={'code': 'QA_FILES_MISSING', 'message': '会话关联的招投标双书文件不存在'},
        )

    prior_history = _extract_session_history(qa_sess.messages_json)
    client, _ = resolve_effective_llm_client(db, current_user.get('id'))
    raw_effort = (body.reasoning_effort or '').strip().lower()
    valid_effort = raw_effort if raw_effort in ('low', 'medium', 'high') else None
    try:
        res = ask_bid_expert(
            tender_path=tender_abs,
            bid_path=bid_abs,
            question=body.question.strip(),
            client=client,
            max_rounds=body.max_rounds,
            reasoning_effort=valid_effort,
            history=prior_history,
        )
    except Exception:
        res = ask_bid_expert(
            tender_path=tender_abs,
            bid_path=bid_abs,
            question=body.question.strip(),
            client=None,
            max_rounds=body.max_rounds,
            reasoning_effort=valid_effort,
            history=prior_history,
        )

    quotes_out = [
        QaQuoteOut(
            source=str(q.get('source') or 'tender'),
            quote=str(q.get('quote') or ''),
            offset=q.get('offset'),
            page=q.get('page'),
            quote_verified=bool(q.get('quote_verified', True)),
            note=q.get('note'),
        )
        for q in (res.get('quotes') or [])
    ]

    history = list(qa_sess.messages_json or [])
    history.append({'role': 'user', 'content': body.question.strip(), 'quotes': [], 'steps': []})
    history.append({
        'role': 'assistant',
        'content': str(res.get('answer') or ''),
        'mode': str(res.get('mode') or 'keyword_fallback'),
        'quotes': [q.model_dump() for q in quotes_out],
        'steps': list(res.get('steps') or []),
    })
    qa_sess.messages_json = history
    if qa_sess.title in ('新对话', '标书专家追问会话'):
        qa_sess.title = body.question.strip()[:30]
    qa_sess.updated_at = _utc_now()
    db.commit()

    record_audit(
        db,
        action='qa_ask',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"qa_session:{qa_sess.id}",
        detail_json={'mode': res.get('mode'), 'quotes_count': len(quotes_out)},
    )

    return QaAskResponseOut(
        session_id=qa_sess.id,
        mode=str(res.get('mode') or 'keyword_fallback'),
        question=body.question.strip(),
        answer=str(res.get('answer') or ''),
        quotes=quotes_out,
        steps=list(res.get('steps') or []),
        rounds_used=int(res.get('rounds_used') or 1),
        terminated_by_max_rounds=bool(res.get('terminated_by_max_rounds', False)),
    )


@router.post('/{session_id}/ask/stream')
@router.post('/sessions/{session_id}/ask/stream', include_in_schema=False)
def ask_in_session_stream(
    session_id: int,
    body: QaAskRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """E21 §2.2：SSE 流式问答端点（推送 event: step / answer_delta / done / error，在 done 帧原子落库更新会话）。"""
    tenant_id = int(current_user.tenant_id)
    user_id = current_user.get('id')
    qa_sess = _get_tenant_session_or_404(db, session_id, tenant_id)

    tender_rel = qa_sess.tender_path
    bid_rel = qa_sess.bid_path
    if (not tender_rel or not bid_rel) and qa_sess.task_id is not None:
        task = db.query(BidCheckTask).filter_by(id=qa_sess.task_id, tenant_id=tenant_id).first()
        if task is not None:
            tender_rel = tender_rel or task.tender_path
            bid_rel = bid_rel or task.bid_path

    tender_abs = resolve_project_path(tender_rel)
    bid_abs = resolve_project_path(bid_rel)
    if not tender_abs.is_file() or not bid_abs.is_file():
        raise HTTPException(
            status_code=422,
            detail={'code': 'QA_FILES_MISSING', 'message': '会话关联的招投标双书文件不存在'},
        )

    prior_history = _extract_session_history(qa_sess.messages_json)
    client, _ = resolve_effective_llm_client(db, user_id)
    raw_effort = (body.reasoning_effort or '').strip().lower()
    valid_effort = raw_effort if raw_effort in ('low', 'medium', 'high') else None
    clean_question = body.question.strip()
    max_rounds = body.max_rounds
    # 绑定当前请求所属 Engine 工厂（防止 create_app 替换 db_mod.SessionLocal 后模块顶层旧绑定失效）
    db_bind = db.get_bind()
    write_session_factory = (
        db_mod.get_session_factory(db_bind) if db_bind is not None else db_mod.SessionLocal
    )

    def _sse_stream_gen():
        try:
            stream_iter = ask_bid_expert_stream(
                tender_path=tender_abs,
                bid_path=bid_abs,
                question=clean_question,
                client=client,
                max_rounds=max_rounds,
                reasoning_effort=valid_effort,
                history=prior_history,
            )
            for ev in stream_iter:
                ev_type = str(ev.get('type') or 'step')
                ev_data = ev.get('data') or {}

                if ev_type == 'done':
                    quotes_out = [
                        QaQuoteOut(
                            source=str(q.get('source') or 'tender'),
                            quote=str(q.get('quote') or ''),
                            offset=q.get('offset'),
                            page=q.get('page'),
                            quote_verified=bool(q.get('quote_verified', True)),
                            note=q.get('note'),
                        )
                        for q in (ev_data.get('quotes') or [])
                    ]
                    done_payload = {
                        'session_id': session_id,
                        'mode': str(ev_data.get('mode') or 'keyword_fallback'),
                        'question': clean_question,
                        'answer': str(ev_data.get('answer') or ''),
                        'quotes': [q.model_dump() for q in quotes_out],
                        'steps': list(ev_data.get('steps') or []),
                        'rounds_used': int(ev_data.get('rounds_used') or 1),
                        'terminated_by_max_rounds': bool(ev_data.get('terminated_by_max_rounds', False)),
                    }

                    write_sess = write_session_factory()
                    try:
                        row = write_sess.query(QaSession).filter_by(id=session_id, tenant_id=tenant_id).first()
                        if row is not None:
                            new_hist = list(row.messages_json or [])
                            new_hist.append({'role': 'user', 'content': clean_question, 'quotes': [], 'steps': []})
                            new_hist.append({
                                'role': 'assistant',
                                'content': done_payload['answer'],
                                'mode': done_payload['mode'],
                                'quotes': done_payload['quotes'],
                                'steps': done_payload['steps'],
                            })
                            row.messages_json = new_hist
                            if row.title in ('新对话', '标书专家追问会话'):
                                row.title = clean_question[:30]
                            row.updated_at = _utc_now()
                            write_sess.commit()
                        record_audit(
                            write_sess,
                            action='qa_ask',
                            tenant_id=tenant_id,
                            user_id=user_id,
                            target=f"qa_session:{session_id}",
                            detail_json={'mode': done_payload['mode'], 'quotes_count': len(quotes_out), 'stream': True},
                        )
                    finally:
                        write_sess.close()

                    yield f"event: done\ndata: {json.dumps(done_payload, ensure_ascii=False)}\n\n"
                else:
                    yield f"event: {ev_type}\ndata: {json.dumps(ev_data, ensure_ascii=False)}\n\n"
        except Exception as exc:
            err_payload = {'detail': f"{type(exc).__name__}: {exc}"}
            yield f"event: error\ndata: {json.dumps(err_payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _sse_stream_gen(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        },
    )
