import { del, get, getStoredToken, patch, post, put } from './http'
import { downloadFromApi } from './download'
import type {
  AdminUserCreateRequest,
  AdminUserOut,
  AdminUserResetPasswordRequest,
  AdminUserUpdateRequest,
  AuditLogListOut,
  BatchDeliverZipOut,
  BidFindingOut,
  BidRuleConfigOut,
  BidRuleConfigUpsertRequest,
  BidTaskDetailOut,
  BidTaskListOut,
  BidTaskOut,
  BidTaskRenamePayload,
  BizLineIndicatorOverviewOut,
  BizLineType,
  CertificateOut,
  CertificateParseOut,
  CertificateUpsertRequest,
  ChangePasswordRequest,
  CleanBidExportOut,
  ComposeDraftDetailOut,
  ComposeDraftListOut,
  ComposerSkillListOut,
  ComposerSkillOut,
  ComposerSkillUpsertPayload,
  DashboardStatsOut,
  DeliverIncludeKey,
  DeviationExportOut,
  ExtractChecklistOut,
  FindingFeedbackRequest,
  FindingListOut,
  HealthOut,
  LoginRequest,
  LoginResponse,
  ModelConfigOut,
  ModelConfigTestOut,
  ModelConfigUpsertRequest,
  OkResponse,
  PriceManualBackfillOut,
  PriceManualBackfillRequest,
  QaAskRequest,
  QaAskResponseOut,
  ScoreEstimateOut,
  QaLastTurnWithdrawOut,
  QaSessionCreatePayload,
  QaSessionOut,
  QaSessionRenamePayload,
  RadarScanOut,
  RegulationCrawlOut,
  RegulationImportOut,
  RegulationLawDetailOut,
  RegulationLawOut,
  RunTaskTriggerOut,
  SimilarityReportDetailOut,
  SimilarityReportListOut,
  SkillImportPreviewOut,
  TenantCreateRequest,
  TenantOut,
  TenantUpdateRequest,
  UserProfileOut,
  VerifyArticleOut,
  QaAttachmentOut,
  QaStreamErrorType,
  QaStreamHandlers,
  ReActStepTraceItem
} from './types'

export const ALLOWED_DOC_EXTS = ['.docx', '.pdf', '.txt'] as const
export const MAX_UPLOAD_SIZE_BYTES = 100 * 1024 * 1024 // 100MB

/**
 * 上传文件前端预校验（铁律：仅允许 .docx/.pdf/.txt 且大小 <= 100MB）
 */
export function validateUploadFile(file: File): { valid: boolean; message: string } {
  const name = (file.name || '').trim()
  const dotIdx = name.lastIndexOf('.')
  const ext = dotIdx >= 0 ? name.slice(dotIdx).toLowerCase() : ''
  if (!ALLOWED_DOC_EXTS.includes(ext as (typeof ALLOWED_DOC_EXTS)[number])) {
    return {
      valid: false,
      message: `不支持的文件格式「${ext || '未知'}」，仅允许上传 .docx / .pdf / .txt 文件`,
    }
  }
  if (file.size <= 0) {
    return { valid: false, message: `文件「${name}」内容为空` }
  }
  if (file.size > MAX_UPLOAD_SIZE_BYTES) {
    return { valid: false, message: `文件「${name}」超过 100MB 大小上限` }
  }
  return { valid: true, message: '' }
}

export const ALLOWED_QA_ATTACHMENT_EXTS = [
  '.docx',
  '.pdf',
  '.txt',
  '.md',
  '.png',
  '.jpg',
  '.jpeg',
] as const
export const MAX_QA_ATTACHMENT_BYTES = 20 * 1024 * 1024 // 20MB

export function validateQaAttachmentFile(file: File): { valid: boolean; message: string } {
  const name = (file.name || '').trim()
  const dotIdx = name.lastIndexOf('.')
  const ext = dotIdx >= 0 ? name.slice(dotIdx).toLowerCase() : ''
  if (!ALLOWED_QA_ATTACHMENT_EXTS.includes(ext as (typeof ALLOWED_QA_ATTACHMENT_EXTS)[number])) {
    return {
      valid: false,
      message: `不支持的附件格式「${ext || '未知'}」，仅支持 .docx/.pdf/.txt/.md/.png/.jpg/.jpeg`,
    }
  }
  if (file.size <= 0) {
    return { valid: false, message: `附件「${name}」内容为空` }
  }
  if (file.size > MAX_QA_ATTACHMENT_BYTES) {
    return { valid: false, message: `附件「${name}」超过 20MB 单文件上限` }
  }
  return { valid: true, message: '' }
}

