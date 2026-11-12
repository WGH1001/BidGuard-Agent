import { del, get, getStoredToken, post, put } from './http'
import { downloadFromApi } from './download'
import type {
  AuditLogListOut,
  BatchDeliverZipOut,
  BidFindingOut,
  BidRuleConfigOut,
  BidRuleConfigUpsertRequest,
  BidTaskDetailOut,
  BidTaskListOut,
  BidTaskOut,
  CertificateOut,
  CertificateParseOut,
  CertificateUpsertRequest,
  ChangePasswordRequest,
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
  QaSessionCreatePayload,
  QaSessionOut,
  RadarScanOut,
  RegulationCrawlOut,
  RegulationImportOut,
  RegulationLawDetailOut,
  RegulationLawOut,
  RunTaskTriggerOut,
  UserProfileOut,
  VerifyArticleOut,
  QaAttachmentOut,
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
  remove(taskId: number): Promise<OkResponse> {
    return del<OkResponse>(`/checks/${taskId}`)
  },
  downloadStoredFile(fileUrl: string, fallbackName: string): Promise<string> {
    return downloadFromApi(fileUrl, fallbackName)
  },
}

export const extractApi = {
  extract(file: File): Promise<ExtractChecklistOut> {
    const fd = new FormData()
    fd.append('file', file, file.name)
    return post<ExtractChecklistOut>('/extract', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 600000, // E19：F1 大标书 LLM 结构化提取单独放宽至 10 分钟（600000ms），其余 API 保持 3 分钟
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
  scan(tenderFile: File, title?: string): Promise<RadarScanOut> {
    const fd = new FormData()
    fd.append('tender_file', tenderFile, tenderFile.name)
    if (title && title.trim()) {
      fd.append('title', title.trim())
    }
    return post<RadarScanOut>('/radar', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  list(): Promise<BidTaskListOut> {
    return get<BidTaskListOut>('/radar')
  },
  detail(taskId: number): Promise<RadarScanOut> {
    return get<RadarScanOut>(`/radar/${taskId}`)
  },
  rescan(taskId: number): Promise<RadarScanOut> {
    return post<RadarScanOut>(`/radar/${taskId}/rescan`)
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
        handlers.onError?.(String((parsed as { detail?: string }).detail || '流式问答异常'))
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
          throw new Error(`HTTP ${resp.status}`)
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
          handlers.onError?.('流式响应未正常结束')
        }
      } catch (err: unknown) {
        if ((err as { name?: string })?.name === 'AbortError') return
        handlers.onError?.(err instanceof Error ? err.message : '流式连接中断')
      }
    })()

    return () => {
      controller.abort()
    }
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
