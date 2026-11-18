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
    tenant_name: Optional[str] = None
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
    show_demo_login: bool = False


class TenantOut(BaseModel):
    id: int
    name: str
    is_active: bool = True
    created_at: Optional[str] = None


class TenantCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class TenantUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class AdminUserOut(BaseModel):
    id: int
    username: str
    display_name: str = ''
    role: str
    tenant_id: int
    tenant_name: str = ''
    is_active: bool = True
    last_login_at: Optional[str] = None
    created_at: Optional[str] = None


class AdminUserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    display_name: str = Field(default='', max_length=128)
    role: str = 'member'
    tenant_id: int


class AdminUserUpdateRequest(BaseModel):
    username: Optional[str] = Field(default=None, max_length=64)
    display_name: Optional[str] = Field(default=None, max_length=128)
    role: Optional[str] = None
    tenant_id: Optional[int] = None
    is_active: Optional[bool] = None


class AdminUserResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=1, max_length=128)


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


class PrivacyStatsOut(BaseModel):
    """E40：上传与脱敏只读统计（不展示明文敏感编号）。"""

    id_masked: int = 0
    bank_masked: int = 0


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
    privacy_stats: Optional[PrivacyStatsOut] = None
    delivered_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BidTaskRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=256)


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
class MatchedLedgerCertOut(BaseModel):
    id: int
    cert_name: str
    cert_no: Optional[str] = None
    valid_until: Optional[str] = None
    is_long_term: bool = False
    days_remaining: Optional[int] = None
    status: Literal['valid', 'expiring_soon', 'expired']
    has_attachment: bool = False
    attachment_name: Optional[str] = None


class RequiredCertItemOut(BaseModel):
    cert_name: str
    category: str
    requirement_level: Literal['mandatory', 'bonus'] = 'mandatory'
    source_section: str
    quote: str
    quote_verified: bool = True
    offset: Optional[int] = None
    page: Optional[int] = None
    score_hint: Optional[str] = None
    ledger_status: Literal['valid', 'expiring_soon', 'expired', 'not_in_ledger'] = 'not_in_ledger'
    matched_cert: Optional[MatchedLedgerCertOut] = None
    matched_count: int = 0


class CertLedgerSummaryOut(BaseModel):
    total: int = 0
    mandatory_count: int = 0
    bonus_count: int = 0
    valid_count: int = 0
    expiring_soon_count: int = 0
    expired_count: int = 0
    not_in_ledger_count: int = 0
    reference_date: str
    reference_date_source: Literal['open_date', 'today'] = 'today'


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
    required_certificates: list[RequiredCertItemOut] = Field(default_factory=list)
    cert_ledger_summary: Optional[CertLedgerSummaryOut] = None
    privacy_stats: Optional[PrivacyStatsOut] = None


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


class QaSessionRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=256)


class QaAttachmentOut(BaseModel):
    attachment_id: str
    filename: str
    ext: str
    size_bytes: int
    is_image: bool = False
    ocr_chars: int = 0
    has_text: bool = False
    privacy_stats: Optional[PrivacyStatsOut] = None
    uploaded_at: Optional[str] = None


class QaAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    max_rounds: Optional[int] = Field(default=None, ge=1, le=12)
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
    grounded: bool = True
    fast_path: bool = False
    route: Optional[str] = None


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
    suggested_questions: list[str] = Field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class QaLastTurnWithdrawOut(BaseModel):
    session_id: int
    withdrawn_question: str = ''
    withdrawn_attachment_ids: list[str] = Field(default_factory=list)
    remaining_messages: int = 0


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
    valid_until: Optional[date] = None
    is_long_term: bool = False
    warn_days: int = Field(default=30, ge=1, le=365)
    remark: Optional[str] = Field(default=None, max_length=500)


class CertificateOut(BaseModel):
    id: int
    tenant_id: int
    cert_name: str
    cert_no: Optional[str] = None
    valid_until: Optional[str] = None
    is_long_term: bool = False
    warn_days: int = 30
    days_remaining: Optional[int] = None
    is_expired: bool = False
    is_expiring_soon: bool = False
    remark: Optional[str] = None
    attachment_name: Optional[str] = None
    attachment_size: Optional[int] = None
    has_attachment: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CertificateParseOut(BaseModel):
    cert_name_hint: Optional[str] = None
    cert_name_source: Optional[Literal['ocr', 'filename_fallback']] = None
    cert_no_hint: Optional[str] = None
    valid_until_hint: Optional[str] = None
    is_long_term_hint: bool = False
    ocr_chars: int = 0
    text_excerpt: str = ''

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