export const authApi = {
  health(): Promise<HealthOut> {
    return get<HealthOut>('/auth/health')
  },
  login(payload: LoginRequest): Promise<LoginResponse> {
    return post<LoginResponse>('/auth/login', payload)
  },
  me(): Promise<UserProfileOut> {
    return get<UserProfileOut>('/auth/me')
  },
  changePassword(payload: ChangePasswordRequest): Promise<OkResponse> {
    return post<OkResponse>('/auth/change_password', payload)
  },
  listTenants(): Promise<TenantOut[]> {
    return get<TenantOut[]>('/auth/tenants')
  },
  createTenant(payload: TenantCreateRequest): Promise<TenantOut> {
    return post<TenantOut>('/auth/tenants', payload)
  },
  updateTenant(tenantId: number, payload: TenantUpdateRequest): Promise<TenantOut> {
    return patch<TenantOut>(`/auth/tenants/${tenantId}`, payload)
  },
  listUsers(tenantId?: number): Promise<AdminUserOut[]> {
    return get<AdminUserOut[]>(
      '/auth/users',
      tenantId !== undefined ? { params: { tenant_id: tenantId } } : undefined,
    )
  },
  createUser(payload: AdminUserCreateRequest): Promise<AdminUserOut> {
    return post<AdminUserOut>('/auth/users', payload)
  },
  updateUser(userId: number, payload: AdminUserUpdateRequest): Promise<AdminUserOut> {
    return patch<AdminUserOut>(`/auth/users/${userId}`, payload)
  },
  resetUserPassword(userId: number, payload: AdminUserResetPasswordRequest): Promise<OkResponse> {
    return post<OkResponse>(`/auth/users/${userId}/reset-password`, payload)
  },
}

export const dashboardApi = {
  getStats(): Promise<DashboardStatsOut> {
    return get<DashboardStatsOut>('/dashboard')
  },
}

