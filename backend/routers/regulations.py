# -*- coding: utf-8 -*-
"""E4 · 法规库与法条核验路由（/api/regulations/*）。

端点清单（对应 E4 任务卡 §1）：
1. GET  /api/regulations/laws        ：法规列表（名称、发文机关、施行日期、条文数、哈希、更新时间、来源）
2. GET  /api/regulations/laws/{id}   ：单部法规详情 + 条文分页与 keyword 检索
3. POST /api/regulations/verify      ：法条真伪三态核验（exact_match / fuzzy_hint / unverifiable）
4. POST /api/regulations/import      ：上传 json/txt/docx/pdf 四格式导入法规（admin 权限，PDF 走 parse_document OCR 链路）
5. POST /api/regulations/crawl       ：按 config/regulation_sources.yaml 检查官网更新（admin 权限，TODO_USER_FILL 诚实报"源未配置"）
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit, require_admin
from backend.database import get_db
from backend.models import RegulationArticle, RegulationLaw
from backend.schemas import OkResponse
from core.regulation_crawler import crawl_all_configured_sources
from core.regulation_library import import_regulation_file, verify_article

router = APIRouter(prefix='/api/regulations', tags=['regulations'])

_ALLOWED_IMPORT_EXTS = {'.json', '.txt', '.docx', '.pdf'}


# ── Pydantic 响应与请求契约（显式 response_model，禁裸 dict 出路由）──────────

class RegulationLawOut(BaseModel):
    id: int
    name: str
    issuing_body: Optional[str] = None
    effective_date: Optional[str] = None
    source_url: Optional[str] = None
    article_count: int = 0
    content_hash: str = ''
    is_seed: bool = False
    fetched_at: Optional[str] = None
    updated_at: Optional[str] = None


class RegulationArticleOut(BaseModel):
    id: int
    law_id: int
    article_no: str
    article_title: Optional[str] = None
    content: str
    content_hash: str = ''


class RegulationLawDetailOut(BaseModel):
    law: RegulationLawOut
    total_articles: int = 0
    page: int = 1
    page_size: int = 50
    articles: list[RegulationArticleOut] = Field(default_factory=list)


class VerifyArticleRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class VerifyArticleOut(BaseModel):
    status: str  # 'exact_match' | 'fuzzy_hint' | 'unverifiable'
    verified: bool
    law: Optional[str] = None
    article_no: Optional[str] = None
    official_content: Optional[str] = None
    similarity: float = 0.0
    badge: str
    message: str
    llm_diff_hint: Optional[str] = None


class RegulationImportOut(BaseModel):
    law_id: int
    law_name: str
    article_count: int
    content_hash: str
    added_count: int
    modified_count: int
    unchanged_count: int


class CrawlSourceResultItem(BaseModel):
    name: str
    url: str
    status: str
    updated: bool
    article_count: int = 0
    added_count: int = 0
    modified_count: int = 0
    unchanged_count: int = 0
    message: str


class RegulationCrawlOut(BaseModel):
    total_sources: int
    updated_laws_count: int
    unconfigured_count: int
    results: list[CrawlSourceResultItem] = Field(default_factory=list)


def _law_to_out(row: RegulationLaw) -> RegulationLawOut:
    return RegulationLawOut(
        id=row.id,
        name=row.name,
        issuing_body=row.issuing_body,
        effective_date=row.effective_date,
        source_url=row.source_url,
        article_count=int(row.article_count or 0),
        content_hash=row.content_hash or '',
        is_seed=bool(row.is_seed),
        fetched_at=row.fetched_at.isoformat() if row.fetched_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


@router.get('/laws', response_model=list[RegulationLawOut])
def list_regulation_laws(
    _current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RegulationLawOut]:
    """查询已入库的全部招投标法律法规列表。"""
    rows = db.query(RegulationLaw).order_by(RegulationLaw.id.asc()).all()
    return [_law_to_out(r) for r in rows]


@router.get('/laws/{law_id}', response_model=RegulationLawDetailOut)
def get_regulation_law_detail(
    law_id: int,
    keyword: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    _current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RegulationLawDetailOut:
    """获取单部法规详情及条文分页列表（支持按条号或正文 keyword 过滤）。"""
    law = db.query(RegulationLaw).filter_by(id=law_id).first()
    if law is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'LAW_NOT_FOUND', 'message': '请求的法规不存在'},
        )

    q = db.query(RegulationArticle).filter_by(law_id=law.id)
    if keyword and keyword.strip():
        kw = keyword.strip()
        q = q.filter(
            or_(
                RegulationArticle.article_no.contains(kw),
                RegulationArticle.content.contains(kw),
            )
        )

    total = q.count()
    rows = (
        q.order_by(RegulationArticle.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return RegulationLawDetailOut(
        law=_law_to_out(law),
        total_articles=total,
        page=page,
        page_size=page_size,
        articles=[
            RegulationArticleOut(
                id=a.id,
                law_id=a.law_id,
                article_no=a.article_no,
                article_title=a.article_title,
                content=a.content,
                content_hash=a.content_hash or '',
            )
            for a in rows
        ],
    )


@router.post('/verify', response_model=VerifyArticleOut)
def verify_regulation_article_endpoint(
    body: VerifyArticleRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VerifyArticleOut:
    """对用户输入的法条文本执行三态真伪核验（exact_match / fuzzy_hint / unverifiable）。"""
    res = verify_article(body.text, session=db, client=None)
    record_audit(
        db,
        action='verify_regulation_article',
        tenant_id=current_user.tenant_id,
        user_id=current_user.get('id'),
        target=res.get('law') or 'unverifiable',
        detail_json={'status': res['status'], 'similarity': res['similarity']},
    )
    return VerifyArticleOut(**res)


@router.post('/import', response_model=RegulationImportOut)
def import_regulation_endpoint(
    file: UploadFile = File(...),
    law_name: Optional[str] = Form(default=None),
    current_user: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RegulationImportOut:
    """管理员上传 json / txt / docx / pdf 四格式法规文件切条入库（PDF 扫描件走 parse_document OCR 链路）。"""
    orig_name = Path(file.filename or 'regulation.json').name
    ext = Path(orig_name).suffix.lower()
    if ext not in _ALLOWED_IMPORT_EXTS:
        raise HTTPException(
            status_code=422,
            detail={
                'code': 'UNSUPPORTED_FORMAT',
                'message': f"不支持的法规文件格式: {ext}，仅支持 json/txt/docx/pdf",
            },
        )

    content = file.file.read()
    if not content:
        raise HTTPException(
            status_code=422,
            detail={'code': 'EMPTY_FILE', 'message': '上传的法规文件内容为空'},
        )

    fd, tmp_str = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    tmp_path = Path(tmp_str)
    tmp_path.write_bytes(content)

    try:
        inferred_hint = (law_name or '').strip() or Path(orig_name).stem
        res = import_regulation_file(
            session=db,
            file_path=tmp_path,
            law_name_hint=inferred_hint,
            source_url=f"upload://{orig_name}",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={'code': 'IMPORT_PARSE_FAILED', 'message': str(exc)},
        ) from exc
    finally:
        tmp_path.unlink(missing_ok=True)
        for suffix_ext in ('.ocr.txt', '.ocr.json', '.media.ocr.json'):
            Path(str(tmp_path) + suffix_ext).unlink(missing_ok=True)

    record_audit(
        db,
        action='import_regulation',
        tenant_id=current_user.tenant_id,
        user_id=current_user.get('id'),
        target=res['law_name'],
        detail_json={
            'filename': orig_name,
            'article_count': res['article_count'],
            'added_count': res['added_count'],
            'modified_count': res['modified_count'],
        },
    )
    return RegulationImportOut(**res)


@router.post('/crawl', response_model=RegulationCrawlOut)
def crawl_regulations_endpoint(
    current_user: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RegulationCrawlOut:
    """管理员触发官网法规更新检查（消费 config/regulation_sources.yaml，TODO_USER_FILL 诚实报"源未配置"）。"""
    report = crawl_all_configured_sources(session=db)
    record_audit(
        db,
        action='crawl_regulations',
        tenant_id=current_user.tenant_id,
        user_id=current_user.get('id'),
        target='regulation_sources.yaml',
        detail_json={
            'total_sources': report['total_sources'],
            'updated_laws_count': report['updated_laws_count'],
            'unconfigured_count': report['unconfigured_count'],
        },
    )
    return RegulationCrawlOut(
        total_sources=report['total_sources'],
        updated_laws_count=report['updated_laws_count'],
        unconfigured_count=report['unconfigured_count'],
        results=[CrawlSourceResultItem(**item) for item in report['results']],
    )


@router.delete('/laws/{law_id}', response_model=OkResponse)
def delete_regulation_law(
    law_id: int,
    current_user: AuthUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> OkResponse:
    """管理员删除指定法规及该法全部条文（级联清理 RegulationArticle）。"""
    law = db.query(RegulationLaw).filter_by(id=law_id).first()
    if law is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'LAW_NOT_FOUND', 'message': f"未找到 ID={law_id} 的法规记录"},
        )

    law_name = law.name
    article_count = int(law.article_count or 0)
    deleted_articles = (
        db.query(RegulationArticle)
        .filter_by(law_id=law_id)
        .delete(synchronize_session=False)
    )
    db.delete(law)
    db.commit()

    record_audit(
        db,
        action='delete_regulation',
        tenant_id=current_user.tenant_id,
        user_id=current_user.get('id'),
        target=law_name,
        detail_json={
            'law_id': law_id,
            'deleted_articles': int(deleted_articles or article_count),
        },
    )
    return OkResponse(
        ok=True,
        message=f"已删除《{law_name}》及全部 {int(deleted_articles or article_count)} 条条文",
    )