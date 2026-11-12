# -*- coding: utf-8 -*-
"""B2 · Pydantic v2 请求与响应强类型契约（严禁裸 dict 出路由）。

契约依据：INTERFACE.md §1 ~ §3 与 B2-backend路由层.md。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ── 0. 通用错误与基础响应 ─────────────────────────────────────────────────
class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    detail: ErrorDetail


class OkResponse(BaseModel):
    ok: bool = True
    message: str = 'ok'


# ── 1. /api/auth 契约 ────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserProfileOut(BaseModel):
    id: Optional[int] = None
    username: str
    display_name: str = ''
    role: str
    tenant_id: Optional[int] = None
    is_active: bool = True


class LoginResponse(BaseModel):
    token: str
    user: UserProfileOut


class ChangePasswordRequest(BaseModel):
    old_password: Optional[str] = None
    new_password: str = Field(min_length=6, max_length=128)


class HealthOut(BaseModel):
    status: str = 'ok'
    service: str = 'bidguard-agent'
    version: str = '1.0.0'


# ── 2. /api/dashboard 契约 ───────────────────────────────────────────────
class DefenseMatrixItem(BaseModel):
    module: str
    title: str
    hit_count: int = 0
    status: str = 'ok'


class CountdownTaskItem(BaseModel):
    task_id: int
    title: str
    task_type: str
    status: str
    open_date: Optional[str] = None
    days_left: Optional[int] = None
    veto_count: int = 0
    warning_count: int = 0


class DashboardStatsOut(BaseModel):
    tenant_id: int
    total_projects: int = 0
    pre_check_count: int = 0
    radar_count: int = 0
    veto_intercepted_count: int = 0
    warning_count: int = 0
    deviation_tables_generated: int = 0
    star_recall_rate: float = 1.0
    expiring_certs_count: int = 0
    defense_matrix: list[DefenseMatrixItem] = Field(default_factory=list)
    countdown_alerts: list[CountdownTaskItem] = Field(default_factory=list)
    recent_tasks: list[BidTaskOut] = Field(default_factory=list)


# ── 3. /api/checks 契约 ──────────────────────────────────────────────────
class BidFindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    source_module: str
    code: str
    severity: str
    category: Optional[str] = None
    clause_no: Optional[str] = None
    quote: Optional[str] = None
    bid_quote: Optional[str] = None
    quote_offset: Optional[int] = None
    quote_verified: bool = True
    page: Optional[int] = None
    message: str
    suggestion: Optional[str] = None
    consequence: Optional[str] = None
    feedback: str = 'pending'
    feedback_note: Optional[str] = None
    detail_json: Optional[dict[str, Any]] = None
    created_at: Optional[str] = None


class BidTaskOut(BaseModel):
    id: int
    tenant_id: int
    user_id: Optional[int] = None
    task_type: str = 'pre_check'
    title: str
    status: str
    progress: int = 0
    stage_detail: Optional[str] = None
    tender_filename: Optional[str] = None
    bid_filename: Optional[str] = None
    tender_url: Optional[str] = None
    bid_url: Optional[str] = None
    error_msg: Optional[str] = None
    stats_json: Optional[dict[str, Any]] = None
    price_manual_json: Optional[dict[str, Any]] = None
    config_json: Optional[dict[str, Any]] = None
    llm_usage_json: Optional[dict[str, Any]] = None
    delivered_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BidTaskListOut(BaseModel):
    total: int
    items: list[BidTaskOut] = Field(default_factory=list)


class BidTaskDetailOut(BaseModel):
    task: BidTaskOut
    findings: list[BidFindingOut] = Field(default_factory=list)


class FindingListOut(BaseModel):
    total: int
    items: list[BidFindingOut] = Field(default_factory=list)


class FindingFeedbackRequest(BaseModel):
    feedback: Literal['pending', 'accepted', 'rejected']
    feedback_note: Optional[str] = Field(default=None, max_length=500)


class PriceItemInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    qty: Decimal | float | int | str
    unit_price: Decimal | float | int | str
    total_price: Decimal | float | int | str


class PriceManualBackfillRequest(BaseModel):
    items: list[PriceItemInput] = Field(default_factory=list)
    table_total: Optional[Decimal | float | int | str] = None
    bid_total: Optional[Decimal | float | int | str] = None
    bid_total_upper: Optional[str] = None
    price_cap: Optional[Decimal | float | int | str] = None


class PriceManualBackfillOut(BaseModel):
    task_id: int
    status: str
    price_findings_count: int
    findings: list[BidFindingOut] = Field(default_factory=list)


class RunTaskTriggerOut(BaseModel):
    task_id: int
    status: str
    message: str


# ── 4. /api/extract 契约 ─────────────────────────────────────────────────
class ExtractChecklistOut(BaseModel):
    filename: str
    source_type: str
    pages: int = 1
    chars: int = 0
    open_date: Optional[str] = None
    open_date_source: Optional[str] = None
    open_date_quote: Optional[str] = None
    llm_status: str = 'skipped'
    chapters: list[dict[str, Any]] = Field(default_factory=list)
    marked_clauses: list[dict[str, Any]] = Field(default_factory=list)
    invalid_bid_clauses: list[dict[str, Any]] = Field(default_factory=list)
    qualification_hits: list[dict[str, Any]] = Field(default_factory=list)
    compliance_hits: list[dict[str, Any]] = Field(default_factory=list)
    eval_chapter: Optional[dict[str, Any]] = None
    scoring_tables: list[dict[str, Any]] = Field(default_factory=list)
    scoring_prose: list[dict[str, Any]] = Field(default_factory=list)
    llm_structured: list[dict[str, Any]] = Field(default_factory=list)


# ── 5. /api/deviation 契约 ───────────────────────────────────────────────
class DeviationExportOut(BaseModel):
    task_id: int
    download_url: str
    output_filename: str
    total_rows: int = 0
    no_deviation_count: int = 0
    negative_deviation_count: int = 0
    missing_count: int = 0
    contradiction_count: int = 0
    rows: list[dict[str, Any]] = Field(default_factory=list)


# ── 6. /api/radar 契约 ───────────────────────────────────────────────────
class SuspiciousParamOut(BaseModel):
    quote: str
    offset: int = 0
    page: Optional[int] = None
    exclusive_type: str
    confidence: float = 0.8
    reason: str
    quote_verified: bool = True
    llm_status: str = 'skipped'


class RadarConclusionOut(BaseModel):
    recommendation: str
    summary: str


class RadarScanOut(BaseModel):
    task_id: int
    title: str
    open_date: Optional[str] = None
    open_date_source: Optional[str] = None
    open_date_quote: Optional[str] = None
    llm_status: str
    suspicious_params: list[SuspiciousParamOut] = Field(default_factory=list)
    conclusion: RadarConclusionOut

# ── 7. /api/qa 契约 ──────────────────────────────────────────────────────
class QaSessionCreateRequest(BaseModel):
    task_id: Optional[int] = None
    title: Optional[str] = Field(default=None, max_length=256)
    tender_path: Optional[str] = None
    bid_path: Optional[str] = None


class QaAttachmentOut(BaseModel):
    attachment_id: str
    filename: str
    ext: str
    size_bytes: int
    is_image: bool = False
    ocr_chars: int = 0
    has_text: bool = False
    uploaded_at: Optional[str] = None


class QaAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    max_rounds: int = Field(default=7, ge=1, le=8)
    reasoning_effort: Optional[str] = None
    attachment_ids: list[str] = Field(default_factory=list)


class QaQuoteOut(BaseModel):
    source: str
    quote: str
    offset: Optional[int] = None
    page: Optional[int] = None
    quote_verified: bool = True
    note: Optional[str] = None
    attachment_id: Optional[str] = None
    attachment_name: Optional[str] = None


class QaAskResponseOut(BaseModel):
    session_id: int
    mode: str
    question: str
    answer: str
    quotes: list[QaQuoteOut] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    rounds_used: int = 1
    terminated_by_max_rounds: bool = False


class QaSessionOut(BaseModel):
    id: int
    tenant_id: int
    user_id: Optional[int] = None
    task_id: Optional[int] = None
    title: str
    tender_path: Optional[str] = None
    bid_path: Optional[str] = None
    messages_json: list[dict[str, Any]] = Field(default_factory=list)
    attachments: list[QaAttachmentOut] = Field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ── 8. /api/rules 契约 ───────────────────────────────────────────────────
class CustomRedlineItem(BaseModel):
    keyword: str = Field(min_length=1, max_length=128)
    severity: Literal['error', 'warning', 'info'] = 'error'
    consequence: Literal['veto', 'deduction', 'bonus', 'none'] = 'veto'


class BidRuleConfigUpsertRequest(BaseModel):
    name: str = Field(default='默认核查规则', max_length=128)
    price_cap: Optional[Decimal] = None
    open_date: Optional[date] = None
    buyer_name: Optional[str] = Field(default=None, max_length=256)
    required_social_months_json: list[str] = Field(default_factory=list)
    custom_redlines_json: list[CustomRedlineItem] = Field(default_factory=list)
    is_default: bool = True


class BidRuleConfigOut(BaseModel):
    id: int
    tenant_id: int
    name: str
    price_cap: Optional[str] = None
    open_date: Optional[str] = None
    buyer_name: Optional[str] = None
    required_social_months_json: list[str] = Field(default_factory=list)
    custom_redlines_json: list[dict[str, Any]] = Field(default_factory=list)
    is_default: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CertificateUpsertRequest(BaseModel):
    cert_name: str = Field(min_length=1, max_length=256)
    cert_no: Optional[str] = Field(default=None, max_length=128)
    valid_until: date
    warn_days: int = Field(default=30, ge=1, le=365)
    remark: Optional[str] = Field(default=None, max_length=500)


class CertificateOut(BaseModel):
    id: int
    tenant_id: int
    cert_name: str
    cert_no: Optional[str] = None
    valid_until: str
    warn_days: int = 30
    days_remaining: int = 0
    is_expired: bool = False
    is_expiring_soon: bool = False
    remark: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ── 9. /api/reports 契约 ─────────────────────────────────────────────────
class AuditLogItemOut(BaseModel):
    id: int
    tenant_id: Optional[int] = None
    user_id: Optional[int] = None
    action: str
    target: Optional[str] = None
    detail_json: Optional[dict[str, Any]] = None
    ip: Optional[str] = None
    created_at: Optional[str] = None


class AuditLogListOut(BaseModel):
    total: int
    items: list[AuditLogItemOut] = Field(default_factory=list)


# ── 10. /api/model-config 契约 ───────────────────────────────────────────
class ModelConfigUpsertRequest(BaseModel):
    model_config = ConfigDict(extra='ignore')

    base_url: str = Field(min_length=1, max_length=256)
    api_key: Optional[str] = Field(default=None, max_length=512)
    model_flash: str = Field(min_length=1, max_length=128)
    model_flagship: Optional[str] = Field(default=None, max_length=128)
    supports_reasoning_effort: bool = False
    supports_vision: bool = False


class ModelConfigOut(BaseModel):
    configured: bool = False
    base_url: str = ''
    api_key_masked: str = ''
    api_key_last4: str = ''
    model_flash: str = ''
    model_flagship: str = ''
    supports_reasoning_effort: bool = False
    supports_vision: bool = False
    updated_at: Optional[str] = None


class ModelConfigTestOut(BaseModel):
    ok: bool
    latency_ms: int = 0
    model: str
    message: str


DashboardStatsOut.model_rebuild()
