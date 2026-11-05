# -*- coding: utf-8 -*-
"""B2 · 第 8 组路由：/api/rules（bid_rule_configs CRUD + company_certificates CRUD 与到期预警计算）。"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit
from backend.database import get_db
from backend.models import BidRuleConfig, CompanyCertificate
from backend.schemas import (
    BidRuleConfigOut,
    BidRuleConfigUpsertRequest,
    CertificateOut,
    CertificateUpsertRequest,
    OkResponse,
)

router = APIRouter(prefix='/api/rules', tags=['rules'])


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
        created_at=c.created_at.isoformat() if c.created_at else None,
        updated_at=c.updated_at.isoformat() if c.updated_at else None,
    )


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


@router.post('/certificates', response_model=CertificateOut)
def create_certificate(
    body: CertificateUpsertRequest,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CertificateOut:
    """新增企业投标证照记录。"""
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


@router.delete('/certificates/{cert_id}', response_model=OkResponse)
def delete_certificate(
    cert_id: int,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OkResponse:
    """删除本租户下的指定证照记录。"""
    tenant_id = int(current_user.tenant_id)
    cert = db.query(CompanyCertificate).filter_by(id=cert_id, tenant_id=tenant_id).first()
    if cert is None:
        raise HTTPException(
            status_code=404,
            detail={'code': 'CERT_NOT_FOUND', 'message': '证照记录不存在或无权访问'},
        )
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