export const checksApi = {
  create(payload: {
    tenderFile: File
    bidFile: File
    title?: string
    openDate?: string
    buyerName?: string
    autoRun?: boolean
  }): Promise<BidTaskOut> {
    const fd = new FormData()
    fd.append('tender_file', payload.tenderFile, payload.tenderFile.name)
    fd.append('bid_file', payload.bidFile, payload.bidFile.name)
    if (payload.title && payload.title.trim()) {
      fd.append('title', payload.title.trim())
    }
    if (payload.openDate && payload.openDate.trim()) {
      fd.append('open_date', payload.openDate.trim())
    }
    if (payload.buyerName && payload.buyerName.trim()) {
      fd.append('buyer_name', payload.buyerName.trim())
    }
    fd.append('auto_run', payload.autoRun ? 'true' : 'false')
    return post<BidTaskOut>('/checks', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  list(params?: {
    status?: string
    task_type?: string
    page?: number
    page_size?: number
  }): Promise<BidTaskListOut> {
    return get<BidTaskListOut>('/checks', { params })
  },
  detail(taskId: number): Promise<BidTaskDetailOut> {
    return get<BidTaskDetailOut>(`/checks/${taskId}`)
  },
  run(taskId: number, sync = false): Promise<RunTaskTriggerOut> {
    return post<RunTaskTriggerOut>(`/checks/${taskId}/run`, undefined, {
      params: { sync },
    })
  },
  cancel(taskId: number): Promise<OkResponse> {
    return post<OkResponse>(`/checks/${taskId}/cancel`)
  },
  rename(taskId: number, payload: BidTaskRenamePayload): Promise<BidTaskOut> {
    return patch<BidTaskOut>(`/checks/${taskId}`, payload)
  },
  findings(
    taskId: number,
    params?: { source_module?: string; severity?: string },
  ): Promise<FindingListOut> {
    return get<FindingListOut>(`/checks/${taskId}/findings`, { params })
  },
  feedback(
    taskId: number,
    findingId: number,
    payload: FindingFeedbackRequest,
  ): Promise<BidFindingOut> {
    return post<BidFindingOut>(`/checks/${taskId}/findings/${findingId}/feedback`, payload)
  },
  priceManual(taskId: number, payload: PriceManualBackfillRequest): Promise<PriceManualBackfillOut> {
    return post<PriceManualBackfillOut>(`/checks/${taskId}/price-manual`, payload)
  },
  runScoreEstimate(taskId: number, asyncRun = true): Promise<ScoreEstimateOut> {
    return post<ScoreEstimateOut>(`/checks/${taskId}/score-estimate`, undefined, {
      params: { async_run: asyncRun },
      timeout: 300000,
    })
  },
  getScoreEstimate(taskId: number): Promise<ScoreEstimateOut> {
    return get<ScoreEstimateOut>(`/checks/${taskId}/score-estimate`)
  },
  downloadScoreEstimateReport(
    taskId: number,
    fallbackName = `${taskId}_标书预估得分与提分诊断报告.docx`,
  ): Promise<string> {
    return downloadFromApi(`/checks/${taskId}/score-estimate/export`, fallbackName)
  },
  remove(taskId: number): Promise<OkResponse> {
    return del<OkResponse>(`/checks/${taskId}`)
  },
  downloadStoredFile(fileUrl: string, fallbackName: string): Promise<string> {
    return downloadFromApi(fileUrl, fallbackName)
  },
}

export const extractApi = {
  extract(file: File, signal?: AbortSignal): Promise<ExtractChecklistOut> {
    const fd = new FormData()
    fd.append('file', file, file.name)
    return post<ExtractChecklistOut>('/extract', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 600000, // E19：F1 大标书 LLM 结构化提取单独放宽至 10 分钟（600000ms），其余 API 保持 3 分钟
      signal,
    })
  },
}

export const deviationApi = {
  /** 获取偏离表：产物已存在时后端直接复用（秒回），首次才生成 */
  get(taskId: number): Promise<DeviationExportOut> {
    return get<DeviationExportOut>(`/deviation/${taskId}`)
  },
  /** 强制重新生成（全量重算，仅在用户显式点击"重新生成"时调用） */
  generate(taskId: number): Promise<DeviationExportOut> {
    return post<DeviationExportOut>(`/deviation/${taskId}`)
  },
  downloadByUrl(downloadUrl: string, fallbackName = '技术与商务偏离表.docx'): Promise<string> {
    return downloadFromApi(downloadUrl, fallbackName)
  },
  download(taskId: number, fallbackName = `${taskId}_偏离表.docx`): Promise<string> {
    return downloadFromApi(`/deviation/${taskId}/download`, fallbackName)
  },
}

export const radarApi = {
  scan(tenderFile: File, title?: string, signal?: AbortSignal): Promise<RadarScanOut> {
    const fd = new FormData()
    fd.append('tender_file', tenderFile, tenderFile.name)
    if (title && title.trim()) {
      fd.append('title', title.trim())
    }
    return post<RadarScanOut>('/radar', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
      signal,
    })
  },
  list(): Promise<BidTaskListOut> {
    return get<BidTaskListOut>('/radar')
  },
  detail(taskId: number): Promise<RadarScanOut> {
    return get<RadarScanOut>(`/radar/${taskId}`)
  },
  rescan(taskId: number, signal?: AbortSignal): Promise<RadarScanOut> {
    return post<RadarScanOut>(`/radar/${taskId}/rescan`, undefined, { signal })
  },
  remove(taskId: number): Promise<OkResponse> {
    return del<OkResponse>(`/radar/${taskId}`)
  },
}

