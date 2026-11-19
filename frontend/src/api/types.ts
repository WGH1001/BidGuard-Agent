/**
 * BidGuard-Agent 前端全局 TypeScript 契约类型定义（E6 完整覆盖版）
 * 严格逐字段对齐 FRONTEND.md §2、backend/schemas.py、E4 法规库与 E6 docx 双轨页码契约
 */

export type UserRole = 'admin' | 'member'
export type TaskType = 'pre_check' | 'radar'
export type TaskStatus =
  | 'created'
  | 'extracting'
  | 'checking'
  | 'grading'
  | 'delivered'
  | 'failed'
  | 'need_manual'
export type SeverityType = 'error' | 'warning' | 'info'
export type ConsequenceType = 'veto' | 'deduction' | 'bonus' | 'none'
export type FeedbackType = 'pending' | 'accepted' | 'rejected'
export type SidebarThemeMode = 'slate' | 'carbon' | 'fjord'

// ── 1. 认证与用户 (/api/auth) ───────────────────────────────────────────
export interface LoginRequest {
  username: string
  password: string
}

export interface UserProfileOut {
  id: number | null
  username: string
  display_name: string
  role: UserRole
  tenant_id: number | null
  tenant_name?: string | null
  is_active: boolean
}

export interface LoginResponse {
  token: string
  user: UserProfileOut
}

export interface ChangePasswordRequest {
  old_password?: string
  new_password: string
}

export interface OkResponse {
  ok: boolean
  message: string
}

export interface HealthOut {
  status: string
  service: string
  version: string
  show_demo_login?: boolean
}

export interface TenantOut {
  id: number
  name: string
  is_active: boolean
  created_at?: string | null
}

export interface TenantCreateRequest {
  name: string
}

export interface TenantUpdateRequest {
  name: string
}

export interface AdminUserOut {
  id: number
  username: string
  display_name: string
  role: UserRole
  tenant_id: number
  tenant_name: string
  is_active: boolean
  last_login_at?: string | null
  created_at?: string | null
}

export interface AdminUserCreateRequest {
  username: string
  password: string
  display_name?: string
  role?: UserRole
  tenant_id: number
}

export interface AdminUserUpdateRequest {
  username?: string
  display_name?: string
  role?: UserRole
  tenant_id?: number
  is_active?: boolean
}

export interface AdminUserResetPasswordRequest {
  new_password: string
}

// ── 2. 防废标体检任务与发现明细 (/api/checks) ───────────────────────────
export interface FindingDetailJson {
  footer_page?: number | null
  is_docx_page?: boolean
  tender_footer_page?: number | null
  tender_is_docx?: boolean
  bid_footer_page?: number | null
  bid_is_docx?: boolean
  [key: string]: unknown
}

export interface BidFindingOut {
  id: number
  task_id: number
  source_module: 'F1' | 'F2' | 'F3' | 'F4' | 'F6' | string
  code: string
  severity: SeverityType
  category?: string | null
  clause_no?: string | null
  quote?: string | null
  bid_quote?: string | null
  quote_offset?: number | null
  quote_verified: boolean
  page?: number | null
  message: string
  suggestion?: string | null
  consequence?: ConsequenceType | string | null
  feedback: FeedbackType
  feedback_note?: string | null
  detail_json?: FindingDetailJson | null
  created_at?: string | null
}

export interface BidTaskStats {
  total_findings?: number
  error_count?: number
  warning_count?: number
  total_requirements?: number
  aligned_hits?: number
  hit_rate?: number
  price_status?: 'OK' | 'NEED_MANUAL' | string
  price_cap?: string | null
  bid_total?: string | null
  reference_bid_total?: string | null
  table_total?: string | null
  bid_total_upper?: string | null
  price_items?: PriceItemInput[]
  llm_status?: 'done' | 'skipped' | string
  suspicious_count?: number
  recommendation?: string
  summary?: string
}

