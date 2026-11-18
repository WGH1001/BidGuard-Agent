# -*- coding: utf-8 -*-
"""E37 + E38 · 第 13 组路由：/api/composer（写作 Skills 管理/GitHub·Gitee·本地导入 + 异步/AI 动态编制 + 取消/下载/清理）。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit
from backend.database import PROJECT_ROOT, get_db
from backend.models import (
    BidCheckTask,
    BidComposeDraft,
    BidFinding,
    BidRuleConfig,
    CompanyCertificate,
    ComposerSkill,
)
from backend.schemas import (
    ComposerSkillListOut,
    ComposerSkillOut,
    ComposerSkillUpsertRequest,
    ComposeDraftDetailOut,
    ComposeDraftListOut,
    ComposeDraftSummaryOut,
    OkResponse,
    SkillImportPreviewOut,
    SkillImportUrlRequest,
    SkillLayoutSpecSchema,
)
from backend.worker import (
    PipelineCancelled,
    get_worker,
    resolve_effective_llm_client,
    resolve_project_path,
)
from core.bid_composer import compose_bid_blueprint, export_composed_bid_docx
from core.composer_skills import (
    BUILTIN_WRITING_SKILLS,
    SkillImportError,
    import_skill_from_git_url,
    normalize_layout_spec,
    parse_skill_markdown_or_json,
)

router = APIRouter(prefix='/api/composer', tags=['composer'])

_ALLOWED_EXTS = {'.docx', '.pdf', '.txt'}
_ALLOWED_SKILL_EXTS = {'.md', '.markdown', '.txt', '.json'}
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100MB
_MAX_SKILL_BYTES = 256 * 1024          # 256KB
_COMPOSER_UPLOADS_ROOT = (PROJECT_ROOT / 'data' / 'uploads' / 'composer').resolve()
_COMPOSER_OUTPUT_ROOT = (PROJECT_ROOT / 'output' / 'composer').resolve()
_DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _validate_and_read_upload(upload: UploadFile, role_label: str) -> tuple[str, str, bytes]:
    orig_name = Path(upload.filename or f"{role_label}.docx").name
    ext = Path(orig_name).suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(
            status_code=422,
            detail={
                'code': 'UNSUPPORTED_FILE_TYPE',
                'message': f"不支持的文件格式 {ext}（{orig_name}），仅支持 .docx/.pdf/.txt",
            },
        )
    content = upload.file.read()
    if not content:
        raise HTTPException(
            status_code=422,
            detail={'code': 'EMPTY_FILE', 'message': f"上传的文件 {orig_name} 内容为空"},
        )
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=422,
            detail={'code': 'FILE_TOO_LARGE', 'message': f"文件 {orig_name} 超过 100MB 上限"},
        )
    return orig_name, ext, content


def ensure_builtin_composer_skills(db: Session, tenant_id: int) -> list[ComposerSkill]:
    """为租户幂等初始化 3 套默认投标写作 Skills（零外网依赖、支持后续编辑或增删）。"""
    existing = (
        db.query(ComposerSkill)
        .filter_by(tenant_id=tenant_id)
        .order_by(ComposerSkill.is_default.desc(), ComposerSkill.id.asc())
        .all()
    )
    if existing:
        return existing

    created: list[ComposerSkill] = []
    for item in BUILTIN_WRITING_SKILLS:
        row = ComposerSkill(
            tenant_id=tenant_id,
            user_id=None,
            name=item['name'],
            description=item['description'],
            source_type=item['source_type'],
            source_url=None,
            writing_instructions=item['writing_instructions'],
            layout_spec_json=normalize_layout_spec(item['layout_spec']),
            is_builtin=bool(item.get('is_builtin', True)),
            is_default=bool(item.get('is_default', False)),
        )
        db.add(row)
        created.append(row)
    db.commit()
    for row in created:
        db.refresh(row)
    return created


def _skill_to_out(row: ComposerSkill) -> ComposerSkillOut:
    layout = normalize_layout_spec(row.layout_spec_json or {})
    return ComposerSkillOut(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        name=row.name,
        description=row.description or '',
        source_type=row.source_type or 'custom',
        source_url=row.source_url,
        writing_instructions=row.writing_instructions or '',
        layout_spec=SkillLayoutSpecSchema(**layout),
        is_builtin=bool(row.is_builtin),
        is_default=bool(row.is_default),
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


def _get_tenant_skill_or_404(db: Session, skill_id: int, tenant_id: int) -> ComposerSkill:
    row = db.query(ComposerSkill).filter_by(id=skill_id, tenant_id=tenant_id).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'SKILL_NOT_FOUND', 'message': '写作 Skill 不存在或无权访问'},
        )
    return row


def _get_tenant_draft_or_404(db: Session, draft_id: int, tenant_id: int) -> BidComposeDraft:
    row = (
        db.query(BidComposeDraft)
        .filter_by(id=draft_id, tenant_id=tenant_id)
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'COMPOSER_DRAFT_NOT_FOUND', 'message': '标书编制草稿不存在或无权访问'},
        )
    return row


def _to_summary_out(row: BidComposeDraft) -> ComposeDraftSummaryOut:
    bp = row.blueprint_json or {}
    stats = row.stats_json or bp.get('stats') or {}
    mode = row.compose_mode if row.compose_mode in ('skeleton_v1', 'knowledge_draft_v2') else 'skeleton_v1'
    return ComposeDraftSummaryOut(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        skill_id=row.skill_id,
        skill_name=str(stats.get('skill_name') or bp.get('skill_name') or ''),
        title=row.title,
        status=str(row.status or 'completed'),
        progress=int(row.progress if row.progress is not None else 100),
        stage_detail=str(row.stage_detail or ''),
        error_msg=row.error_msg,
        tender_filename=row.tender_filename,
        compose_mode=mode,  # type: ignore[arg-type]
        project_name=str(bp.get('project_name') or row.title or ''),
        project_code=str(bp.get('project_code') or ''),
        open_date=bp.get('open_date'),
        price_cap=bp.get('price_cap'),
        manual_item_count=int(stats.get('manual_item_count') or len(bp.get('manual_checklist') or [])),
        scoring_fill_mode=str(stats.get('scoring_fill_mode') or 'outline_stub'),
        ai_status=str(stats.get('ai_status') or bp.get('ai_status') or 'skipped'),
        stats=stats,
        download_url=f"/api/composer/drafts/{row.id}/download",
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


def _to_detail_out(row: BidComposeDraft) -> ComposeDraftDetailOut:
    summary = _to_summary_out(row)
    return ComposeDraftDetailOut(
        **summary.model_dump(),
        blueprint=row.blueprint_json or {},
    )


# ── Skills 管理与 GitHub / Gitee / 本地导入端点 ─────────────────────────────

@router.get('/skills', response_model=ComposerSkillListOut)
def list_composer_skills(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ComposerSkillListOut:
    rows = ensure_builtin_composer_skills(db, int(current_user.tenant_id))
    return ComposerSkillListOut(
        total=len(rows),
        items=[_skill_to_out(r) for r in rows],
    )


@router.post('/skills', response_model=ComposerSkillOut)
def create_composer_skill(
    body: ComposerSkillUpsertRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ComposerSkillOut:
    tenant_id = int(current_user.tenant_id)
    ensure_builtin_composer_skills(db, tenant_id)
    if body.is_default:
        db.query(ComposerSkill).filter_by(tenant_id=tenant_id).update({'is_default': False})
    row = ComposerSkill(
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        name=body.name.strip()[:120],
        description=(body.description or '').strip()[:500],
        source_type='custom',
        writing_instructions=body.writing_instructions.strip()[:16_000],
        layout_spec_json=normalize_layout_spec(body.layout_spec),
        is_builtin=False,
        is_default=bool(body.is_default),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    record_audit(
        db,
        action='composer_skill_create',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"skill:{row.id}",
        detail_json={'name': row.name},
    )
    return _skill_to_out(row)


@router.put('/skills/{skill_id}', response_model=ComposerSkillOut)
def update_composer_skill(
    skill_id: int,
    body: ComposerSkillUpsertRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ComposerSkillOut:
    tenant_id = int(current_user.tenant_id)
    row = _get_tenant_skill_or_404(db, skill_id, tenant_id)
    if body.is_default:
        db.query(ComposerSkill).filter_by(tenant_id=tenant_id).update({'is_default': False})
    row.name = body.name.strip()[:120]
    row.description = (body.description or '').strip()[:500]
    row.writing_instructions = body.writing_instructions.strip()[:16_000]
    row.layout_spec_json = normalize_layout_spec(body.layout_spec)
    row.is_default = bool(body.is_default)
    row.updated_at = _utc_now()
    db.commit()
    db.refresh(row)
    record_audit(
        db,
        action='composer_skill_update',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"skill:{row.id}",
        detail_json={'name': row.name},
    )
    return _skill_to_out(row)


@router.delete('/skills/{skill_id}', response_model=OkResponse)
def delete_composer_skill(
    skill_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OkResponse:
    tenant_id = int(current_user.tenant_id)
    row = _get_tenant_skill_or_404(db, skill_id, tenant_id)
    total = db.query(ComposerSkill).filter_by(tenant_id=tenant_id).count()
    if total <= 1:
        raise HTTPException(
            status_code=409,
            detail={'code': 'LAST_SKILL_PROTECTED', 'message': '至少需保留 1 个可用写作 Skill'},
        )
    was_default = bool(row.is_default)
    db.delete(row)
    db.flush()
    if was_default:
        first_left = db.query(ComposerSkill).filter_by(tenant_id=tenant_id).order_by(ComposerSkill.id.asc()).first()
        if first_left is not None:
            first_left.is_default = True
    db.commit()
    record_audit(
        db,
        action='composer_skill_delete',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"skill:{skill_id}",
    )
    return OkResponse(ok=True, message='写作 Skill 已删除')


@router.post('/skills/preview-url', response_model=SkillImportPreviewOut)
def preview_skill_from_url(
    body: SkillImportUrlRequest,
    _current_user: AuthUser = Depends(get_current_user),
) -> SkillImportPreviewOut:
    """从 GitHub / Gitee 拉取并预览纯文本 Skill（含离线优雅降级提示）。"""
    try:
        parsed = import_skill_from_git_url(
            body.url_or_repo,
            allow_gitee_fallback=body.allow_gitee_fallback,
        )
    except SkillImportError as exc:
        raise HTTPException(
            status_code=422,
            detail={'code': exc.code, 'message': exc.message, **exc.detail},
        ) from exc
    return SkillImportPreviewOut(
        name=parsed['name'],
        description=parsed['description'],
        source_type=parsed['source_type'],
        source_url=parsed.get('source_url'),
        writing_instructions=parsed['writing_instructions'],
        layout_spec=SkillLayoutSpecSchema(**parsed['layout_spec']),
        fetched_from_provider=parsed.get('fetched_from_provider', ''),
        used_fallback=bool(parsed.get('used_fallback', False)),
    )


@router.post('/skills/import-url', response_model=ComposerSkillOut)
def import_skill_by_url(
    body: SkillImportUrlRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ComposerSkillOut:
    tenant_id = int(current_user.tenant_id)
    ensure_builtin_composer_skills(db, tenant_id)
    try:
        parsed = import_skill_from_git_url(
            body.url_or_repo,
            allow_gitee_fallback=body.allow_gitee_fallback,
        )
    except SkillImportError as exc:
        raise HTTPException(
            status_code=422,
            detail={'code': exc.code, 'message': exc.message, **exc.detail},
        ) from exc

    row = ComposerSkill(
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        name=parsed['name'],
        description=parsed['description'],
        source_type=parsed['source_type'],
        source_url=parsed.get('source_url'),
        writing_instructions=parsed['writing_instructions'],
        layout_spec_json=normalize_layout_spec(parsed['layout_spec']),
        is_builtin=False,
        is_default=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    record_audit(
        db,
        action='composer_skill_import_url',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"skill:{row.id}",
        detail_json={'name': row.name, 'source_type': row.source_type, 'source_url': row.source_url},
    )
    return _skill_to_out(row)


@router.post('/skills/import-file', response_model=ComposerSkillOut)
def import_skill_from_file(
    file: UploadFile = File(...),
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ComposerSkillOut:
    """离线兜底：上传本地 SKILL.md / Markdown / JSON 文件直接解析入库（无需外网）。"""
    orig_name = Path(file.filename or 'SKILL.md').name
    ext = Path(orig_name).suffix.lower()
    if ext not in _ALLOWED_SKILL_EXTS:
        raise HTTPException(
            status_code=422,
            detail={'code': 'UNSUPPORTED_SKILL_FILE', 'message': 'Skill 文件仅支持 .md / .markdown / .txt / .json'},
        )
    raw_bytes = file.file.read()
    if not raw_bytes:
        raise HTTPException(status_code=422, detail={'code': 'EMPTY_FILE', 'message': '上传的 Skill 文件为空'})
    if len(raw_bytes) > _MAX_SKILL_BYTES:
        raise HTTPException(status_code=422, detail={'code': 'FILE_TOO_LARGE', 'message': 'Skill 文件大小不可超过 256KB'})

    try:
        raw_text = raw_bytes.decode('utf-8')
    except UnicodeDecodeError:
        raw_text = raw_bytes.decode('gb18030', errors='replace')

    try:
        parsed = parse_skill_markdown_or_json(
            raw_text,
            source_name=orig_name,
            default_source_type='local_file',
        )
    except SkillImportError as exc:
        raise HTTPException(status_code=422, detail={'code': exc.code, 'message': exc.message}) from exc

    tenant_id = int(current_user.tenant_id)
    ensure_builtin_composer_skills(db, tenant_id)
    row = ComposerSkill(
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        name=parsed['name'],
        description=parsed['description'],
        source_type='local_file',
        source_url=None,
        writing_instructions=parsed['writing_instructions'],
        layout_spec_json=normalize_layout_spec(parsed['layout_spec']),
        is_builtin=False,
        is_default=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    record_audit(
        db,
        action='composer_skill_import_file',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"skill:{row.id}",
        detail_json={'name': row.name, 'filename': orig_name},
    )
    return _skill_to_out(row)


# ── 标书编制任务（支持 async_run=True 后台异步运行 & 切页保活）────────────────

@router.post('/generate', response_model=ComposeDraftDetailOut)
def generate_composed_draft(
    tender_file: Optional[UploadFile] = File(default=None),
    task_id: Optional[int] = Form(default=None),
    knowledge_files: list[UploadFile] = File(default=[]),
    compose_mode: str = Form(default='skeleton_v1'),
    title: Optional[str] = Form(default=None),
    package_no: Optional[int] = Form(default=None),
    skill_id: Optional[int] = Form(default=None),
    use_ai: bool = Form(default=True),
    async_run: bool = Form(default=False),
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ComposeDraftDetailOut:
    """E37 + E38：上传招标书或复用体检任务，结合选定写作 Skill 与可选 AI 撰写标书初稿（支持异步后台执行）。"""
    mode_clean = (compose_mode or 'skeleton_v1').strip()
    if mode_clean not in ('skeleton_v1', 'knowledge_draft_v2'):
        raise HTTPException(
            status_code=422,
            detail={'code': 'INVALID_COMPOSE_MODE', 'message': f"非法编制模式: {mode_clean}"},
        )

    tenant_id = int(current_user.tenant_id)
    user_id = current_user.get('id')
    has_upload = tender_file is not None and bool((tender_file.filename or '').strip())
    if not has_upload and task_id is None:
        raise HTTPException(
            status_code=422,
            detail={'code': 'MISSING_TENDER_INPUT', 'message': '请上传招标文件，或指定一个已有的体检任务进行装配'},
        )

    skills = ensure_builtin_composer_skills(db, tenant_id)
    selected_skill: Optional[ComposerSkill] = None
    if skill_id is not None:
        selected_skill = _get_tenant_skill_or_404(db, int(skill_id), tenant_id)
    elif skills:
        selected_skill = next((s for s in skills if s.is_default), skills[0])

    skill_snapshot: Optional[dict[str, Any]] = None
    if selected_skill is not None:
        skill_snapshot = {
            'id': selected_skill.id,
            'name': selected_skill.name,
            'writing_instructions': selected_skill.writing_instructions,
            'layout_spec': normalize_layout_spec(selected_skill.layout_spec_json or {}),
        }

    source_task: Optional[BidCheckTask] = None
    if task_id is not None:
        source_task = db.query(BidCheckTask).filter_by(id=int(task_id), tenant_id=tenant_id).first()
        if source_task is None:
            raise HTTPException(
                status_code=404,
                detail={'code': 'TASK_NOT_FOUND', 'message': '关联的体检任务不存在或无权访问'},
            )

    tender_bytes_info: Optional[tuple[str, str, bytes]] = None
    if has_upload and tender_file is not None:
        tender_bytes_info = _validate_and_read_upload(tender_file, 'tender')
    else:
        assert source_task is not None
        task_tender_abs = resolve_project_path(source_task.tender_path)
        if not task_tender_abs.is_file():
            raise HTTPException(
                status_code=422,
                detail={'code': 'TENDER_FILE_MISSING', 'message': '关联任务的招标源文件缺失，请重新上传招标文件'},
            )

    valid_knowledge_payloads: list[tuple[str, str, bytes]] = []
    for idx, kf in enumerate(knowledge_files or []):
        if kf is None or not (kf.filename or '').strip():
            continue
        valid_knowledge_payloads.append(_validate_and_read_upload(kf, f"knowledge_{idx}"))

    tender_filename_disp = (
        tender_bytes_info[0]
        if tender_bytes_info is not None
        else (source_task.tender_filename or '招标文件.docx')  # type: ignore[union-attr]
    )
    custom_title = (title or '').strip()
    eff_title = custom_title or (
        source_task.title if source_task is not None and not tender_bytes_info else Path(tender_filename_disp).stem
    ) or '未命名投标编制初稿'

    draft = BidComposeDraft(
        tenant_id=tenant_id,
        user_id=user_id,
        skill_id=selected_skill.id if selected_skill is not None else None,
        title=eff_title[:250],
        status='queued' if async_run else 'running',
        progress=5,
        stage_detail='任务已入队，等待后台开始编制…' if async_run else '正在编制标书初稿…',
        tender_filename=tender_filename_disp[:250],
        tender_path='',
        compose_mode=mode_clean,
        blueprint_json={},
        output_path='',
        stats_json={'skill_name': selected_skill.name if selected_skill else ''},
        started_at=_utc_now(),
    )
    db.add(draft)
    db.flush()

    upload_dir = (_COMPOSER_UPLOADS_ROOT / str(tenant_id) / str(draft.id)).resolve()
    if not upload_dir.is_relative_to(_COMPOSER_UPLOADS_ROOT):
        db.rollback()
        raise HTTPException(status_code=422, detail={'code': 'PATH_TRAVERSAL', 'message': '非法上传目录'})
    upload_dir.mkdir(parents=True, exist_ok=True)

    saved_k_paths: list[Path] = []
    try:
        if tender_bytes_info is not None:
            _orig_t_name, t_ext, t_bytes = tender_bytes_info
            tender_abs = (upload_dir / f"tender{t_ext}").resolve()
            tender_abs.write_bytes(t_bytes)
            tender_rel = tender_abs.relative_to(PROJECT_ROOT.resolve()).as_posix()
        else:
            assert source_task is not None
            tender_abs = resolve_project_path(source_task.tender_path)
            tender_rel = source_task.tender_path

        for idx, (_k_name, k_ext, k_bytes) in enumerate(valid_knowledge_payloads):
            k_abs = (upload_dir / f"knowledge_{idx}{k_ext}").resolve()
            k_abs.write_bytes(k_bytes)
            saved_k_paths.append(k_abs)

        draft.tender_path = tender_rel
        db.commit()
        db.refresh(draft)
    except HTTPException:
        db.rollback()
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise
    except Exception as exc:
        db.rollback()
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise HTTPException(
            status_code=422,
            detail={'code': 'COMPOSER_UPLOAD_FAILED', 'message': f"上传源文件存储失败：{exc}"},
        ) from exc

    draft_id = int(draft.id)
    source_task_id = int(source_task.id) if source_task is not None else None
    worker = get_worker()
    job_key = f"composer:{draft_id}"
    sync_error_box: list[Exception] = []

    def _run_composer_job(cancel_ev) -> None:
        sess: Session = worker.session_factory()
        last_pct = 5
        last_stage = ''
        try:
            d_row = sess.query(BidComposeDraft).filter_by(id=draft_id).first()
            if d_row is None:
                return
            d_row.status = 'running'
            d_row.progress = 8
            d_row.stage_detail = '正在加载证照台账与写作 Skill…'
            d_row.updated_at = _utc_now()
            sess.commit()

            certs = sess.query(CompanyCertificate).filter_by(tenant_id=tenant_id).all()
            rule_cfg = (
                sess.query(BidRuleConfig)
                .filter_by(tenant_id=tenant_id)
                .order_by(BidRuleConfig.is_default.desc(), BidRuleConfig.id.asc())
                .first()
            )
            f4_rows: list[BidFinding] = []
            if source_task_id is not None:
                f4_rows = (
                    sess.query(BidFinding)
                    .filter(BidFinding.task_id == source_task_id, BidFinding.feedback != 'rejected')
                    .order_by(BidFinding.id.asc())
                    .all()
                )

            llm_client = None
            if use_ai:
                llm_client, _ = resolve_effective_llm_client(sess, user_id)

            def _on_prog(pct: int, detail: str) -> None:
                nonlocal last_pct, last_stage
                worker.check_cancelled(job_key, cancel_ev)
                if pct - last_pct < 5 and detail == last_stage:
                    return
                last_pct = pct
                last_stage = detail
                try:
                    cur = sess.query(BidComposeDraft).filter_by(id=draft_id).first()
                    if cur is not None:
                        cur.progress = min(98, max(5, int(pct)))
                        cur.stage_detail = str(detail)[:250]
                        cur.updated_at = _utc_now()
                        sess.commit()
                except Exception:
                    sess.rollback()

            blueprint = compose_bid_blueprint(
                tender_abs,
                ledger_certs=certs,
                rule_config=rule_cfg,
                f4_findings=f4_rows,
                task_title=eff_title,
                package_no=package_no,
                compose_mode=mode_clean,
                knowledge_paths=saved_k_paths,
                client=llm_client,
                skill=skill_snapshot,
                use_ai=use_ai,
                progress_cb=_on_prog,
                cancel_check=lambda: worker.check_cancelled(job_key, cancel_ev),
            )
            bp_dict = blueprint.to_dict()

            _COMPOSER_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
            out_docx_abs = (_COMPOSER_OUTPUT_ROOT / f"{tenant_id}_{draft_id}_投标文件初稿骨架.docx").resolve()
            if not out_docx_abs.is_relative_to(_COMPOSER_OUTPUT_ROOT):
                raise RuntimeError('非法输出路径')
            export_composed_bid_docx(bp_dict, out_docx_abs)
            out_rel = out_docx_abs.relative_to(PROJECT_ROOT.resolve()).as_posix()

            # 持久化前剔除宿主机本地磁盘路径，避免 DB/API 暴露服务器存储结构
            for sec in bp_dict.get('sections') or []:
                for blk in sec.get('content_blocks') or []:
                    if isinstance(blk, dict) and 'attachment_path' in blk:
                        blk.pop('attachment_path', None)

            worker.check_cancelled(job_key, cancel_ev)
            now_utc = _utc_now()
            d_row = sess.query(BidComposeDraft).filter_by(id=draft_id).first()
            if d_row is None:
                return
            d_row.blueprint_json = bp_dict
            d_row.stats_json = bp_dict.get('stats') or {}
            d_row.output_path = out_rel
            if not custom_title and bp_dict.get('project_name'):
                d_row.title = str(bp_dict['project_name'])[:250]
            d_row.status = 'completed'
            d_row.progress = 100
            d_row.stage_detail = '标书初稿编排与导出完成'
            d_row.error_msg = None
            d_row.finished_at = now_utc
            d_row.updated_at = now_utc
            sess.commit()

            record_audit(
                sess,
                action='composer_generate',
                tenant_id=tenant_id,
                user_id=user_id,
                target=f"composer:{draft_id}",
                detail_json={
                    'title': d_row.title,
                    'compose_mode': mode_clean,
                    'skill_name': bp_dict.get('skill_name'),
                    'ai_status': bp_dict.get('ai_status'),
                    'knowledge_count': len(saved_k_paths),
                },
            )
        except PipelineCancelled:
            sess.rollback()
            now_utc = _utc_now()
            d_row = sess.query(BidComposeDraft).filter_by(id=draft_id).first()
            if d_row is not None:
                d_row.status = 'failed'
                d_row.stage_detail = '用户中断'
                d_row.error_msg = '用户中断'
                d_row.finished_at = now_utc
                d_row.updated_at = now_utc
                sess.commit()
        except Exception as exc:
            sess.rollback()
            if not async_run:
                sync_error_box.append(exc)
            now_utc = _utc_now()
            d_row = sess.query(BidComposeDraft).filter_by(id=draft_id).first()
            if d_row is not None:
                d_row.status = 'failed'
                d_row.error_msg = str(exc)[:500]
                d_row.stage_detail = f"生成失败：{str(exc)[:120]}"
                d_row.finished_at = now_utc
                d_row.updated_at = now_utc
                sess.commit()
        finally:
            sess.close()

    worker.submit_background_job(job_key, _run_composer_job, sync=(not async_run))

    if not async_run and sync_error_box:
        shutil.rmtree(upload_dir, ignore_errors=True)
        row_del = db.query(BidComposeDraft).filter_by(id=draft_id).first()
        if row_del is not None:
            db.delete(row_del)
            db.commit()
        raise HTTPException(
            status_code=422,
            detail={'code': 'COMPOSER_GENERATE_FAILED', 'message': f"标书初稿生成失败：{sync_error_box[0]}"},
        )

    db.expire_all()
    fresh = _get_tenant_draft_or_404(db, draft_id, tenant_id)
    return _to_detail_out(fresh)


@router.post('/drafts/{draft_id}/cancel', response_model=OkResponse)
def cancel_composed_draft(
    draft_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OkResponse:
    tenant_id = int(current_user.tenant_id)
    row = _get_tenant_draft_or_404(db, draft_id, tenant_id)
    if row.status not in ('queued', 'running'):
        raise HTTPException(
            status_code=409,
            detail={'code': 'TASK_NOT_RUNNING', 'message': f"草稿当前状态为 {row.status}，无法中断"},
        )
    worker = get_worker()
    worker.request_cancel(f"composer:{draft_id}")
    now_utc = _utc_now()
    row.status = 'failed'
    row.stage_detail = '用户中断'
    row.error_msg = '用户中断'
    row.finished_at = now_utc
    row.updated_at = now_utc
    db.commit()
    return OkResponse(ok=True, message='已发出中断指令')


@router.get('/drafts', response_model=ComposeDraftListOut)
def list_composed_drafts(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ComposeDraftListOut:
    """列出当前租户的历史标书编制初稿草稿箱。"""
    tenant_id = int(current_user.tenant_id)
    rows = (
        db.query(BidComposeDraft)
        .filter_by(tenant_id=tenant_id)
        .order_by(BidComposeDraft.id.desc())
        .all()
    )
    return ComposeDraftListOut(
        total=len(rows),
        items=[_to_summary_out(r) for r in rows],
    )


@router.get('/drafts/{draft_id}', response_model=ComposeDraftDetailOut)
def get_composed_draft(
    draft_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ComposeDraftDetailOut:
    """查看标书编制初稿蓝图详情（严格租户隔离）。"""
    row = _get_tenant_draft_or_404(db, draft_id, int(current_user.tenant_id))
    return _to_detail_out(row)


@router.get('/drafts/{draft_id}/download')
def download_composed_draft(
    draft_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """下载生成的《投标文件初稿.docx》。"""
    tenant_id = int(current_user.tenant_id)
    row = _get_tenant_draft_or_404(db, draft_id, tenant_id)
    if row.status in ('queued', 'running'):
        raise HTTPException(
            status_code=409,
            detail={'code': 'COMPOSER_TASK_RUNNING', 'message': '标书初稿正在后台生成中，请稍候完成后再下载'},
        )
    if row.status == 'failed':
        raise HTTPException(
            status_code=422,
            detail={'code': 'COMPOSER_DRAFT_FAILED', 'message': row.error_msg or '该草稿生成失败，无法导出文件'},
        )

    out_file = (_COMPOSER_OUTPUT_ROOT / f"{tenant_id}_{row.id}_投标文件初稿骨架.docx").resolve()
    if not out_file.is_relative_to(_COMPOSER_OUTPUT_ROOT):
        raise HTTPException(status_code=422, detail={'code': 'PATH_TRAVERSAL', 'message': '非法下载路径'})
    if not out_file.is_file():
        export_composed_bid_docx(row.blueprint_json or {}, out_file)

    record_audit(
        db,
        action='composer_download',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"composer:{row.id}",
    )
    return FileResponse(
        str(out_file),
        media_type=_DOCX_MIME,
        filename=f"{row.title}_投标文件初稿.docx",
    )


@router.delete('/drafts/{draft_id}', response_model=OkResponse)
def delete_composed_draft(
    draft_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OkResponse:
    """删除编制草稿并级联清理磁盘上传文件与导出文档。"""
    tenant_id = int(current_user.tenant_id)
    row = _get_tenant_draft_or_404(db, draft_id, tenant_id)
    if row.status in ('queued', 'running'):
        raise HTTPException(
            status_code=409,
            detail={'code': 'COMPOSER_TASK_RUNNING', 'message': '任务正在后台运行，请先中断后再删除'},
        )

    upload_dir = (_COMPOSER_UPLOADS_ROOT / str(tenant_id) / str(row.id)).resolve()
    if upload_dir.is_dir() and upload_dir.is_relative_to(_COMPOSER_UPLOADS_ROOT):
        shutil.rmtree(upload_dir, ignore_errors=True)
    out_file = (_COMPOSER_OUTPUT_ROOT / f"{tenant_id}_{row.id}_投标文件初稿骨架.docx").resolve()
    if out_file.is_file() and out_file.is_relative_to(_COMPOSER_OUTPUT_ROOT):
        out_file.unlink(missing_ok=True)

    db.delete(row)
    db.commit()
    record_audit(
        db,
        action='composer_delete',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"composer:{draft_id}",
    )
    return OkResponse(ok=True, message='标书编制草稿及生成文件已清理删除')
