import { del, get, post, put } from './http'
import { downloadFromApi } from './download'
import type {
  AuditLogListOut,
  BidFindingOut,
  BidRuleConfigOut,
  BidRuleConfigUpsertRequest,
  BidTaskDetailOut,
  BidTaskListOut,
  BidTaskOut,
  CertificateOut,
  CertificateUpsertRequest,
  ChangePasswordRequest,
  DashboardStatsOut,
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
  QaSessionOut,
  RadarScanOut,
  RunTaskTriggerOut,
  UserProfileOut,
} from './types'

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
  create(formData: FormData): Promise<BidTaskOut> {
    return post<BidTaskOut>('/checks', formData, {
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
}

export const extractApi = {
  extract(file: File): Promise<ExtractChecklistOut> {
    const fd = new FormData()
    fd.append('file', file)
    return post<ExtractChecklistOut>('/extract', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
}

export const deviationApi = {
  generate(taskId: number): Promise<DeviationExportOut> {
    return post<DeviationExportOut>(`/deviation/${taskId}`)
  },
  download(taskId: number, fallbackName = `偏离表_${taskId}.docx`): Promise<string> {
    return downloadFromApi(`/deviation/${taskId}/download`, fallbackName)
  },
}

export const radarApi = {
  scan(tenderFile: File, title?: string): Promise<RadarScanOut> {
    const fd = new FormData()
    fd.append('tender_file', tenderFile)
    if (title) fd.append('title', title)
    return post<RadarScanOut>('/radar', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
}

export const qaApi = {
  createSession(payload: {
    task_id?: number
    title?: string
    tender_path?: string
    bid_path?: string
  }): Promise<QaSessionOut> {
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
}

export const reportsApi = {
  downloadRedline(taskId: number, fallbackName = `${taskId}_标红原件.docx`): Promise<string> {
    return downloadFromApi(`/reports/${taskId}/redline`, fallbackName)
  },
  downloadDeliverZip(taskId: number, fallbackName = `${taskId}_封标交付包.zip`): Promise<string> {
    return downloadFromApi(`/reports/${taskId}/deliver.zip`, fallbackName)
  },
  listAuditLogs(params?: {
    action?: string
    page?: number
    page_size?: number
  }): Promise<AuditLogListOut> {
    return get<AuditLogListOut>('/reports/audit-logs', { params })
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