export interface TaskDurationRecord {
  f1_ms?: number | null
  f_parse_ms?: number | null
  f2_ms?: number | null
  f3_ms?: number | null
  f4_ms?: number | null
  grading_ms?: number | null
  total_ms?: number | null
}

export interface PrivacyStatsOut {
  id_masked: number
  bank_masked: number
}

export interface BidTaskOut {
  id: number
  tenant_id: number
  user_id?: number | null
  task_type: TaskType | string
  title: string
  status: TaskStatus | string
  progress: number
  stage_detail?: string | null
  tender_filename?: string | null
  bid_filename?: string | null
  tender_url?: string | null
  bid_url?: string | null
  error_msg?: string | null
  stats_json?: BidTaskStats | null
  price_manual_json?: Record<string, unknown> | null
  config_json?: {
    open_date?: string | null
    open_date_source?: string | null
    [key: string]: unknown
  } | null
  llm_usage_json?: Record<string, unknown> | null
  score_estimate_json?: ScoreEstimateOut | Record<string, unknown> | null
  privacy_stats?: PrivacyStatsOut | null
  duration_json?: TaskDurationRecord | null
  started_at?: string | null
  finished_at?: string | null
  delivered_at?: string | null
  created_at?: string | null
  updated_at?: string | null
}

export interface BidTaskListOut {
  total: number
  items: BidTaskOut[]
}

export interface BidTaskRenamePayload {
  title: string
}

export interface BidTaskDetailOut {
  task: BidTaskOut
  findings: BidFindingOut[]
}

export interface FindingListOut {
  total: number
  items: BidFindingOut[]
}

export interface FindingFeedbackRequest {
  feedback: FeedbackType
  feedback_note?: string
}

export interface PriceItemInput {
  name: string
  qty: string
  unit_price: string
  total_price: string
}

export interface PriceManualBackfillRequest {
  items: PriceItemInput[]
  table_total?: string | null
  bid_total?: string | null
  bid_total_upper?: string | null
  price_cap?: string | null
}

export interface PriceManualBackfillOut {
  task_id: number
  status: string
  price_findings_count: number
  findings: BidFindingOut[]
}

export interface RunTaskTriggerOut {
  task_id: number
  status: string
  message: string
}

export type ScoreItemStatusType = 'evaluated' | 'pending_open_bid' | 'veto_risk' | 'need_llm'

export interface ScoreEvidenceQuoteOut {
  quote: string
  quote_verified: boolean
  offset?: number | null
  page?: number | null
}

export interface ScoreVetoAlertOut {
  source_module: string
  code: string
  severity: string
  clause_no: string
  message: string
  suggestion: string
  consequence: string
}

export interface ScoreCriterionItemOut {
  criterion_id: string
  section: string
  factor: string
  category: string
  evaluation_mode: string
  max_score: string
  estimated_score?: string | null
  status: ScoreItemStatusType
  status_reason?: string | null
  score_ratio_pct?: number | null
  deduction_reasons: string[]
  improvement_suggestions: string[]
  evidence_quotes: ScoreEvidenceQuoteOut[]
}

export interface ScorePriorityItemOut {
  criterion_id: string
  factor: string
  category: string
  max_score: string
  estimated_score?: string | null
  gap_score: string
  status: string
  action_summary: string
  f8_composer_hint: string
}

export interface ScoreEstimateOut {
  task_id: number
  status: 'not_started' | 'queued' | 'running' | 'completed' | 'failed'
  progress: number
  stage_detail: string
  error_msg?: string | null
  disclaimer: string
  llm_status: string
  has_veto_risk: boolean
  veto_alerts: ScoreVetoAlertOut[]
  total_declared_max_score: string
  items_sum_max_score: string
  assessed_max_score: string
  assessed_estimated_score: string
  assessed_ratio_pct?: number | null
  price_pending_max_score: string
  need_llm_max_score: string
  score_scale_warning?: string | null
  items: ScoreCriterionItemOut[]
  top_priorities: ScorePriorityItemOut[]
  updated_at?: string | null
}

