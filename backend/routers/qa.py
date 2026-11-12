# -*- coding: utf-8 -*-
"""B2 / E21 / E24 · 第 7 组路由：/api/qa（会话 CRUD + 附件上传/删除与配额管控 + 同步/流式多模态追问）。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from typing import Any, Optional
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit
import backend.database as db_mod
from backend.database import PROJECT_ROOT, get_db
from backend.models import BidCheckTask, QaSession, ensure_qa_sessions_columns
from backend.schemas import (
    OkResponse,
    QaAskRequest,
    QaAskResponseOut,
    QaAttachmentOut,
    QaQuoteOut,
    QaSessionCreateRequest,
    QaSessionOut,
)
from backend.worker import resolve_effective_llm_client, resolve_project_path
from core.bid_react_agent import ask_bid_expert, ask_bid_expert_stream
from core.parser import parse_qa_attachment

router = APIRouter(prefix='/api/qa', tags=['qa'])

# E24 §2.3：附件类型白名单与四级配额常量
_QA_ALLOWED_EXTS = {'.docx', '.pdf', '.txt', '.md', '.png', '.jpg', '.jpeg'}
_QA_IMAGE_EXTS = {'.png', '.jpg', '.jpeg'}
_QA_MAX_SINGLE_FILE_BYTES = 20 * 1024 * 1024       # 单文件上限 20 MB
_QA_MAX_SESSION_FILES = 10                         # 单会话附件个数上限 10 个
_QA_MAX_SESSION_TOTAL_BYTES = 100 * 1024 * 1024    # 单会话累计体积上限 100 MB
_QA_MAX_IMAGES_PER_TURN = 4                        # 单轮追问图片上限 4 张
_QA_UPLOADS_ROOT = (PROJECT_ROOT / 'data' / 'uploads' / 'qa').resolve()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _att_dict_to_out(a: dict[str, Any]) -> QaAttachmentOut:
    return QaAttachmentOut(
        attachment_id=str(a.get('attachment_id') or ''),
        filename=str(a.get('filename') or ''),
        ext=str(a.get('ext') or ''),
        size_bytes=int(a.get('size_bytes') or 0),
        is_image=bool(a.get('is_image', False)),
        ocr_chars=int(a.get('ocr_chars') or 0),
        has_text=bool(a.get('has_text', False)),
        uploaded_at=a.get('uploaded_at'),
    )


def _session_to_out(s: QaSession) -> QaSessionOut:
    raw_atts = getattr(s, 'attachments_json', None) or []
    atts_out = [_att_dict_to_out(a) for a in raw_atts if isinstance(a, dict)]
    return QaSessionOut(
        id=s.id,
        tenant_id=s.tenant_id,
        user_id=s.user_id,
        task_id=s.task_id,
        title=s.title,
        tender_path=s.tender_path,
        bid_path=s.bid_path,
        messages_json=list(s.messages_json or []),
        attachments=atts_out,
        created_at=s.created_at.isoformat() if s.created_at else None,
        updated_at=s.updated_at.isoformat() if s.updated_at else None,
    )


def _get_tenant_session_or_404(db: Session, session_id: int, tenant_id: int) -> QaSession:
    ensure_qa_sessions_columns(db.get_bind())
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


def _resolve_turn_attachments(
    qa_sess: QaSession,
    requested_ids: Optional[list[str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """E24 §2.3：校验并加载本轮追问携带的附件列表。

    - 若 requested_ids 含不在本会话 attachments_json 中的 ID -> HTTP 422 INVALID_ATTACHMENT_ID；
    - 若选中的图片附件数量 > 4 -> HTTP 422 TOO_MANY_IMAGES_PER_TURN；
    - 返回 (runtime_attachments_for_agent, summary_attachments_for_message_bubble)。
    """
    if not requested_ids:
        return [], []

    stored_list = [a for a in (getattr(qa_sess, 'attachments_json', None) or []) if isinstance(a, dict)]
    by_id = {str(a.get('attachment_id') or ''): a for a in stored_list if a.get('attachment_id')}

    selected_meta: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_id in requested_ids:
        aid = str(raw_id or '').strip()
        if not aid or aid in seen:
            continue
        if aid not in by_id:
            raise HTTPException(
                status_code=422,
                detail={'code': 'INVALID_ATTACHMENT_ID', 'message': f"附件不存在或不属于当前会话: {aid}"},
            )
        seen.add(aid)
        selected_meta.append(by_id[aid])

    img_count = sum(1 for a in selected_meta if a.get('is_image'))
    if img_count > _QA_MAX_IMAGES_PER_TURN:
        raise HTTPException(
            status_code=422,
            detail={
                'code': 'TOO_MANY_IMAGES_PER_TURN',
                'message': f"单轮追问最多允许附带 {_QA_MAX_IMAGES_PER_TURN} 张图片（当前选择了 {img_count} 张）",
            },
        )

    runtime_atts: list[dict[str, Any]] = []
    bubble_summaries: list[dict[str, Any]] = []
    for meta in selected_meta:
        abs_p = resolve_project_path(meta.get('rel_path'))
        if not abs_p.is_file():
            raise HTTPException(
                status_code=422,
                detail={'code': 'ATTACHMENT_FILE_MISSING', 'message': f"附件物理文件缺失: {meta.get('filename')}"},
            )
        parsed = parse_qa_attachment(abs_p)
        runtime_atts.append({
            'attachment_id': meta['attachment_id'],
            'filename': meta['filename'],
            'ext': meta.get('ext', ''),
            'file_path': str(abs_p),
            'text': parsed['text'],
            'is_image': bool(parsed['is_image']),
            'ocr_chars': int(parsed['ocr_chars']),
            'has_text': bool(parsed['has_text']),
        })
        bubble_summaries.append({
            'attachment_id': meta['attachment_id'],
            'filename': meta['filename'],
            'is_image': bool(meta.get('is_image', False)),
            'ocr_chars': int(meta.get('ocr_chars', 0)),
        })

    return runtime_atts, bubble_summaries


@router.post('/sessions', response_model=QaSessionOut)
@router.post('', response_model=QaSessionOut, include_in_schema=False)
def create_qa_session(
    body: QaSessionCreateRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QaSessionOut:
    """创建新的 F7 标书专家问答会话（可绑定已有 task_id 自动继承双书路径）。"""
    ensure_qa_sessions_columns(db.get_bind())
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
        attachments_json=[],
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
    ensure_qa_sessions_columns(db.get_bind())
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
    """获取单条问答会话完整历史消息与已上传附件清单。"""
    tenant_id = int(current_user.tenant_id)
    row = _get_tenant_session_or_404(db, session_id, tenant_id)
    return _session_to_out(row)


@router.post('/sessions/{session_id}/attachments', response_model=QaAttachmentOut)
@router.post('/{session_id}/attachments', response_model=QaAttachmentOut, include_in_schema=False)
def upload_qa_attachment(
    session_id: int,
    file: UploadFile = File(...),
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QaAttachmentOut:
    """E24 §2.3：向指定问答会话上传补充文档或图片附件（校验类型、20MB单文件上限、10个/100MB会话总配额）。"""
    tenant_id = int(current_user.tenant_id)
    qa_sess = _get_tenant_session_or_404(db, session_id, tenant_id)

    orig_name = Path(file.filename or 'attachment.txt').name
    ext = Path(orig_name).suffix.lower()
    if ext not in _QA_ALLOWED_EXTS:
        raise HTTPException(
            status_code=422,
            detail={
                'code': 'UNSUPPORTED_FILE_TYPE',
                'message': f"不支持的附件格式: {ext}，仅支持 docx/pdf/txt/md/png/jpg/jpeg",
            },
        )

    content = file.file.read()
    size_bytes = len(content)
    if size_bytes == 0:
        raise HTTPException(
            status_code=422,
            detail={'code': 'EMPTY_FILE', 'message': f"上传的附件「{orig_name}」内容为空"},
        )
    if size_bytes > _QA_MAX_SINGLE_FILE_BYTES:
        raise HTTPException(
            status_code=422,
            detail={'code': 'FILE_TOO_LARGE', 'message': f"附件「{orig_name}」超过 20MB 单文件上限"},
        )

    existing_atts = [a for a in (getattr(qa_sess, 'attachments_json', None) or []) if isinstance(a, dict)]
    if len(existing_atts) >= _QA_MAX_SESSION_FILES:
        raise HTTPException(
            status_code=422,
            detail={
                'code': 'ATTACHMENT_LIMIT_EXCEEDED',
                'message': f"单会话最多允许上传 {_QA_MAX_SESSION_FILES} 个附件，请先删除旧附件",
            },
        )
    current_total_bytes = sum(int(a.get('size_bytes') or 0) for a in existing_atts)
    if current_total_bytes + size_bytes > _QA_MAX_SESSION_TOTAL_BYTES:
        raise HTTPException(
            status_code=422,
            detail={
                'code': 'ATTACHMENT_LIMIT_EXCEEDED',
                'message': '单会话附件累计存储体积超过 100MB 上限，请先清理部分附件',
            },
        )

    att_id = f"att_{uuid.uuid4().hex[:12]}"
    sess_dir = (_QA_UPLOADS_ROOT / str(tenant_id) / str(session_id)).resolve()
    if not sess_dir.is_relative_to(_QA_UPLOADS_ROOT):
        raise HTTPException(
            status_code=422,
            detail={'code': 'PATH_TRAVERSAL', 'message': '非法存储目录路径'},
        )
    sess_dir.mkdir(parents=True, exist_ok=True)

    target_file = (sess_dir / f"{att_id}{ext}").resolve()
    if not target_file.is_relative_to(_QA_UPLOADS_ROOT):
        raise HTTPException(
            status_code=422,
            detail={'code': 'PATH_TRAVERSAL', 'message': '非法附件目标路径'},
        )
    target_file.write_bytes(content)

    try:
        parsed = parse_qa_attachment(target_file)
    except Exception as exc:
        target_file.unlink(missing_ok=True)
        for sfx in ('.ocr.txt', '.ocr.json', '.media.ocr.json'):
            Path(str(target_file) + sfx).unlink(missing_ok=True)
        raise HTTPException(
            status_code=422,
            detail={'code': 'ATTACHMENT_PARSE_FAILED', 'message': f"附件解析失败：{exc}"},
        ) from exc

    rel_path = target_file.relative_to(PROJECT_ROOT.resolve()).as_posix()
    att_record = {
        'attachment_id': att_id,
        'filename': orig_name,
        'ext': ext,
        'size_bytes': size_bytes,
        'is_image': bool(parsed['is_image']),
        'ocr_chars': int(parsed['ocr_chars']),
        'has_text': bool(parsed['has_text']),
        'rel_path': rel_path,
        'uploaded_at': _utc_now().isoformat(),
    }

    existing_atts.append(att_record)
    qa_sess.attachments_json = list(existing_atts)
    qa_sess.updated_at = _utc_now()
    db.commit()

    record_audit(
        db,
        action='qa_attachment_upload',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"qa_session:{session_id}",
        detail_json={'attachment_id': att_id, 'filename': orig_name, 'size_bytes': size_bytes},
    )
    return _att_dict_to_out(att_record)


@router.delete('/sessions/{session_id}/attachments/{attachment_id}', response_model=OkResponse)
@router.delete('/{session_id}/attachments/{attachment_id}', response_model=OkResponse, include_in_schema=False)
def delete_qa_attachment(
    session_id: int,
    attachment_id: str,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OkResponse:
    """E24 §2.3：删除会话中的单个附件，并同步清理磁盘原文件及伴生 OCR 缓存。"""
    tenant_id = int(current_user.tenant_id)
    qa_sess = _get_tenant_session_or_404(db, session_id, tenant_id)

    existing_atts = [a for a in (getattr(qa_sess, 'attachments_json', None) or []) if isinstance(a, dict)]
    target_meta = next((a for a in existing_atts if a.get('attachment_id') == attachment_id), None)
    if target_meta is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'ATTACHMENT_NOT_FOUND', 'message': '未找到指定的会话附件'},
        )

    rel_p = target_meta.get('rel_path')
    if rel_p:
        abs_p = resolve_project_path(rel_p)
        if abs_p.is_relative_to(_QA_UPLOADS_ROOT):
            abs_p.unlink(missing_ok=True)
            for sfx in ('.ocr.txt', '.ocr.json', '.media.ocr.json'):
                Path(str(abs_p) + sfx).unlink(missing_ok=True)

    qa_sess.attachments_json = [a for a in existing_atts if a.get('attachment_id') != attachment_id]
    qa_sess.updated_at = _utc_now()
    db.commit()
    return OkResponse(ok=True, message='附件已删除')


@router.delete('/sessions/{session_id}', response_model=OkResponse)
@router.delete('/{session_id}', response_model=OkResponse, include_in_schema=False)
def delete_qa_session(
    session_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OkResponse:
    """删除问答会话，并级联清理 data/uploads/qa/{tenant_id}/{session_id}/ 下的全部物理附件与 OCR 缓存。"""
    tenant_id = int(current_user.tenant_id)
    row = _get_tenant_session_or_404(db, session_id, tenant_id)
    db.delete(row)
    db.commit()

    sess_dir = (_QA_UPLOADS_ROOT / str(tenant_id) / str(session_id)).resolve()
    if sess_dir.is_dir() and sess_dir.is_relative_to(_QA_UPLOADS_ROOT):
        shutil.rmtree(sess_dir, ignore_errors=True)

    return OkResponse(ok=True, message='会话已删除')


@router.post('/{session_id}/ask', response_model=QaAskResponseOut)
@router.post('/sessions/{session_id}/ask', response_model=QaAskResponseOut, include_in_schema=False)
def ask_in_session(
    session_id: int,
    body: QaAskRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QaAskResponseOut:
    """在指定会话中调用 F7 ask_bid_expert 提问（自动带入会话多轮历史与本轮勾选的补充附件），并将整轮 Q&A 追加进 messages_json。"""
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

    runtime_atts, bubble_atts = _resolve_turn_attachments(qa_sess, body.attachment_ids)
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
            attachments=runtime_atts,
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
            attachments=runtime_atts,
        )

    quotes_out = [
        QaQuoteOut(
            source=str(q.get('source') or 'tender'),
            quote=str(q.get('quote') or ''),
            offset=q.get('offset'),
            page=q.get('page'),
            quote_verified=bool(q.get('quote_verified', True)),
            note=q.get('note'),
            attachment_id=q.get('attachment_id'),
            attachment_name=q.get('attachment_name'),
        )
        for q in (res.get('quotes') or [])
    ]

    history = list(qa_sess.messages_json or [])
    history.append({
        'role': 'user',
        'content': body.question.strip(),
        'attachment_ids': [a['attachment_id'] for a in bubble_atts],
        'attachments': bubble_atts,
        'quotes': [],
        'steps': [],
    })
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
        detail_json={
            'mode': res.get('mode'),
            'quotes_count': len(quotes_out),
            'attachments_count': len(runtime_atts),
        },
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
    """E21 / E24：SSE 流式问答端点（支持本轮携带附件与多模态图片，推送 step / answer_delta / done / error）。"""
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

    runtime_atts, bubble_atts = _resolve_turn_attachments(qa_sess, body.attachment_ids)
    prior_history = _extract_session_history(qa_sess.messages_json)
    client, _ = resolve_effective_llm_client(db, user_id)
    raw_effort = (body.reasoning_effort or '').strip().lower()
    valid_effort = raw_effort if raw_effort in ('low', 'medium', 'high') else None
    clean_question = body.question.strip()
    max_rounds = body.max_rounds
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
                attachments=runtime_atts,
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
                            attachment_id=q.get('attachment_id'),
                            attachment_name=q.get('attachment_name'),
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
                            new_hist.append({
                                'role': 'user',
                                'content': clean_question,
                                'attachment_ids': [a['attachment_id'] for a in bubble_atts],
                                'attachments': bubble_atts,
                                'quotes': [],
                                'steps': [],
                            })
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
                            detail_json={
                                'mode': done_payload['mode'],
                                'quotes_count': len(quotes_out),
                                'attachments_count': len(runtime_atts),
                                'stream': True,
                            },
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
