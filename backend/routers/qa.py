# -*- coding: utf-8 -*-
"""B2 / E21 / E24 · 第 7 组路由：/api/qa（会话 CRUD + 附件上传/删除与配额管控 + 同步/流式多模态追问）。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import threading
from typing import Any, Optional
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit
import backend.database as db_mod
from backend.database import PROJECT_ROOT, get_db
from backend.models import BidCheckTask, BidFinding, QaSession, ensure_qa_sessions_columns
from backend.schemas import (
    OkResponse,
    QaAskRequest,
    QaAskResponseOut,
    QaAttachmentOut,
    QaLastTurnWithdrawOut,
    QaQuoteOut,
    QaSessionCreateRequest,
    QaSessionOut,
    QaSessionRenameRequest,
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

# E28 §2.1：SQLite/多线程并发流落库串行互斥锁（仅保护短事务读写提交，绝不包住耗时的 OCR/LLM 调用）
_QA_DB_WRITE_LOCK = threading.Lock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


_GATEWAY_ERR_MODULES = ('openai', 'httpx', 'httpcore', 'urllib', 'requests', 'ssl')
_GATEWAY_ERR_CLASSES = frozenset({
    'APITimeoutError', 'APIConnectionError', 'APIStatusError', 'RateLimitError',
    'ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout', 'RemoteProtocolError',
    'TimeoutError', 'ConnectionError', 'ConnectionResetError', 'ConnectionRefusedError',
})
_TOOL_INTERNAL_ERR_CLASSES = frozenset({
    'InvalidOperation', 'ConversionSyntax', 'ValueError', 'TypeError',
    'KeyError', 'IndexError', 'AttributeError', 'ZeroDivisionError',
})
_GATEWAY_ERR_KEYWORDS = (
    'timeout', 'timed out', 'connection', 'connect ', 'tun', 'proxy',
    'unreachable', 'refused', 'ssl', 'tls', 'bad gateway',
    '502', '503', '504', '429', 'rate limit', 'unauthorized', 'api key',
    '网关', '超时', '连接', '代理',
)


def _classify_qa_exception(exc: Exception) -> str:
    """E31：区分网关连接/超时错误('gateway')与工具计算/内部代码异常('tool_or_internal')。"""
    cls_name = type(exc).__name__
    mod_name = (type(exc).__module__ or '').lower()
    if any(mod_name == m or mod_name.startswith(f"{m}.") for m in _GATEWAY_ERR_MODULES):
        return 'gateway'
    if cls_name in _GATEWAY_ERR_CLASSES or (isinstance(exc, (TimeoutError, ConnectionError)) and not isinstance(exc, FileNotFoundError)):
        return 'gateway'
    if cls_name in _TOOL_INTERNAL_ERR_CLASSES:
        return 'tool_or_internal'
    msg_cf = str(exc or '').lower()
    if any(kw in msg_cf for kw in _GATEWAY_ERR_KEYWORDS):
        return 'gateway'
    return 'tool_or_internal'


def _is_default_session_title(title: Optional[str]) -> bool:
    t = (title or '').strip()
    if not t:
        return True
    if t in ('新对话', '标书专家追问会话'):
        return True
    if t.endswith(' · 专家追问') or t.endswith('·专家追问'):
        return True
    return False


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


_DEFAULT_SUGGESTED_QUESTIONS = (
    '招标文件要求的免费质保期是多久？我方投标书在哪响应的？',
    '评分细则表里关于业绩和证书的加分项有哪些？',
    '本项目最高投标限价是多少？我方总报价是多少？',
    '招标文件中带 ★ 号的强制性技术要求有哪些？',
    '分“资格废标门槛”和“评分加分项”列出招标文件要求提供的全部企业证照、产品认证及人员证书并标明出处页码。',
)


def _build_suggested_questions(
    db: Optional[Session],
    task: Optional[BidCheckTask],
    attachments: list[dict[str, Any]],
) -> list[str]:
    """E25 §2.3：0-Token 纯确定性动态生成 4~5 条上下文快捷追问。"""
    questions: list[str] = []

    if attachments:
        att_names = '、'.join(str(a.get('filename') or '') for a in attachments[:2] if a.get('filename'))
        has_img = any(bool(a.get('is_image')) for a in attachments)
        if has_img:
            questions.append(
                f"请结合已上传的图片/附件（{att_names or '补充材料'}），核验其中的公章、证书编号或有效期是否满足招标要求？"
            )
        else:
            questions.append(
                f"请结合已上传的补充附件（{att_names or '补遗文件'}），交叉核验我方投标书是否存在未响应或前后不一致之处？"
            )

    if task is not None:
        stats = task.stats_json or {}
        cfg = task.config_json or {}
        findings: list[BidFinding] = []
        if db is not None:
            try:
                findings = db.query(BidFinding).filter_by(task_id=task.id).all()
            except Exception:
                findings = []

        has_f2_issue = (
            stats.get('price_status') == 'NEED_MANUAL'
            or any(f.source_module == 'F2' for f in findings)
        )
        has_f4_star_issue = (
            int(stats.get('f1_extracted_count') or stats.get('total_requirements') or 0) > 0
            or any(f.source_module == 'F4' for f in findings)
        )
        has_f3_or_date_issue = (
            not cfg.get('open_date')
            or any(f.source_module == 'F3' for f in findings)
        )

        if has_f2_issue:
            questions.append('本项目招标最高投标限价是多少？我方开标一览表总报价与分项报价合计是否完全一致？')
        if has_f4_star_issue:
            questions.append('招标文件中带 ★ 号的实质性要求有哪些？我方投标书逐条在哪些页码作出了响应？')
        if has_f3_or_date_issue:
            questions.append('招标文件规定的开标时间（投标截止时间）和社保证明覆盖月份要求是什么？我方证照是否全部在有效期内？')

    # 内置投前备证兜底提问（为 E26 确定性证照清单与台账联动卡先行过渡）
    cert_checklist_q = '分“资格废标门槛”和“评分加分项”列出招标文件要求提供的全部企业证照、产品认证及人员证书并标明出处页码。'
    if cert_checklist_q not in questions:
        questions.append(cert_checklist_q)

    for dq in _DEFAULT_SUGGESTED_QUESTIONS:
        if len(questions) >= 5:
            break
        if dq not in questions:
            questions.append(dq)

    return questions[:5]


def _session_to_out(s: QaSession, db: Optional[Session] = None) -> QaSessionOut:
    raw_atts = [a for a in (getattr(s, 'attachments_json', None) or []) if isinstance(a, dict)]
    atts_out = [_att_dict_to_out(a) for a in raw_atts]
    bound_task: Optional[BidCheckTask] = getattr(s, 'task', None)
    if bound_task is None and s.task_id is not None and db is not None:
        try:
            bound_task = db.query(BidCheckTask).filter_by(id=s.task_id, tenant_id=s.tenant_id).first()
        except Exception:
            bound_task = None
    suggested = _build_suggested_questions(db, bound_task, raw_atts)
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
        suggested_questions=suggested,
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

    with _QA_DB_WRITE_LOCK:
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
        return _session_to_out(qa_sess, db)


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
    return [_session_to_out(r, db) for r in rows]


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
    return _session_to_out(row, db)


@router.patch('/sessions/{session_id}', response_model=QaSessionOut)
@router.patch('/{session_id}', response_model=QaSessionOut, include_in_schema=False)
def rename_qa_session(
    session_id: int,
    body: QaSessionRenameRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QaSessionOut:
    """E28 §2.4：重命名问答会话标题（多租户隔离 + 写锁串行化 + 审计留痕）。"""
    new_title = (body.title or '').strip()
    if not new_title:
        raise HTTPException(
            status_code=422,
            detail={'code': 'EMPTY_SESSION_TITLE', 'message': '会话标题不能为空'},
        )
    tenant_id = int(current_user.tenant_id)
    with _QA_DB_WRITE_LOCK:
        db.rollback()
        db.expire_all()
        row = _get_tenant_session_or_404(db, session_id, tenant_id)
        old_title = row.title
        row.title = new_title[:256]
        row.updated_at = _utc_now()
        db.commit()
        db.refresh(row)
        record_audit(
            db,
            action='qa_rename_session',
            tenant_id=tenant_id,
            user_id=current_user.get('id'),
            target=f"qa_session:{session_id}",
            detail_json={'old_title': old_title, 'new_title': row.title},
        )
        return _session_to_out(row, db)


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

    with _QA_DB_WRITE_LOCK:
        db.rollback()
        db.expire_all()
        fresh_sess = _get_tenant_session_or_404(db, session_id, tenant_id)
        latest_atts = [a for a in (getattr(fresh_sess, 'attachments_json', None) or []) if isinstance(a, dict)]
        if len(latest_atts) >= _QA_MAX_SESSION_FILES or (
            sum(int(a.get('size_bytes') or 0) for a in latest_atts) + size_bytes > _QA_MAX_SESSION_TOTAL_BYTES
        ):
            target_file.unlink(missing_ok=True)
            for sfx in ('.ocr.txt', '.ocr.json', '.media.ocr.json'):
                Path(str(target_file) + sfx).unlink(missing_ok=True)
            raise HTTPException(
                status_code=422,
                detail={'code': 'ATTACHMENT_LIMIT_EXCEEDED', 'message': '会话附件配额已满，请先清理部分附件'},
            )
        latest_atts.append(att_record)
        fresh_sess.attachments_json = list(latest_atts)
        fresh_sess.updated_at = _utc_now()
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
    with _QA_DB_WRITE_LOCK:
        db.rollback()
        db.expire_all()
        fresh_sess = _get_tenant_session_or_404(db, session_id, tenant_id)
        latest_atts = [a for a in (getattr(fresh_sess, 'attachments_json', None) or []) if isinstance(a, dict)]
        target_meta = next((a for a in latest_atts if a.get('attachment_id') == attachment_id), None)
        if target_meta is None:
            raise HTTPException(
                status_code=404,
                detail={'code': 'ATTACHMENT_NOT_FOUND', 'message': '未找到指定的会话附件'},
            )
        rel_p = target_meta.get('rel_path')
        fresh_sess.attachments_json = [a for a in latest_atts if a.get('attachment_id') != attachment_id]
        fresh_sess.updated_at = _utc_now()
        db.commit()

    if rel_p:
        abs_p = resolve_project_path(rel_p)
        if abs_p.is_relative_to(_QA_UPLOADS_ROOT):
            abs_p.unlink(missing_ok=True)
            for sfx in ('.ocr.txt', '.ocr.json', '.media.ocr.json'):
                Path(str(abs_p) + sfx).unlink(missing_ok=True)
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
    with _QA_DB_WRITE_LOCK:
        db.rollback()
        db.expire_all()
        row = _get_tenant_session_or_404(db, session_id, tenant_id)
        db.delete(row)
        db.commit()

    sess_dir = (_QA_UPLOADS_ROOT / str(tenant_id) / str(session_id)).resolve()
    if sess_dir.is_dir() and sess_dir.is_relative_to(_QA_UPLOADS_ROOT):
        shutil.rmtree(sess_dir, ignore_errors=True)

    return OkResponse(ok=True, message='会话已删除')


def _build_qa_quotes_out(raw_quotes: Optional[list[dict[str, Any]]]) -> list[QaQuoteOut]:
    return [
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
        for q in (raw_quotes or [])
    ]


def _persist_qa_turn_locked(
    db_or_factory: Any,
    *,
    is_factory: bool,
    session_id: int,
    tenant_id: int,
    user_id: Optional[int],
    question: str,
    bubble_atts: list[dict[str, Any]],
    answer: str,
    mode: str,
    quotes_payload: list[dict[str, Any]],
    steps_payload: list[dict[str, Any]],
    attachments_count: int,
    is_stream: bool,
) -> None:
    """E28 §2.1：串行持有写锁并重载最新消息历史追加整轮 Q&A，防 SQLite 并发落库 lock 与脏覆盖。"""
    with _QA_DB_WRITE_LOCK:
        write_sess: Session = db_or_factory() if is_factory else db_or_factory
        try:
            if not is_factory:
                write_sess.rollback()
                write_sess.expire_all()
            row = write_sess.query(QaSession).filter_by(id=session_id, tenant_id=tenant_id).first()
            if row is not None:
                new_hist = list(row.messages_json or [])
                new_hist.append({
                    'role': 'user',
                    'content': question,
                    'attachment_ids': [a['attachment_id'] for a in bubble_atts],
                    'attachments': bubble_atts,
                    'quotes': [],
                    'steps': [],
                })
                new_hist.append({
                    'role': 'assistant',
                    'content': answer,
                    'mode': mode,
                    'quotes': quotes_payload,
                    'steps': steps_payload,
                })
                row.messages_json = new_hist
                clean_q = (question or '').strip()
                if _is_default_session_title(row.title) and clean_q:
                    row.title = clean_q[:30]
                row.updated_at = _utc_now()
                write_sess.commit()

            detail_json: dict[str, Any] = {
                'mode': mode,
                'quotes_count': len(quotes_payload),
                'attachments_count': attachments_count,
            }
            if is_stream:
                detail_json['stream'] = True
            record_audit(
                write_sess,
                action='qa_ask',
                tenant_id=tenant_id,
                user_id=user_id,
                target=f"qa_session:{session_id}",
                detail_json=detail_json,
            )
        finally:
            if is_factory:
                write_sess.close()


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
    except Exception as exc:
        err_type = _classify_qa_exception(exc)
        if client is not None:
            if err_type == 'gateway':
                raise HTTPException(
                    status_code=502,
                    detail={
                        'code': 'QA_GATEWAY_ERROR',
                        'error_type': 'gateway',
                        'message': (
                            f"模型网关调用异常或超时（{type(exc).__name__}: {exc}），"
                            f"已避免降级覆盖为 0-Token 原始碎片；请排查 TUN 代理/网关连通性后重试或切换 0-Token 模式"
                        ),
                    },
                )
            raise HTTPException(
                status_code=500,
                detail={
                    'code': 'QA_EXECUTION_ERROR',
                    'error_type': 'tool_or_internal',
                    'message': (
                        f"问答工具或内部解析执行异常（{type(exc).__name__}: {exc}），"
                        f"非网关超时；请核对提问格式或联系管理员排查"
                    ),
                },
            )
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

    quotes_out = _build_qa_quotes_out(res.get('quotes'))
    clean_question = body.question.strip()
    answer_text = str(res.get('answer') or '')
    mode_str = str(res.get('mode') or 'keyword_fallback')
    steps_list = list(res.get('steps') or [])

    _persist_qa_turn_locked(
        db,
        is_factory=False,
        session_id=qa_sess.id,
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        question=clean_question,
        bubble_atts=bubble_atts,
        answer=answer_text,
        mode=mode_str,
        quotes_payload=[q.model_dump() for q in quotes_out],
        steps_payload=steps_list,
        attachments_count=len(runtime_atts),
        is_stream=False,
    )

    return QaAskResponseOut(
        session_id=qa_sess.id,
        mode=mode_str,
        question=clean_question,
        answer=answer_text,
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
    db.rollback()

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
                    quotes_out = _build_qa_quotes_out(ev_data.get('quotes'))
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

                    _persist_qa_turn_locked(
                        write_session_factory,
                        is_factory=True,
                        session_id=session_id,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        question=clean_question,
                        bubble_atts=bubble_atts,
                        answer=done_payload['answer'],
                        mode=done_payload['mode'],
                        quotes_payload=done_payload['quotes'],
                        steps_payload=done_payload['steps'],
                        attachments_count=len(runtime_atts),
                        is_stream=True,
                    )

                    yield f"event: done\ndata: {json.dumps(done_payload, ensure_ascii=False)}\n\n"
                else:
                    yield f"event: {ev_type}\ndata: {json.dumps(ev_data, ensure_ascii=False)}\n\n"
        except Exception as exc:
            err_payload = {
                'detail': f"{type(exc).__name__}: {exc}",
                'error_type': _classify_qa_exception(exc),
            }
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


@router.delete('/{session_id}/last-turn', response_model=QaLastTurnWithdrawOut)
@router.delete('/sessions/{session_id}/last-turn', response_model=QaLastTurnWithdrawOut, include_in_schema=False)
def withdraw_last_turn_in_session(
    session_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QaLastTurnWithdrawOut:
    """E27 §2.2.4：撤回会话最后一轮问答（移除末尾 assistant 与配对 user 消息，返回被撤回的问题文本以便前端回填或重发）。"""
    tenant_id = int(current_user.tenant_id)
    with _QA_DB_WRITE_LOCK:
        db.rollback()
        db.expire_all()
        qa_sess = _get_tenant_session_or_404(db, session_id, tenant_id)
        history = list(qa_sess.messages_json or [])

        withdrawn_q = ''
        withdrawn_att_ids: list[str] = []

        if history and str(history[-1].get('role') or '') == 'assistant':
            history.pop()
        if history and str(history[-1].get('role') or '') == 'user':
            last_user = history.pop()
            withdrawn_q = str(last_user.get('content') or '')
            withdrawn_att_ids = [str(a) for a in (last_user.get('attachment_ids') or []) if a]

        qa_sess.messages_json = history
        qa_sess.updated_at = _utc_now()
        db.commit()

        record_audit(
            db,
            action='qa_withdraw_last_turn',
            tenant_id=tenant_id,
            user_id=current_user.get('id'),
            target=f"qa_session:{qa_sess.id}",
            detail_json={
                'withdrawn_question': withdrawn_q[:80],
                'remaining_messages': len(history),
            },
        )

    return QaLastTurnWithdrawOut(
        session_id=qa_sess.id,
        withdrawn_question=withdrawn_q,
        withdrawn_attachment_ids=withdrawn_att_ids,
        remaining_messages=len(history),
    )