// ── 3. 总览大盘 (/api/dashboard) ─────────────────────────────────────────
export interface DefenseMatrixItem {
  module: string
  title: string
  hit_count: number
  status: string
}

export interface CountdownTaskItem {
  task_id: number
  title: string
  task_type: string
  status: string
  open_date?: string | null
  days_left?: number | null
  veto_count: number
  warning_count: number
}

export interface DashboardStatsOut {
  tenant_id: number
  total_projects: number
  pre_check_count: number
  radar_count: number
  veto_intercepted_count: number
  warning_count: number
  deviation_tables_generated: number
  star_recall_rate: number
  expiring_certs_count: number
  defense_matrix: DefenseMatrixItem[]
  countdown_alerts: CountdownTaskItem[]
  recent_tasks: BidTaskOut[]
}

// ── 4. F1 招标红线提取 (/api/extract) ────────────────────────────────────
export interface LlmStructuredClause {
  is_requirement?: boolean
  clause_type?: string
  consequence?: ConsequenceType | string
  clause_no?: string
  full_text?: string
  reason?: string
}

export interface MarkedClauseItem {
  mark: '★' | '▲' | string
  text: string
  full_text?: string
  start: number
  end: number
  page?: number | null
  clause_no: string
  kind: 'requirement' | 'usage_note' | 'table_marker' | string
  consequence?: ConsequenceType | string
  quote_verified?: boolean
  structured?: LlmStructuredClause | null
}

export interface InvalidBidClauseItem {
  pattern: 'A' | 'B' | string
  text: string
  full_text?: string
  start: number
  end: number
  page?: number | null
  clause_no: string
  promise_doc?: string
  consequence?: ConsequenceType | string
  quote_verified?: boolean
  structured?: LlmStructuredClause | null
}

export interface KeywordSectionHitItem {
  keyword: string
  start: number
  page?: number | null
  excerpt: string
}

export interface ScoringTableItem {
  table_index?: number
  row_index?: number
  no?: string
  section?: string
  factor?: string
  detail?: string
  score?: string
  score_max?: number | null
  type?: string
}

export interface ScoringProseItem {
  factor?: string
  detail?: string
  detail_raw?: string
  score?: string
  score_max?: number | null
  evidence_material?: string
  consequence?: ConsequenceType | string
  quote_verified?: boolean
}

export interface MatchedLedgerCertOut {
  id: number
  cert_name: string
  cert_no?: string | null
  valid_until: string | null
  is_long_term?: boolean
  days_remaining: number | null
  status: 'valid' | 'expiring_soon' | 'expired' | string
  has_attachment: boolean
  attachment_name?: string | null
}

export interface RequiredCertItemOut {
  cert_name: string
  category: 'enterprise_qualification' | 'product_cert' | 'staff_cert' | 'bid_bond_or_ca' | string
  requirement_level: 'mandatory' | 'bonus' | string
  source_section: string
  quote: string
  quote_verified: boolean
  offset?: number | null
  page?: number | null
  score_hint?: string | null
  ledger_status: 'valid' | 'expiring_soon' | 'expired' | 'not_in_ledger' | string
  matched_cert?: MatchedLedgerCertOut | null
  matched_count: number
}

export interface CertLedgerSummaryOut {
  total: number
  mandatory_count: number
  bonus_count: number
  valid_count: number
  expiring_soon_count: number
  expired_count: number
  not_in_ledger_count: number
  reference_date: string
  reference_date_source: 'open_date' | 'today' | string
}

export interface BlindBidRequirementItemOut {
  category: string
  summary: string
  clause_no: string
  quote: string
  quote_verified: boolean
  offset: number
  page?: number | null
  recommended_rule?: string | null
}

