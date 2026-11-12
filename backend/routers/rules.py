# -*- coding: utf-8 -*-
"""B2 / E25 · 第 8 组路由：/api/rules（bid_rule_configs CRUD + company_certificates CRUD、证照扫件 OCR 预填与原件留档管理）。"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit
from backend.database import PROJECT_ROOT, get_db
from backend.models import (
    BidRuleConfig,
    CompanyCertificate,
    ensure_company_certificates_columns,
)
from backend.schemas import (
    BidRuleConfigOut,
    BidRuleConfigUpsertRequest,
    CertificateOut,
    CertificateParseOut,
    CertificateUpsertRequest,
    OkResponse,
)
from backend.worker import resolve_project_path
from core.parser import parse_qa_attachment

router = APIRouter(prefix='/api/rules', tags=['rules'])

_CERT_ALLOWED_EXTS = {'.pdf', '.png', '.jpg', '.jpeg'}
_CERT_MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB
_CERT_UPLOADS_ROOT = (PROJECT_ROOT / 'data' / 'uploads' / 'certs').resolve()

_CERT_NAME_PATTERNS = (
    re.compile(r'((?:ISO|IEC|GB/T)\s*[0-9/:.\-]+\s*[^\n，。；：:]{2,25}?(?:管理体系认证证书|认证证书|证书))', re.IGNORECASE),
    re.compile(r'([^\n，。；：:《》\s]{2,28}?(?:管理体系认证证书|等级保护备案证明|安全服务资质证书|软件企业证书|高新技术企业证书|营业执照|安全生产许可证|资质证书|认证证书))'),
)
_CERT_NO_PATTERNS = (
    re.compile(
        r'(?:证书编号|注册号|认证编号|备案编号|统一社会信用代码|许可证编号|编号|No\.?)\s*[：:is为]?\s*([A-Za-z0-9\-_/]{5,36})',
        re.IGNORECASE,
    ),
)
_CERT_VALID_UNTIL_PATTERNS = (
    re.compile(
        r'(?:有效期(?:至|到|截止|期满)?|有效期限(?:至|到)?|至|到|截止日期|到期日)\s*[：:为]?\s*'
        r'(20[2-4]\s*\d)\s*[年\-/.]\s*([01]?\s*\d)\s*[月\-/.]\s*([0-3]?\s*\d)\s*日?'
    ),
    re.compile(
        r'(?:20[1-3]\d)\s*[年\-/.]\s*(?:[01]?\d)\s*[月\-/.]\s*(?:[0-3]?\d)\s*日?\s*(?:至|到|—|-|~|～)\s*'
        r'(20[2-4]\s*\d)\s*[年\-/.]\s*([01]?\s*\d)\s*[月\-/.]\s*([0-3]?\s*\d)\s*日?'
    ),
)


def _extract_cert_fields_from_text(text: str) -> dict[str, Optional[str]]:
    """E25 §2.6.2：0-Token 确定性正则从证照 OCR/PDF 文本中预提取证书名称、证书编号与有效期截止日。"""
    clean_text = (text or '').strip()
    if not clean_text:
        return {'cert_name_hint': None, 'cert_no_hint': None, 'valid_until_hint': None}

    cert_name_hint: Optional[str] = None
    for pat in _CERT_NAME_PATTERNS:
        m = pat.search(clean_text)
        if m:
            cand = re.sub(r'\s+', ' ', m.group(1)).strip()
            if 4 <= len(cand) <= 60:
                cert_name_hint = cand
                break

    cert_no_hint: Optional[str] = None
    for pat in _CERT_NO_PATTERNS:
        m = pat.search(clean_text)
        if m:
            cert_no_hint = m.group(1).strip()
            break

    valid_until_hint: Optional[str] = None
    for pat in _CERT_VALID_UNTIL_PATTERNS:
        for m in pat.finditer(clean_text):
            y_s = re.sub(r'\s+', '', m.group(1))
            m_s = re.sub(r'\s+', '', m.group(2))
            d_s = re.sub(r'\s+', '', m.group(3))
            try:
                dt = datetime(int(y_s), int(m_s), int(d_s))
                valid_until_hint = dt.strftime('%Y-%m-%d')
            except ValueError:
                continue
        if valid_until_hint:
            break

    return {
        'cert_name_hint': cert_name_hint,
        'cert_no_hint': cert_no_hint,
        'valid_until_hint': valid_until_hint,
    }


def _rule_to_out(r: BidRuleConfig) -> BidRuleConfigOut:
    return BidRuleConfigOut(
        id=r.id,
        tenant_id=r.tenant_id,
        name=r.name,
        price_cap=f"{Decimal(str(r.price_cap)):.2f}" if r.price_cap is not None else None,
        open_date=r.open_date.isoformat() if r.open_date else None,
        buyer_name=r.buyer_name,
        required_social_months_json=list(r.required_social_months_json or []),
        custom_redlines_json=list(r.custom_redlines_json or []),
        is_default=bool(r.is_default),
        created_at=r.created_at.isoformat() if r.created_at else None,
        updated_at=r.updated_at.isoformat() if r.updated_at else None,
    )


def _cert_to_out(c: CompanyCertificate) -> CertificateOut:
    today = date.today()
    days_rem = (c.valid_until - today).days if c.valid_until else 0
    warn_limit = int(c.warn_days if c.warn_days is not None else 30)
    att_name = getattr(c, 'attachment_name', None)
    att_path = getattr(c, 'attachment_path', None)
    att_size = getattr(c, 'attachment_size', None)
    return CertificateOut(
        id=c.id,
        tenant_id=c.tenant_id,
        cert_name=c.cert_name,
        cert_no=c.cert_no,
        valid_until=c.valid_until.isoformat(),
        warn_days=warn_limit,
        days_remaining=days_rem,
        is_expired=(days_rem < 0),
        is_expiring_soon=(days_rem <= warn_limit),
        remark=c.remark,
        attachment_name=att_name,
        attachment_size=int(att_size) if att_size is not None else None,
        has_attachment=bool(att_name and att_path),
        created_at=c.created_at.isoformat() if c.created_at else None,
        updated_at=c.updated_at.isoformat() if c.updated_at else None,
    )


def _cleanup_cert_attachment_files(cert: CompanyCertificate) -> None:
    """清理证照绑定的磁盘原件及伴生 OCR 缓存目录。"""
    rel_p = getattr(cert, 'attachment_path', None)
    if rel_p:
        abs_p = resolve_project_path(rel_p)
        if abs_p.is_relative_to(_CERT_UPLOADS_ROOT):
            abs_p.unlink(missing_ok=True)
            for sfx in ('.ocr.txt', '.ocr.json', '.media.ocr.json'):
                Path(str(abs_p) + sfx).unlink(missing_ok=True)
    cert_dir = (_CERT_UPLOADS_ROOT / str(cert.tenant_id) / str(cert.id)).resolve()
    if cert_dir.is_dir() and cert_dir.is_relative_to(_CERT_UPLOADS_ROOT):
        shutil.rmtree(cert_dir, ignore_errors=True)


@router.get('', response_model=list[BidRuleConfigOut])
def list_rule_configs(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[BidRuleConfigOut]:
    """查询本租户下的核查参数与自定义红线配置列表（若为空自动初始化一条默认规则）。"""
    tenant_id = int(current_user.tenant_id)
    rows = db.query(BidRuleConfig).filter_by(tenant_id=tenant_id).order_by(BidRuleConfig.id.asc()).all()
    if not rows:
        default_cfg = BidRuleConfig(
            tenant_id=tenant_id,
            name='默认核查规则',
            price_cap=Decimal('1450000.00'),
            open_date=date(2026, 11, 5),
            buyer_name='',
            required_social_months_json=[],
            custom_redlines_json=[],
            is_default=True,
        )
        db.add(default_cfg)
        db.commit()
        db.refresh(default_cfg)
        rows = [default_cfg]
    return [_rule_to_out(r) for r in rows]


@router.post('', response_model=BidRuleConfigOut)
@router.put('', response_model=BidRuleConfigOut)
def upsert_default_rule_config(
    body: BidRuleConfigUpsertRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BidRuleConfigOut:
    """创建或更新本租户的默认废标核查规则（金额使用 Decimal，禁 float）。"""
    tenant_id = int(current_user.tenant_id)
    cfg = db.query(BidRuleConfig).filter_by(tenant_id=tenant_id, is_default=True).first()
    redlines_dicts = [item.model_dump() for item in body.custom_redlines_json]
    price_dec = Decimal(str(body.price_cap)).quantize(Decimal('0.01')) if body.price_cap is not None else None

    if cfg is None:
        cfg = BidRuleConfig(
            tenant_id=tenant_id,
            name=body.name.strip(),
            price_cap=price_dec,
            open_date=body.open_date,
            buyer_name=body.buyer_name,
            required_social_months_json=list(body.required_social_months_json),
            custom_redlines_json=redlines_dicts,
            is_default=True,
        )
        db.add(cfg)
    else:
        cfg.name = body.name.strip()
        cfg.price_cap = price_dec
        cfg.open_date = body.open_date
        cfg.buyer_name = body.buyer_name
        cfg.required_social_months_json = list(body.required_social_months_json)
        cfg.custom_redlines_json = redlines_dicts

    db.commit()
    db.refresh(cfg)

    record_audit(
        db,
        action='upsert_rule_config',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"rule_config:{cfg.id}",
        detail_json={'name': cfg.name, 'redlines_count': len(redlines_dicts)},
    )
    return _rule_to_out(cfg)


@router.get('/certificates', response_model=list[CertificateOut])
def list_certificates(
    expiring_only: bool = Query(default=False),
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CertificateOut]:
    """查询企业证照效期台账（expiring_only=True 时仅返回 valid_until - today <= warn_days 的预警/过期证照）。"""
    ensure_company_certificates_columns(db.get_bind())
    tenant_id = int(current_user.tenant_id)
    rows = (
        db.query(CompanyCertificate)
        .filter_by(tenant_id=tenant_id)
        .order_by(CompanyCertificate.valid_until.asc())
        .all()
    )
    out_list = [_cert_to_out(c) for c in rows]
    if expiring_only:
        out_list = [c for c in out_list if c.is_expiring_soon]
    return out_list


@router.post('/certificates/parse-file', response_model=CertificateParseOut)
def parse_certificate_file_for_prefill(
    file: UploadFile = File(...),
    _current_user: AuthUser = Depends(get_current_user),
) -> CertificateParseOut:
    """E25 §2.6.2：接收 PDF/图片证照扫件（≤20MB），通过 parse_qa_attachment 提取文本并返回证书名称/编号/有效期候选值。"""
    orig_name = Path(file.filename or 'cert.png').name
    ext = Path(orig_name).suffix.lower()
    if ext not in _CERT_ALLOWED_EXTS:
        raise HTTPException(
            status_code=422,
            detail={
                'code': 'UNSUPPORTED_FILE_TYPE',
                'message': f"不支持的证照文件格式: {ext}，仅支持 pdf/png/jpg/jpeg",
            },
        )

    content = file.file.read()
    if not content:
        raise HTTPException(
            status_code=422,
            detail={'code': 'EMPTY_FILE', 'message': '上传的证照文件内容为空'},
        )
    if len(content) > _CERT_MAX_FILE_BYTES:
        raise HTTPException(
            status_code=422,
            detail={'code': 'FILE_TOO_LARGE', 'message': '证照文件超过 20MB 上限'},
        )

    fd, tmp_str = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    tmp_path = Path(tmp_str)
    tmp_path.write_bytes(content)

    try:
        parsed = parse_qa_attachment(tmp_path)
        full_text = str(parsed.get('text') or '')
        hints = _extract_cert_fields_from_text(full_text)
        name_hint = hints['cert_name_hint'] or Path(orig_name).stem
        return CertificateParseOut(
            cert_name_hint=name_hint,
            cert_no_hint=hints['cert_no_hint'],
            valid_until_hint=hints['valid_until_hint'],
            ocr_chars=len(full_text.strip()),
            text_excerpt=full_text.strip()[:300],
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={'code': 'CERT_PARSE_FAILED', 'message': f"证照文件解析失败：{exc}"},
        ) from exc
    finally:
        tmp_path.unlink(missing_ok=True)
        for sfx in ('.ocr.txt', '.ocr.json', '.media.ocr.json'):
            Path(str(tmp_path) + sfx).unlink(missing_ok=True)


@router.post('/certificates', response_model=CertificateOut)
def create_certificate(
    body: CertificateUpsertRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CertificateOut:
    """新增企业投标证照记录。"""
    ensure_company_certificates_columns(db.get_bind())
    tenant_id = int(current_user.tenant_id)
    cert = CompanyCertificate(
        tenant_id=tenant_id,
        cert_name=body.cert_name.strip(),
        cert_no=body.cert_no.strip() if body.cert_no else None,
        valid_until=body.valid_until,
        warn_days=body.warn_days,
        remark=body.remark,
    )
    db.add(cert)
    db.commit()
    db.refresh(cert)

    record_audit(
        db,
        action='create_certificate',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"cert:{cert.id}",
        detail_json={'cert_name': cert.cert_name, 'valid_until': cert.valid_until.isoformat()},
    )
    return _cert_to_out(cert)


@router.put('/certificates/{cert_id}', response_model=CertificateOut)
def update_certificate(
    cert_id: int,
    body: CertificateUpsertRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CertificateOut:
    """更新本租户下的指定证照记录。"""
    ensure_company_certificates_columns(db.get_bind())
    tenant_id = int(current_user.tenant_id)
    cert = db.query(CompanyCertificate).filter_by(id=cert_id, tenant_id=tenant_id).first()
    if cert is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'CERT_NOT_FOUND', 'message': '证照记录不存在或无权访问'},
        )
    cert.cert_name = body.cert_name.strip()
    cert.cert_no = body.cert_no.strip() if body.cert_no else None
    cert.valid_until = body.valid_until
    cert.warn_days = body.warn_days
    cert.remark = body.remark
    db.commit()
    db.refresh(cert)

    record_audit(
        db,
        action='update_certificate',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"cert:{cert.id}",
    )
    return _cert_to_out(cert)


@router.post('/certificates/{cert_id}/attachment', response_model=CertificateOut)
def upload_certificate_attachment(
    cert_id: int,
    file: UploadFile = File(...),
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CertificateOut:
    """E25 §2.6.2：为指定企业证照上传并绑定原件扫件（落盘至 data/uploads/certs/{tenant_id}/{cert_id}/）。"""
    ensure_company_certificates_columns(db.get_bind())
    tenant_id = int(current_user.tenant_id)
    cert = db.query(CompanyCertificate).filter_by(id=cert_id, tenant_id=tenant_id).first()
    if cert is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'CERT_NOT_FOUND', 'message': '证照记录不存在或无权访问'},
        )

    orig_name = Path(file.filename or 'certificate.pdf').name
    ext = Path(orig_name).suffix.lower()
    if ext not in _CERT_ALLOWED_EXTS:
        raise HTTPException(
            status_code=422,
            detail={
                'code': 'UNSUPPORTED_FILE_TYPE',
                'message': f"不支持的证照文件格式: {ext}，仅支持 pdf/png/jpg/jpeg",
            },
        )

    content = file.file.read()
    size_bytes = len(content)
    if size_bytes == 0:
        raise HTTPException(
            status_code=422,
            detail={'code': 'EMPTY_FILE', 'message': '上传的证照原件为空'},
        )
    if size_bytes > _CERT_MAX_FILE_BYTES:
        raise HTTPException(
            status_code=422,
            detail={'code': 'FILE_TOO_LARGE', 'message': '证照原件超过 20MB 上限'},
        )

    _cleanup_cert_attachment_files(cert)

    cert_dir = (_CERT_UPLOADS_ROOT / str(tenant_id) / str(cert.id)).resolve()
    if not cert_dir.is_relative_to(_CERT_UPLOADS_ROOT):
        raise HTTPException(
            status_code=422,
            detail={'code': 'PATH_TRAVERSAL', 'message': '非法存储目录路径'},
        )
    cert_dir.mkdir(parents=True, exist_ok=True)

    target_file = (cert_dir / f"cert_original{ext}").resolve()
    if not target_file.is_relative_to(_CERT_UPLOADS_ROOT):
        raise HTTPException(
            status_code=422,
            detail={'code': 'PATH_TRAVERSAL', 'message': '非法目标文件路径'},
        )
    target_file.write_bytes(content)

    cert.attachment_name = orig_name
    cert.attachment_path = target_file.relative_to(PROJECT_ROOT.resolve()).as_posix()
    cert.attachment_size = size_bytes
    db.commit()
    db.refresh(cert)

    record_audit(
        db,
        action='upload_certificate_attachment',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"cert:{cert.id}",
        detail_json={'filename': orig_name, 'size_bytes': size_bytes},
    )
    return _cert_to_out(cert)


@router.get('/certificates/{cert_id}/attachment')
def download_certificate_attachment(
    cert_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """E25 §2.6.2：按租户权限安全下载/预览证照绑定的原件扫件。"""
    ensure_company_certificates_columns(db.get_bind())
    tenant_id = int(current_user.tenant_id)
    cert = db.query(CompanyCertificate).filter_by(id=cert_id, tenant_id=tenant_id).first()
    if cert is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'CERT_NOT_FOUND', 'message': '证照记录不存在或无权访问'},
        )
    if not cert.attachment_path:
        raise HTTPException(
            status_code=404,
            detail={'code': 'CERT_ATTACHMENT_NOT_FOUND', 'message': '该证照尚未上传原件附件'},
        )

    abs_p = resolve_project_path(cert.attachment_path)
    if not abs_p.is_file() or not abs_p.is_relative_to(_CERT_UPLOADS_ROOT):
        raise HTTPException(
            status_code=404,
            detail={'code': 'CERT_ATTACHMENT_NOT_FOUND', 'message': '证照原件物理文件不存在'},
        )

    ext = abs_p.suffix.lower()
    media_map = {
        '.pdf': 'application/pdf',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
    }
    return FileResponse(
        str(abs_p),
        media_type=media_map.get(ext, 'application/octet-stream'),
        filename=cert.attachment_name or f"certificate_{cert.id}{ext}",
    )


@router.delete('/certificates/{cert_id}/attachment', response_model=CertificateOut)
def delete_certificate_attachment(
    cert_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CertificateOut:
    """E25 §2.6.2：移除证照绑定的原件附件并清理磁盘文件。"""
    ensure_company_certificates_columns(db.get_bind())
    tenant_id = int(current_user.tenant_id)
    cert = db.query(CompanyCertificate).filter_by(id=cert_id, tenant_id=tenant_id).first()
    if cert is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'CERT_NOT_FOUND', 'message': '证照记录不存在或无权访问'},
        )

    _cleanup_cert_attachment_files(cert)
    cert.attachment_name = None
    cert.attachment_path = None
    cert.attachment_size = None
    db.commit()
    db.refresh(cert)

    record_audit(
        db,
        action='delete_certificate_attachment',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"cert:{cert.id}",
    )
    return _cert_to_out(cert)


@router.delete('/certificates/{cert_id}', response_model=OkResponse)
def delete_certificate(
    cert_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OkResponse:
    """删除本租户下的指定证照记录，并级联清理磁盘上的证照原件目录。"""
    ensure_company_certificates_columns(db.get_bind())
    tenant_id = int(current_user.tenant_id)
    cert = db.query(CompanyCertificate).filter_by(id=cert_id, tenant_id=tenant_id).first()
    if cert is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'CERT_NOT_FOUND', 'message': '证照记录不存在或无权访问'},
        )
    _cleanup_cert_attachment_files(cert)
    db.delete(cert)
    db.commit()

    record_audit(
        db,
        action='delete_certificate',
        tenant_id=tenant_id,
        user_id=current_user.get('id'),
        target=f"cert:{cert_id}",
    )
    return OkResponse(ok=True, message='证照记录已删除')