export const qaApi = {
  uploadAttachment(sessionId: number, file: File): Promise<QaAttachmentOut> {
    const fd = new FormData()
    fd.append('file', file, file.name)
    return post<QaAttachmentOut>(`/qa/sessions/${sessionId}/attachments`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  deleteAttachment(sessionId: number, attachmentId: string): Promise<OkResponse> {
    return del<OkResponse>(`/qa/sessions/${sessionId}/attachments/${attachmentId}`)
  },
  createSession(payload: QaSessionCreatePayload): Promise<QaSessionOut> {
    return post<QaSessionOut>('/qa/sessions', payload)
  },
  listSessions(taskId?: number): Promise<QaSessionOut[]> {
    return get<QaSessionOut[]>('/qa/sessions', {
      params: taskId != null ? { task_id: taskId } : undefined,
    })
  },
  getSession(sessionId: number): Promise<QaSessionOut> {
    return get<QaSessionOut>(`/qa/sessions/${sessionId}`)
  },
  renameSession(sessionId: number, payload: QaSessionRenamePayload): Promise<QaSessionOut> {
    return patch<QaSessionOut>(`/qa/sessions/${sessionId}`, payload)
  },
  deleteSession(sessionId: number): Promise<OkResponse> {
    return del<OkResponse>(`/qa/sessions/${sessionId}`)
  },
  ask(sessionId: number, payload: QaAskRequest): Promise<QaAskResponseOut> {
    return post<QaAskResponseOut>(`/qa/sessions/${sessionId}/ask`, payload)
  },
  askStream(
    sessionId: number,
    payload: QaAskRequest,
    handlers: QaStreamHandlers,
  ): () => void {
    const controller = new AbortController()
    const token = getStoredToken()
    let terminalEmitted = false

    const processSseBlock = (rawBlock: string): void => {
      if (!rawBlock.trim()) return
      let evName = 'message'
      const dataLines: string[] = []
      for (const line of rawBlock.split(/\r?\n/)) {
        if (line.startsWith('event:')) {
          evName = line.slice(6).trim()
        } else if (line.startsWith('data:')) {
          dataLines.push(line.slice(5).trim())
        }
      }
      if (dataLines.length === 0) return
      const parsed = JSON.parse(dataLines.join('\n'))
      if (evName === 'step') {
        handlers.onStep?.(parsed as ReActStepTraceItem)
      } else if (evName === 'answer_delta') {
        handlers.onDelta?.(String((parsed as { text?: string }).text || ''))
      } else if (evName === 'done') {
        terminalEmitted = true
        handlers.onDone?.(parsed as QaAskResponseOut)
      } else if (evName === 'error') {
        terminalEmitted = true
        const errObj = parsed as { detail?: string; error_type?: QaStreamErrorType }
        const errType: QaStreamErrorType | undefined =
          errObj.error_type === 'gateway' || errObj.error_type === 'tool_or_internal'
            ? errObj.error_type
            : undefined
        handlers.onError?.(String(errObj.detail || '流式问答异常'), errType)
      }
    }

    void (async () => {
      try {
        const resp = await fetch(`/api/qa/sessions/${sessionId}/ask/stream`, {
          method: 'POST',
          headers: {
            Accept: 'text/event-stream',
            'Content-Type': 'application/json',
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify(payload),
          signal: controller.signal,
        })

        if (!resp.ok || !resp.body) {
          const httpErr = new Error(`HTTP ${resp.status}`) as Error & { errorType?: QaStreamErrorType }
          if (resp.status === 502 || resp.status === 503 || resp.status === 504) {
            httpErr.errorType = 'gateway'
          } else if (resp.status === 500) {
            httpErr.errorType = 'tool_or_internal'
          }
          throw httpErr
        }

        const reader = resp.body.getReader()
        const decoder = new TextDecoder('utf-8')
        let buffer = ''

        while (true) {
          const { value, done } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          const blocks = buffer.split(/\r?\n\r?\n/)
          buffer = blocks.pop() || ''

          for (const rawBlock of blocks) {
            processSseBlock(rawBlock)
          }
        }
        buffer += decoder.decode()
        if (buffer.trim()) {
          processSseBlock(buffer)
        }
        if (!terminalEmitted && !controller.signal.aborted) {
          handlers.onError?.('流式响应未正常结束', 'gateway')
        }
      } catch (err: unknown) {
        if ((err as { name?: string })?.name === 'AbortError') return
        const typedErr = err as { errorType?: QaStreamErrorType; message?: string }
        handlers.onError?.(
          err instanceof Error ? err.message : '流式连接中断',
          typedErr?.errorType || 'gateway',
        )
      }
    })()

    return () => {
      controller.abort()
    }
  },
  deleteLastTurn(sessionId: number): Promise<QaLastTurnWithdrawOut> {
    return del<QaLastTurnWithdrawOut>(`/qa/sessions/${sessionId}/last-turn`)
  },
}

export const rulesApi = {
  listRules(): Promise<BidRuleConfigOut[]> {
    return get<BidRuleConfigOut[]>('/rules')
  },
  saveRule(payload: BidRuleConfigUpsertRequest): Promise<BidRuleConfigOut> {
    return put<BidRuleConfigOut>('/rules', payload)
  },
  listCertificates(expiringOnly = false): Promise<CertificateOut[]> {
    return get<CertificateOut[]>('/rules/certificates', {
      params: { expiring_only: expiringOnly },
    })
  },
  createCertificate(payload: CertificateUpsertRequest): Promise<CertificateOut> {
    return post<CertificateOut>('/rules/certificates', payload)
  },
  updateCertificate(certId: number, payload: CertificateUpsertRequest): Promise<CertificateOut> {
    return put<CertificateOut>(`/rules/certificates/${certId}`, payload)
  },
  deleteCertificate(certId: number): Promise<OkResponse> {
    return del<OkResponse>(`/rules/certificates/${certId}`)
  },
  parseCertificateFile(file: File): Promise<CertificateParseOut> {
    const fd = new FormData()
    fd.append('file', file, file.name)
    return post<CertificateParseOut>('/rules/certificates/parse-file', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  uploadCertificateAttachment(certId: number, file: File): Promise<CertificateOut> {
    const fd = new FormData()
    fd.append('file', file, file.name)
    return post<CertificateOut>(`/rules/certificates/${certId}/attachment`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  downloadCertificateAttachment(certId: number, fallbackName = `certificate_${certId}`): Promise<string> {
    return downloadFromApi(`/rules/certificates/${certId}/attachment`, fallbackName)
  },
  deleteCertificateAttachment(certId: number): Promise<CertificateOut> {
    return del<CertificateOut>(`/rules/certificates/${certId}/attachment`)
  },
}

export const reportsApi = {
  downloadRedline(taskId: number, fallbackName = `${taskId}_标红原件.docx`): Promise<string> {
    return downloadFromApi(`/reports/${taskId}/redline`, fallbackName)
  },
  exportCleanBid(
    taskId: number,
    payload: { replacements?: Record<number, string>; finding_ids?: number[] } = {},
  ): Promise<CleanBidExportOut> {
    return post<CleanBidExportOut>(`/reports/${taskId}/clean-bid`, {
      replacements: payload.replacements || {},
      ...(payload.finding_ids ? { finding_ids: payload.finding_ids } : {}),
    })
  },
  downloadCleanBid(
    taskId: number,
    fallbackName = `${taskId}_修订定稿版投标书.docx`,
  ): Promise<string> {
    return downloadFromApi(`/reports/${taskId}/clean-bid/download`, fallbackName)
  },
  downloadSignoff(
    taskId: number,
    fallbackName = `${taskId}_封标前人工终核签字确认单.docx`,
  ): Promise<string> {
    return downloadFromApi(`/reports/${taskId}/signoff`, fallbackName)
  },
  downloadDeliverZip(taskId: number, fallbackName = `${taskId}_封标交付包.zip`): Promise<string> {
    return downloadFromApi(`/reports/${taskId}/deliver.zip`, fallbackName)
  },
  batchDeliverZip(payload: {
    task_ids: number[]
    include: DeliverIncludeKey[]
  }): Promise<BatchDeliverZipOut> {
    return post<BatchDeliverZipOut>('/reports/batch-zip', payload)
  },
  listAuditLogs(params?: {
    action?: string
    page?: number
    page_size?: number
  }): Promise<AuditLogListOut> {
    return get<AuditLogListOut>('/reports/audit-logs', { params })
  },
  exportAuditLogsCsv(action?: string): Promise<string> {
    const clean = action?.trim()
    const qs = clean ? `?action=${encodeURIComponent(clean)}` : ''
    return downloadFromApi(`/reports/audit-logs/export${qs}`, 'audit_logs.csv')
  },
}

export const auditApi = {
  listLogs(params?: {
    action?: string
    page?: number
    page_size?: number
  }): Promise<AuditLogListOut> {
    return reportsApi.listAuditLogs(params)
  },
  exportLogsCsv(action?: string): Promise<string> {
    return reportsApi.exportAuditLogsCsv(action)
  },
}

export const modelConfigApi = {
  get(): Promise<ModelConfigOut> {
    return get<ModelConfigOut>('/model-config')
  },
  save(payload: ModelConfigUpsertRequest): Promise<ModelConfigOut> {
    return put<ModelConfigOut>('/model-config', payload)
  },
  remove(): Promise<OkResponse> {
    return del<OkResponse>('/model-config')
  },
  test(payload: ModelConfigUpsertRequest): Promise<ModelConfigTestOut> {
    return post<ModelConfigTestOut>('/model-config/test', payload)
  },
}
/** E4 招投标法规库与法条核验 API (/api/regulations) */
export const regulationsApi = {
  listLaws(): Promise<RegulationLawOut[]> {
    return get<RegulationLawOut[]>('/regulations/laws')
  },
  getLawDetail(
    lawId: number,
    params?: { keyword?: string; page?: number; page_size?: number },
  ): Promise<RegulationLawDetailOut> {
    return get<RegulationLawDetailOut>(`/regulations/laws/${lawId}`, { params })
  },
  verifyArticle(text: string): Promise<VerifyArticleOut> {
    return post<VerifyArticleOut>('/regulations/verify', { text })
  },
  importFile(file: File, lawName?: string): Promise<RegulationImportOut> {
    const fd = new FormData()
    fd.append('file', file, file.name)
    if (lawName && lawName.trim()) {
      fd.append('law_name', lawName.trim())
    }
    return post<RegulationImportOut>('/regulations/import', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  crawlUpdates(): Promise<RegulationCrawlOut> {
    return post<RegulationCrawlOut>('/regulations/crawl')
  },
  deleteLaw(lawId: number): Promise<RegulationLawOut | OkResponse> {
    return del<OkResponse>(`/regulations/laws/${lawId}`)
  },
  deleteArticle(articleId: number): Promise<OkResponse> {
    return del<OkResponse>(`/regulations/articles/${articleId}`)
  },
}

/** E36 + E38 多标书交叉比对 API (/api/similarity) */
export const similarityApi = {
  compare(payload: {
    bidFiles: File[]
    labels?: string[]
    tenderFile?: File | null
    title?: string
    enableAiReview?: boolean
    asyncRun?: boolean
  }): Promise<SimilarityReportDetailOut> {
    const fd = new FormData()
    for (const f of payload.bidFiles) {
      fd.append('bid_files', f, f.name)
    }
    if (payload.tenderFile) {
      fd.append('tender_file', payload.tenderFile, payload.tenderFile.name)
    }
    if (payload.title && payload.title.trim()) {
      fd.append('title', payload.title.trim())
    }
    if (payload.labels && payload.labels.length > 0) {
      fd.append('labels_json', JSON.stringify(payload.labels))
    }
    fd.append('enable_ai_review', payload.enableAiReview ? 'true' : 'false')
    fd.append('async_run', payload.asyncRun ? 'true' : 'false')
    return post<SimilarityReportDetailOut>('/similarity/compare', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300000,
    })
  },
  cancel(reportId: number): Promise<OkResponse> {
    return post<OkResponse>(`/similarity/${reportId}/cancel`)
  },
  list(): Promise<SimilarityReportListOut> {
    return get<SimilarityReportListOut>('/similarity')
  },
  detail(reportId: number): Promise<SimilarityReportDetailOut> {
    return get<SimilarityReportDetailOut>(`/similarity/${reportId}`)
  },
  downloadReport(reportId: number, fallbackName = `比对报告_${reportId}.docx`): Promise<string> {
    return downloadFromApi(`/similarity/${reportId}/export`, fallbackName)
  },
  remove(reportId: number): Promise<OkResponse> {
    return del<OkResponse>(`/similarity/${reportId}`)
  },
}

/** E37 + E38 标书编制输出、Skills 管理与无损修订中心 API (/api/composer) */
export const composerApi = {
  listIndicators(bizLine: BizLineType = 'general'): Promise<BizLineIndicatorOverviewOut> {
    return get<BizLineIndicatorOverviewOut>('/composer/indicators', {
      params: { biz_line: bizLine },
    })
  },
  listSkills(): Promise<ComposerSkillListOut> {
    return get<ComposerSkillListOut>('/composer/skills')
  },
  createSkill(payload: ComposerSkillUpsertPayload): Promise<ComposerSkillOut> {
    return post<ComposerSkillOut>('/composer/skills', payload)
  },
  updateSkill(skillId: number, payload: ComposerSkillUpsertPayload): Promise<ComposerSkillOut> {
    return put<ComposerSkillOut>(`/composer/skills/${skillId}`, payload)
  },
  deleteSkill(skillId: number): Promise<OkResponse> {
    return del<OkResponse>(`/composer/skills/${skillId}`)
  },
  previewSkillUrl(urlOrRepo: string, allowGiteeFallback = true): Promise<SkillImportPreviewOut> {
    return post<SkillImportPreviewOut>('/composer/skills/preview-url', {
      url_or_repo: urlOrRepo,
      allow_gitee_fallback: allowGiteeFallback,
    })
  },
  importSkillUrl(urlOrRepo: string, allowGiteeFallback = true): Promise<ComposerSkillOut> {
    return post<ComposerSkillOut>('/composer/skills/import-url', {
      url_or_repo: urlOrRepo,
      allow_gitee_fallback: allowGiteeFallback,
    })
  },
  importSkillFile(file: File): Promise<ComposerSkillOut> {
    const fd = new FormData()
    fd.append('file', file, file.name)
    return post<ComposerSkillOut>('/composer/skills/import-file', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  generate(payload: {
    tenderFile?: File | null
    taskId?: number | null
    knowledgeFiles?: File[]
    composeMode?: 'skeleton_v1' | 'knowledge_draft_v2'
    title?: string
    packageNo?: number | null
    skillId?: number | null
    useAi?: boolean
    useIndicatorLibrary?: boolean
    blindBidMode?: boolean
    blindRules?: string[]
    blindCustomWords?: string[]
    asyncRun?: boolean
  }): Promise<ComposeDraftDetailOut> {
    const fd = new FormData()
    if (payload.tenderFile) {
      fd.append('tender_file', payload.tenderFile, payload.tenderFile.name)
    }
    if (typeof payload.taskId === 'number' && payload.taskId > 0) {
      fd.append('task_id', String(payload.taskId))
    }
    for (const kf of payload.knowledgeFiles || []) {
      fd.append('knowledge_files', kf, kf.name)
    }
    fd.append('compose_mode', payload.composeMode || 'skeleton_v1')
    if (payload.title && payload.title.trim()) {
      fd.append('title', payload.title.trim())
    }
    if (typeof payload.packageNo === 'number' && payload.packageNo > 0) {
      fd.append('package_no', String(payload.packageNo))
    }
    if (typeof payload.skillId === 'number' && payload.skillId > 0) {
      fd.append('skill_id', String(payload.skillId))
    }
    fd.append('use_ai', payload.useAi !== false ? 'true' : 'false')
    fd.append('use_indicator_library', payload.useIndicatorLibrary ? 'true' : 'false')
    fd.append('blind_bid_mode', payload.blindBidMode ? 'true' : 'false')
    if (payload.blindRules) {
      fd.append('blind_rules_json', JSON.stringify(payload.blindRules))
    }
    if (payload.blindCustomWords && payload.blindCustomWords.length > 0) {
      fd.append('blind_custom_words_json', JSON.stringify(payload.blindCustomWords))
    }
    fd.append('async_run', payload.asyncRun ? 'true' : 'false')
    return post<ComposeDraftDetailOut>('/composer/generate', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300000,
    })
  },
  cancelDraft(draftId: number): Promise<OkResponse> {
    return post<OkResponse>(`/composer/drafts/${draftId}/cancel`)
  },
  listDrafts(): Promise<ComposeDraftListOut> {
    return get<ComposeDraftListOut>('/composer/drafts')
  },
  getDraft(draftId: number): Promise<ComposeDraftDetailOut> {
    return get<ComposeDraftDetailOut>(`/composer/drafts/${draftId}`)
  },
  downloadDraft(draftId: number, fallbackName = `${draftId}_投标文件初稿.docx`): Promise<string> {
    return downloadFromApi(`/composer/drafts/${draftId}/download`, fallbackName)
  },
  deleteDraft(draftId: number): Promise<OkResponse> {
    return del<OkResponse>(`/composer/drafts/${draftId}`)
  },
}