export interface BlindBidInfoOut {
  is_blind_bid: boolean
  requirements: BlindBidRequirementItemOut[]
  recommended_rules: string[]
  toc_forbidden: boolean
  style_forbidden: boolean
  no_images_strict?: boolean
  image_ban_scope?: string
}

export interface ExtractChecklistOut {
  filename: string
  source_type: string
  pages: number
  chars: number
  open_date?: string | null
  open_date_source?: string | null
  open_date_quote?: string | null
  llm_status: 'done' | 'skipped' | string
  chapters: Array<{ index: string; title: string; start: number }>
  marked_clauses: MarkedClauseItem[]
  invalid_bid_clauses: InvalidBidClauseItem[]
  qualification_hits: KeywordSectionHitItem[]
  compliance_hits: KeywordSectionHitItem[]
  eval_chapter?: { index: string; title: string; start: number } | null
  scoring_tables: ScoringTableItem[]
  scoring_prose: ScoringProseItem[]
  llm_structured: MarkedClauseItem[]
  required_certificates?: RequiredCertItemOut[]
  cert_ledger_summary?: CertLedgerSummaryOut | null
  blind_bid_info?: BlindBidInfoOut | null
  privacy_stats?: PrivacyStatsOut | null
}

// ── 5. F5 偏离表导出 (/api/deviation) ────────────────────────────────────
export interface DeviationRowItem {
  seq: string
  clause_no: string
  tender_requirement: string
  bid_response: string
  deviation: string
  location: string
  is_negative: boolean
  status: 'RESPONSE_OK' | 'MISSING_RESPONSE' | 'CONTRADICTION' | string
}

export interface DeviationExportOut {
  task_id: number
  download_url: string
  output_filename: string
  total_rows: number
  no_deviation_count: number
  negative_deviation_count: number
  missing_count: number
  contradiction_count: number
  rows: DeviationRowItem[]
}

// ── 6. F6 投前控标雷达 (/api/radar) ──────────────────────────────────────
export interface SuspiciousParamOut {
  quote: string
  offset: number
  page?: number | null
  exclusive_type: '品牌绑定' | '资质定向' | '业绩定向' | '参数定向' | string
  confidence: number
  reason: string
  quote_verified: boolean
  llm_status: 'done' | 'skipped' | string
}

export interface RadarConclusionOut {
  recommendation: '值得投' | '谨慎投' | '疑似控标高' | string
  summary: string
}

export interface RadarScanOut {
  task_id: number
  title: string
  open_date?: string | null
  open_date_source?: string | null
  open_date_quote?: string | null
  llm_status: 'done' | 'skipped' | string
  suspicious_params: SuspiciousParamOut[]
  conclusion: RadarConclusionOut
}

// ── 7. F7 标书专家追问 (/api/qa) ─────────────────────────────────────────
export interface ReActStepTraceItem {
  round?: number
  step_no?: number
  batch_index?: number
  batch_size?: number
  thought?: string
  action?: string
  action_input?: unknown
  observation?: unknown
}

export interface QaAttachmentOut {
  attachment_id: string
  filename: string
  ext: string
  size_bytes: number
  is_image: boolean
  ocr_chars: number
  has_text: boolean
  privacy_stats?: PrivacyStatsOut | null
  uploaded_at?: string | null
}

export interface QaAttachmentSummary {
  attachment_id: string
  filename: string
  is_image?: boolean
  ocr_chars?: number
}

export interface QaQuoteOut {
  source: 'tender' | 'bid' | 'attachment' | string
  quote: string
  offset?: number | null
  page?: number | null
  quote_verified: boolean
  note?: string | null
  attachment_id?: string | null
  attachment_name?: string | null
}

export interface QaMessageItem {
  role: 'user' | 'assistant'
  content: string
  mode?: 'react' | 'keyword_fallback' | string
  quotes?: QaQuoteOut[]
  steps?: ReActStepTraceItem[]
  attachment_ids?: string[]
  attachments?: QaAttachmentSummary[]
  grounded?: boolean
  fast_path?: boolean
  route?: string | null
  loading?: boolean
  streaming?: boolean
}