# ── 11. /api/similarity 契约（E36 多标书交叉比对）─────────────────────────
class SimilarityBidderOut(BaseModel):
    index: int
    label: str
    filename: str
    chars: int = 0
    net_chars: int = 0
    pages: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)
    media_count: int = 0
    bid_total: Optional[str] = None


class SimilarityPairOut(BaseModel):
    a_index: int
    b_index: int
    a_label: str
    b_label: str
    net_similarity_pct: float = 0.0
    raw_similarity_pct: float = 0.0
    risk_level: Literal['low', 'medium', 'high'] = 'low'
    shared_passage_count: int = 0
    shared_passages: list[dict[str, Any]] = Field(default_factory=list)


class SimilarityFindingOut(BaseModel):
    code: str
    severity: Literal['error', 'warning', 'info'] = 'warning'
    dimension: str
    a_label: str
    b_label: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class SimilarityAiReviewMatchOut(BaseModel):
    a_label: str = ''
    b_label: str = ''
    similarity_type: str = 'semantic_isomorphism'
    reason: str = ''
    a_quote: str = ''
    b_quote: str = ''
    quote_verified: bool = True


class SimilarityAiReviewOut(BaseModel):
    status: str = 'skipped'
    summary: str = ''
    semantic_matches: list[SimilarityAiReviewMatchOut] = Field(default_factory=list)
    false_positive_notes: list[str] = Field(default_factory=list)


class SimilarityReportSummaryOut(BaseModel):
    id: int
    tenant_id: int
    user_id: Optional[int] = None
    title: str
    status: str = 'completed'
    progress: int = 100
    stage_detail: str = ''
    error_msg: Optional[str] = None
    tender_filename: Optional[str] = None
    bidder_count: int = 0
    max_similarity_pct: float = 0.0
    risk_level: Literal['low', 'medium', 'high'] = 'low'
    finding_count: int = 0
    created_at: Optional[str] = None


class SimilarityReportDetailOut(SimilarityReportSummaryOut):
    bidders: list[SimilarityBidderOut] = Field(default_factory=list)
    pair_matrix: list[SimilarityPairOut] = Field(default_factory=list)
    findings: list[SimilarityFindingOut] = Field(default_factory=list)
    ai_review: Optional[SimilarityAiReviewOut] = None
    export_url: str = ''


class SimilarityReportListOut(BaseModel):
    total: int = 0
    items: list[SimilarityReportSummaryOut] = Field(default_factory=list)


# ── 12. /api/composer 与 /clean-bid 契约（E37 + E38）─────────────────────
class SkillLayoutSpecSchema(BaseModel):
    cover_style: Literal['gov_clean', 'navy_business', 'tech_modern'] = 'gov_clean'
    font_zh: str = '仿宋'
    heading_font_zh: str = '黑体'
    font_size_pt: float = 11.5
    line_spacing: float = 1.35
    accent_color_hex: str = '0F172A'
    table_header_bg: str = 'F1F5F9'
    include_toc: bool = True
    append_checklist_at_end: bool = False
    target_section_words: int = 500
    tone: Literal['formal_gov', 'technical_deep', 'concise_direct'] = 'formal_gov'


class ComposerSkillOut(BaseModel):
    id: int
    tenant_id: int
    user_id: Optional[int] = None
    name: str
    description: str = ''
    biz_line: Literal['general', 'security', 'maas'] = 'general'
    source_type: str = 'custom'
    source_url: Optional[str] = None
    writing_instructions: str = ''
    layout_spec: SkillLayoutSpecSchema = Field(default_factory=SkillLayoutSpecSchema)
    is_builtin: bool = False
    is_default: bool = False
    created_at: Optional[str] = None


class ComposerSkillListOut(BaseModel):
    total: int = 0
    items: list[ComposerSkillOut] = Field(default_factory=list)


class ComposerSkillUpsertRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = ''
    biz_line: Literal['general', 'security', 'maas'] = 'general'
    writing_instructions: str = Field(..., min_length=1)
    layout_spec: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False


class SkillImportUrlRequest(BaseModel):
    url_or_repo: str = Field(..., min_length=3, max_length=512)
    allow_gitee_fallback: bool = True


class SkillImportPreviewOut(BaseModel):
    name: str
    description: str = ''
    biz_line: Literal['general', 'security', 'maas'] = 'general'
    source_type: str = 'github'
    source_url: Optional[str] = None
    writing_instructions: str = ''
    layout_spec: SkillLayoutSpecSchema = Field(default_factory=SkillLayoutSpecSchema)
    fetched_from_provider: str = ''
    used_fallback: bool = False


class IndicatorScoringRefOut(BaseModel):
    typical_points: list[str] = Field(default_factory=list)
    weight_hint: str = ''
    high_score_tips: str = ''


class IndicatorEntryOut(BaseModel):
    indicator_id: str
    line: Literal['common', 'security', 'maas']
    category: str = '技术方案'
    indicator_name: str
    standard_response: str
    scoring_ref: IndicatorScoringRefOut = Field(default_factory=IndicatorScoringRefOut)
    param_slots: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    source_ref: str = ''


class LineScoringSummaryOut(BaseModel):
    dimension: str
    typical_weight: str = ''
    key_focus: str = ''


class BizLineIndicatorOverviewOut(BaseModel):
    biz_line: Literal['general', 'security', 'maas'] = 'general'
    biz_line_label: str = ''
    included_lines: list[str] = Field(default_factory=list)
    review_status: str = 'pending_human_review'
    reviewed_notes: list[str] = Field(default_factory=list)
    total_indicators: int = 0
    line_scoring_summary: list[LineScoringSummaryOut] = Field(default_factory=list)
    indicators: list[IndicatorEntryOut] = Field(default_factory=list)


class ComposeDraftSummaryOut(BaseModel):
    id: int
    tenant_id: int
    user_id: Optional[int] = None
    skill_id: Optional[int] = None
    skill_name: str = ''
    biz_line: str = 'general'
    use_indicator_library: bool = False
    title: str
    status: str = 'completed'
    progress: int = 100
    stage_detail: str = ''
    error_msg: Optional[str] = None
    tender_filename: str
    compose_mode: Literal['skeleton_v1', 'knowledge_draft_v2'] = 'skeleton_v1'
    project_name: str = ''
    project_code: str = ''
    open_date: Optional[str] = None
    price_cap: Optional[str] = None
    manual_item_count: int = 0
    scoring_fill_mode: str = 'outline_stub'
    ai_status: str = 'skipped'
    stats: dict[str, Any] = Field(default_factory=dict)
    download_url: str = ''
    created_at: Optional[str] = None


class ComposeDraftDetailOut(ComposeDraftSummaryOut):
    blueprint: dict[str, Any] = Field(default_factory=dict)


class ComposeDraftListOut(BaseModel):
    total: int = 0
    items: list[ComposeDraftSummaryOut] = Field(default_factory=list)


class CleanBidExportRequest(BaseModel):
    replacements: dict[int, str] = Field(default_factory=dict)
    finding_ids: Optional[list[int]] = None


class CleanBidAppliedItemOut(BaseModel):
    finding_id: Optional[int] = None
    code: str = ''
    clause_no: str = ''
    old_text: str = ''
    new_text: str = ''
    occurrences: int = 0


class CleanBidPendingItemOut(BaseModel):
    finding_id: Optional[int] = None
    code: str = ''
    severity: str = 'warning'
    clause_no: str = ''
    quote: str = ''
    message: str = ''
    suggestion: str = ''
    reason: str = ''


class CleanBidExportOut(BaseModel):
    task_id: int
    output_filename: str
    output_path: str
    download_url: str
    applied_count: int = 0
    manual_pending_count: int = 0
    applied_items: list[CleanBidAppliedItemOut] = Field(default_factory=list)
    manual_pending_items: list[CleanBidPendingItemOut] = Field(default_factory=list)


DashboardStatsOut.model_rebuild()
