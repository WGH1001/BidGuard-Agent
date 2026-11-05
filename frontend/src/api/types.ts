/**
 * BidGuard-Agent 前端 TypeScript 类型定义（严格逐字段对齐 FRONTEND.md §2 与 backend/schemas.py）
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
}

// ── 2. 防废标体检任务与发现明细 (/api/checks) ───────────────────────────
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
  detail_json?: Record<string, unknown> | null
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
  llm_status?: 'done' | 'skipped' | string
  suspicious_count?: number
  recommendation?: string
  summary?: string
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
  config_json?: Record<string, unknown> | null
  llm_usage_json?: Record<string, unknown> | null
  delivered_at?: string | null
  created_at?: string | null
  updated_at?: string | null
}

export interface BidTaskListOut {
  total: number
  items: BidTaskOut[]
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
export interface MarkedClauseItem {
  mark: '★' | '▲' | string
  text: string
  start: number
  end: number
  clause_no: string
  kind: 'requirement' | 'usage_note' | 'table_marker' | string
}

export interface InvalidBidClauseItem {
  pattern: 'A' | 'B' | string
  text: string
  start: number
  end: number
  clause_no: string
  promise_doc?: string
}

export interface ExtractChecklistOut {
  filename: string
  source_type: string
  pages: number
  chars: number
  llm_status: 'done' | 'skipped' | string
  chapters: Array<{ index: string; title: string; start: number }>
  marked_clauses: MarkedClauseItem[]
  invalid_bid_clauses: InvalidBidClauseItem[]
  qualification_hits: Array<{ keyword: string; start: number; excerpt: string }>
  compliance_hits: Array<{ keyword: string; start: number; excerpt: string }>
  eval_chapter?: { index: string; title: string; start: number } | null
  scoring_tables: Array<Record<string, unknown>>
  scoring_prose: Array<Record<string, unknown>>
  llm_structured: Array<Record<string, unknown>>
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
  status: string
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
  exclusive_type: string
  confidence: number
  reason: string
  quote_verified: boolean
  llm_status: string
}

export interface RadarConclusionOut {
  recommendation: '值得投' | '谨慎投' | '疑似控标高' | string
  summary: string
}

export interface RadarScanOut {
  task_id: number
  title: string
  llm_status: string
  suspicious_params: SuspiciousParamOut[]
  conclusion: RadarConclusionOut
}

// ── 7. F7 标书专家追问 (/api/qa) ─────────────────────────────────────────
export interface QaQuoteOut {
  source: 'tender' | 'bid' | string
  quote: string
  offset?: number | null
  page?: number | null
  quote_verified: boolean
  note?: string | null
}

export interface QaMessageItem {
  role: 'user' | 'assistant'
  content: string
  mode?: string
  quotes?: QaQuoteOut[]
  steps?: Array<Record<string, unknown>>
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
  created_at?: string | null
  updated_at?: string | null
}

export interface QaAskRequest {
  question: string
  max_rounds?: number
}

export interface QaAskResponseOut {
  session_id: number
  mode: 'react' | 'keyword_fallback' | string
  question: string
  answer: string
  quotes: QaQuoteOut[]
  steps: Array<Record<string, unknown>>
  rounds_used: number
  terminated_by_max_rounds: boolean
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
  valid_until: string
  warn_days: number
  remark?: string | null
}

export interface CertificateOut {
  id: number
  tenant_id: number
  cert_name: string
  cert_no?: string | null
  valid_until: string
  warn_days: number
  days_remaining: number
  is_expired: boolean
  is_expiring_soon: boolean
  remark?: string | null
  created_at?: string | null
  updated_at?: string | null
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

// ── 10. 个人模型配置 (/api/model-config) ─────────────────────────────────
export interface ModelConfigUpsertRequest {
  base_url: string
  api_key?: string
  model_flash: string
  model_flagship?: string
  supports_reasoning_effort: boolean
}

export interface ModelConfigOut {
  configured: boolean
  base_url: string
  api_key_masked: string
  api_key_last4: string
  model_flash: string
  model_flagship: string
  supports_reasoning_effort: boolean
  updated_at?: string | null
}

export interface ModelConfigTestOut {
  ok: boolean
  model: string
  message: string
}

// ── 11. SSE 进度帧类型与页码/偏移格式化工具函数 ─────────────────────────
export interface SSEProgressFrame {
  task_id: number
  status: TaskStatus | string
  progress?: number
  stage_detail?: string
  stats?: BidTaskStats
  error_msg?: string
}

/**
 * FRONTEND.md 铁律 3：docx 页码恒为 null，仅 PDF 有真实页码。
 * 统一格式化位置展示：有页码显示 "第 X 页"，page 为 null 时显示 "字符偏移 @N"。
 */
export function formatPageOrOffset(page?: number | null, offset?: number | null): string {
  if (typeof page === 'number' && page > 0) {
    return `第 ${page} 页`
  }
  if (typeof offset === 'number' && offset >= 0) {
    return `字符偏移 @${offset}`
  }
  return '位置待定位'
}