export interface QaSessionCreatePayload {
  task_id?: number
  title?: string
  tender_path?: string
  bid_path?: string
}

export interface QaSessionRenamePayload {
  title: string
}

export interface QaSessionOut {
  id: number
  tenant_id: number
  user_id?: number | null
  task_id?: number | null
  title: string
  tender_path?: string | null
  bid_path?: string | null
  messages_json: QaMessageItem[]
  attachments?: QaAttachmentOut[]
  suggested_questions?: string[]
  created_at?: string | null
  updated_at?: string | null
}

export type ReasoningEffortLevel = 'low' | 'medium' | 'high'

export interface QaAskRequest {
  question: string
  max_rounds?: number
  reasoning_effort?: ReasoningEffortLevel | null
  attachment_ids?: string[]
}

export interface QaAskResponseOut {
  session_id: number
  mode: 'react' | 'keyword_fallback' | string
  question: string
  answer: string
  quotes: QaQuoteOut[]
  steps: ReActStepTraceItem[]
  rounds_used: number
  terminated_by_max_rounds: boolean
  grounded?: boolean
  fast_path?: boolean
  route?: string | null
}

export interface QaLastTurnWithdrawOut {
  session_id: number
  withdrawn_question: string
  withdrawn_attachment_ids: string[]
  remaining_messages: number
}

export type QaStreamErrorType = 'gateway' | 'tool_or_internal'

export interface QaStreamHandlers {
  onStep?: (step: ReActStepTraceItem) => void
  onDelta?: (deltaText: string) => void
  onDone?: (finalRes: QaAskResponseOut) => void
  onError?: (errMsg: string, errorType?: QaStreamErrorType) => void
}

// ── 8. 废标规则与企业证照台账 (/api/rules) ───────────────────────────────
export interface CustomRedlineItem {
  keyword: string
  severity: SeverityType
  consequence: ConsequenceType
}

export interface BidRuleConfigUpsertRequest {
  name: string
  price_cap?: string | number | null
  open_date?: string | null
  buyer_name?: string | null
  required_social_months_json: string[]
  custom_redlines_json: CustomRedlineItem[]
  is_default?: boolean
}

export interface BidRuleConfigOut {
  id: number
  tenant_id: number
  name: string
  price_cap?: string | null
  open_date?: string | null
  buyer_name?: string | null
  required_social_months_json: string[]
  custom_redlines_json: CustomRedlineItem[]
  is_default: boolean
  created_at?: string | null
  updated_at?: string | null
}

export interface CertificateUpsertRequest {
  cert_name: string
  cert_no?: string | null
  valid_until?: string | null
  is_long_term?: boolean
  warn_days: number
  remark?: string | null
}

export interface CertificateOut {
  id: number
  tenant_id: number
  cert_name: string
  cert_no?: string | null
  valid_until: string | null
  is_long_term?: boolean
  warn_days: number
  days_remaining: number | null
  is_expired: boolean
  is_expiring_soon: boolean
  remark?: string | null
  attachment_name?: string | null
  attachment_size?: number | null
  has_attachment?: boolean
  created_at?: string | null
  updated_at?: string | null
}

export interface CertificateParseOut {
  cert_name_hint?: string | null
  cert_name_source?: 'ocr' | 'filename_fallback' | null
  cert_no_hint?: string | null
  valid_until_hint?: string | null
  is_long_term_hint?: boolean
  ocr_chars: number
  text_excerpt: string
}

// ── 9. 交付中心与审计日志 (/api/reports) ─────────────────────────────────
export interface AuditLogItemOut {
  id: number
  tenant_id?: number | null
  user_id?: number | null
  action: string
  target?: string | null
  detail_json?: Record<string, unknown> | null
  ip?: string | null
  created_at?: string | null
}

export interface AuditLogListOut {
  total: number
  items: AuditLogItemOut[]
}
export type DeliverIncludeKey = 'original' | 'redline' | 'deviation' | 'signoff'

export interface BatchDeliverZipRequest {
  task_ids: number[]
  include: DeliverIncludeKey[]
}

export interface BatchDeliverFailedItem {
  task_id: number
  reason: string
}

export interface BatchDeliverZipOut {
  download_url: string
  zip_path: string
  output_filename: string
  succeeded_task_ids: number[]
  failed: BatchDeliverFailedItem[]
}

// ── 10. 个人模型配置 (/api/model-config) ─────────────────────────────────
export interface ModelConfigUpsertRequest {
  base_url: string
  api_key?: string
  model_flash: string
  model_flagship?: string
  model_embedding?: string
  supports_reasoning_effort: boolean
  supports_vision?: boolean
}

export interface ModelConfigOut {
  configured: boolean
  base_url: string
  api_key_masked: string
  api_key_last4: string
  model_flash: string
  model_flagship: string
  model_embedding?: string
  supports_reasoning_effort: boolean
  supports_vision?: boolean
  updated_at?: string | null
}

export interface ModelConfigTestOut {
  ok: boolean
  latency_ms?: number
  model: string
  message: string
  embedding_ok?: boolean | null
  embedding_message?: string | null
}

// ── 11. SSE 进度帧与通用格式化工具函数 ───────────────────────────────────
export interface SSEProgressFrame {
  task_id: number
  status: TaskStatus | string
  progress?: number
  stage_detail?: string
  stats?: BidTaskStats
  error_msg?: string
  started_at?: string | null
  updated_at?: string | null
}

// ── 12. E4 招投标法规库与法条三态真伪核验 (/api/regulations) ─────────────
export type ArticleVerifyStatus = 'exact_match' | 'fuzzy_hint' | 'unverifiable'

export interface RegulationLawOut {
  id: number
  name: string
  issuing_body?: string | null
  effective_date?: string | null
  source_url?: string | null
  article_count: number
  content_hash: string
  is_seed: boolean
  fetched_at?: string | null
  updated_at?: string | null
}

export interface RegulationArticleOut {
  id: number
  law_id: number
  article_no: string
  article_title?: string | null
  content: string
  content_hash: string
}

export interface RegulationLawDetailOut {
  law: RegulationLawOut
  total_articles: number
  page: number
  page_size: number
  articles: RegulationArticleOut[]
}

export interface VerifyArticleOut {
  status: ArticleVerifyStatus | string
  verified: boolean
  law?: string | null
  article_no?: string | null
  official_content?: string | null
  similarity: number
  badge: string
  message: string
  llm_diff_hint?: string | null
}

export interface RegulationImportOut {
  law_id: number
  law_name: string
  article_count: number
  content_hash: string
  added_count: number
  modified_count: number
  unchanged_count: number
}

export interface CrawlSourceResultItem {
  name: string
  url: string
  status: string
  updated: boolean
  article_count: number
  added_count: number
  modified_count: number
  unchanged_count: number
  message: string
}

export interface RegulationCrawlOut {
  total_sources: number
  updated_laws_count: number
  unconfigured_count: number
  results: CrawlSourceResultItem[]
}

// ── 13. E36 多标书交叉比对 (/api/similarity) ─────────────────────────────
export type SimilarityRiskLevel = 'low' | 'medium' | 'high'

export interface SimilarityBidderOut {
  index: number
  label: string
  filename: string
  chars: number
  net_chars: number
  pages: number
  metadata: Record<string, unknown>
  media_count: number
  bid_total?: string | null
}

export interface SimilaritySharedPassageItem {
  norm_chars: number
  a_quote: string
  a_offset: number
  a_page?: number | null
  b_quote: string
  b_offset: number
  b_page?: number | null
}

export interface SimilarityPairOut {
  a_index: number
  b_index: number
  a_label: string
  b_label: string
  net_similarity_pct: number
  raw_similarity_pct: number
  risk_level: SimilarityRiskLevel
  shared_passage_count: number
  shared_passages: SimilaritySharedPassageItem[]
}

export interface SimilarityFindingOut {
  code: string
  severity: SeverityType
  dimension: string
  a_label: string
  b_label: string
  message: string
  detail: Record<string, unknown>
}

export interface SimilarityAiReviewMatchOut {
  a_label: string
  b_label: string
  similarity_type: string
  reason: string
  a_quote: string
  b_quote: string
  quote_verified: boolean
}

export interface SimilarityAiReviewOut {
  status: string
  summary: string
  semantic_matches: SimilarityAiReviewMatchOut[]
  false_positive_notes: string[]
}

export interface SimilarityReportSummaryOut {
  id: number
  tenant_id: number
  user_id?: number | null
  title: string
  status?: string
  progress?: number
  stage_detail?: string
  error_msg?: string | null
  tender_filename?: string | null
  bidder_count: number
  max_similarity_pct: number
  risk_level: SimilarityRiskLevel
  finding_count: number
  created_at?: string | null
}

export interface SimilarityReportDetailOut extends SimilarityReportSummaryOut {
  bidders: SimilarityBidderOut[]
  pair_matrix: SimilarityPairOut[]
  findings: SimilarityFindingOut[]
  ai_review?: SimilarityAiReviewOut | null
  export_url: string
}

export interface SimilarityReportListOut {
  total: number
  items: SimilarityReportSummaryOut[]
}

export type ComposeModeType = 'skeleton_v1' | 'knowledge_draft_v2'

export type BizLineType = 'general' | 'security' | 'maas'

export interface SkillLayoutSpec {
  cover_style: 'gov_clean' | 'navy_business' | 'tech_modern'
  font_zh: string
  heading_font_zh: string
  font_size_pt: number
  line_spacing: number
  accent_color_hex: string
  table_header_bg: string
  include_toc: boolean
  append_checklist_at_end: boolean
  target_section_words: number
  tone: 'formal_gov' | 'technical_deep' | 'concise_direct'
}

export interface ComposerSkillOut {
  id: number
  tenant_id: number
  user_id?: number | null
  name: string
  description: string
  biz_line: BizLineType
  source_type: string
  source_url?: string | null
  writing_instructions: string
  layout_spec: SkillLayoutSpec
  is_builtin: boolean
  is_default: boolean
  created_at?: string | null
}

export interface ComposerSkillListOut {
  total: number
  items: ComposerSkillOut[]
}

export interface ComposerSkillUpsertPayload {
  name: string
  description: string
  biz_line?: BizLineType
  writing_instructions: string
  layout_spec: Partial<SkillLayoutSpec>
  is_default?: boolean
}

export interface SkillImportPreviewOut {
  name: string
  description: string
  biz_line: BizLineType
  source_type: string
  source_url?: string | null
  writing_instructions: string
  layout_spec: SkillLayoutSpec
  fetched_from_provider: string
  used_fallback: boolean
}

export interface IndicatorScoringRefOut {
  typical_points: string[]
  weight_hint: string
  high_score_tips: string
}

export interface IndicatorEntryOut {
  indicator_id: string
  line: 'common' | 'security' | 'maas'
  category: string
  indicator_name: string
  standard_response: string
  scoring_ref: IndicatorScoringRefOut
  param_slots: string[]
  keywords: string[]
  source_ref: string
}

export interface LineScoringSummaryOut {
  dimension: string
  typical_weight: string
  key_focus: string
}

export interface BizLineIndicatorOverviewOut {
  biz_line: BizLineType
  biz_line_label: string
  included_lines: string[]
  review_status: string
  reviewed_notes: string[]
  total_indicators: number
  line_scoring_summary: LineScoringSummaryOut[]
  indicators: IndicatorEntryOut[]
}

export interface ComposeManualItem {
  section_id: string
  field: string
  reason: string
  hint: string
}

export interface ComposeBlueprintSection {
  section_id: string
  title: string
  slot_type: string
  fill_mode: string
  tender_requirements: Array<Record<string, unknown>>
  content_blocks: Array<Record<string, unknown>>
  needs_manual_items: ComposeManualItem[]
  source_refs: Array<Record<string, unknown>>
}

export interface ComposeBlueprintData {
  project_name: string
  project_code: string
  buyer_name: string
  open_date?: string | null
  open_date_source?: string | null
  price_cap?: string | null
  package_no?: number | null
  compose_mode: ComposeModeType
  tender_filename: string
  skill_id?: number | null
  skill_name?: string
  skill_layout?: SkillLayoutSpec
  ai_status?: string
  planned_chapters?: Array<Record<string, unknown>>
  sections: ComposeBlueprintSection[]
  manual_checklist: ComposeManualItem[]
  cert_match_summary: Record<string, unknown>
  stats: Record<string, unknown>
  blind_bid_mode?: boolean
  blind_rules?: string[]
}

export interface ComposeDraftSummaryOut {
  id: number
  tenant_id: number
  user_id?: number | null
  skill_id?: number | null
  skill_name?: string
  biz_line?: string
  use_indicator_library?: boolean
  blind_bid_mode?: boolean
  title: string
  status?: string
  progress?: number
  stage_detail?: string
  error_msg?: string | null
  tender_filename: string
  compose_mode: ComposeModeType
  project_name: string
  project_code: string
  open_date?: string | null
  price_cap?: string | null
  manual_item_count: number
  scoring_fill_mode: string
  ai_status?: string
  stats: Record<string, unknown>
  download_url: string
  created_at?: string | null
}

export interface ComposeDraftDetailOut extends ComposeDraftSummaryOut {
  blueprint: ComposeBlueprintData | Record<string, unknown>
}

export interface ComposeDraftListOut {
  total: number
  items: ComposeDraftSummaryOut[]
}

export interface CleanBidAppliedItemOut {
  finding_id?: number | null
  code: string
  clause_no: string
  old_text: string
  new_text: string
  occurrences: number
}

export interface CleanBidPendingItemOut {
  finding_id?: number | null
  code: string
  severity: string
  clause_no: string
  quote: string
  message: string
  suggestion: string
  reason: string
}

export interface CleanBidExportOut {
  task_id: number
  output_filename: string
  output_path: string
  download_url: string
  applied_count: number
  manual_pending_count: number
  applied_items: CleanBidAppliedItemOut[]
  manual_pending_items: CleanBidPendingItemOut[]
}

export interface PageFormatOptions {
  isDocx?: boolean
  footerPage?: number | null
}

/**
 * E6 扩展版页码/偏移格式化：
 * - docx 且有页码：返回 "第X页(印刷页Y·估算)" 或 "第X页(物理页·估算)"；
 * - PDF 且有页码：返回 "第 X 页"；
 * - 无页码但有字符偏移：返回 "字符偏移 @N"；
 * - 两者皆无：返回 "—"。
 */
export function formatPageOrOffset(
  page?: number | null,
  offset?: number | null,
  options?: PageFormatOptions,
): string {
  if (typeof page === 'number' && page > 0) {
    const fPage = options?.footerPage
    if (typeof fPage === 'number' && fPage > 0) {
      return `第${page}页(印刷页${fPage}·估算)`
    }
    if (options?.isDocx) {
      return `第${page}页(物理页·估算)`
    }
    return `第 ${page} 页`
  }
  if (typeof offset === 'number' && offset >= 0) {
    return `字符偏移 @${offset}`
  }
  return '—'
}

export function formatMoneyDisplay(val?: string | number | null): string {
  if (val === null || val === undefined || val === '') return '—'
  const cleaned = String(val).replace(/,/g, '').trim()
  const num = Number(cleaned)
  if (!Number.isFinite(num)) return String(val)
  return num.toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}
