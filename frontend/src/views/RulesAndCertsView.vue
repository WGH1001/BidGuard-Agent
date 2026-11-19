<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { composerApi, regulationsApi, rulesApi } from '@/api'
import {
  formatMoneyDisplay,
  type CertificateOut,
  type CustomRedlineItem,
  type IndicatorEntryOut,
  type IndicatorLineDetailOut,
  type RegulationArticleOut,
  type RegulationCrawlOut,
  type RegulationLawOut,
  type VerifyArticleOut,
} from '@/api/types'
import { useAuthStore } from '@/stores/auth'

defineOptions({ name: 'RulesAndCertsView' })

const authStore = useAuthStore()
const route = useRoute()

const activeTab = ref<'rules' | 'certs' | 'regulations' | 'indicators'>('rules')
const loadingRules = ref(false)
const savingRules = ref(false)
const loadingCerts = ref(false)
const expiringOnly = ref(false)

// ── 页签 ①：核查规则与自定义红线状态 ────────────────────────────────────
const AVAILABLE_MONTHS = [
  '2026-01', '2026-02', '2026-03', '2026-04', '2026-05', '2026-06',
  '2026-07', '2026-08', '2026-09', '2026-10', '2026-11', '2026-12',
]

const ruleForm = reactive({
  name: '默认核查规则',
  price_cap: '',
  open_date: '',
  buyer_name: '',
  required_social_months_json: [] as string[],
  custom_redlines_json: [] as CustomRedlineItem[],
})

async function fetchRuleConfig(): Promise<void> {
  loadingRules.value = true
  try {
    const list = await rulesApi.listRules()
    const cfg = list.find((r) => r.is_default) || list[0]
    if (cfg) {
      ruleForm.name = cfg.name || '默认核查规则'
      ruleForm.price_cap = cfg.price_cap || ''
      ruleForm.open_date = cfg.open_date || ''
      ruleForm.buyer_name = cfg.buyer_name || ''
      ruleForm.required_social_months_json = [...(cfg.required_social_months_json || [])]
      ruleForm.custom_redlines_json = (cfg.custom_redlines_json || []).map((item) => ({
        keyword: item.keyword,
        severity: item.severity,
        consequence: item.consequence,
      }))
    }
  } finally {
    loadingRules.value = false
  }
}

function addRedlineRow(): void {
  ruleForm.custom_redlines_json.push({
    keyword: '',
    severity: 'error',
    consequence: 'veto',
  })
}

function removeRedlineRow(idx: number): void {
  ruleForm.custom_redlines_json.splice(idx, 1)
}

async function handleSaveRuleConfig(): Promise<void> {
  const cleanedRedlines = ruleForm.custom_redlines_json
    .map((r) => ({
      keyword: r.keyword.trim(),
      severity: r.severity,
      consequence: r.consequence,
    }))
    .filter((r) => r.keyword.length > 0)

  const cleanCap = String(ruleForm.price_cap || '').replace(/,/g, '').trim()

  savingRules.value = true
  try {
    const saved = await rulesApi.saveRule({
      name: ruleForm.name.trim() || '默认核查规则',
      price_cap: cleanCap || null,
      open_date: ruleForm.open_date.trim() || null,
      buyer_name: ruleForm.buyer_name.trim() || null,
      required_social_months_json: ruleForm.required_social_months_json,
      custom_redlines_json: cleanedRedlines,
      is_default: true,
    })
    ruleForm.custom_redlines_json = [...(saved.custom_redlines_json || [])]
    ElMessage.success('防废标核查参数与自定义红线已保存生效')
  } finally {
    savingRules.value = false
  }
}

// ── 页签 ②：企业证照效期台账 CRUD 状态 ──────────────────────────────────
const certs = ref<CertificateOut[]>([])
const certDialogVisible = ref(false)
const savingCert = ref(false)
const editingCertId = ref<number | null>(null)
const pendingCertFile = ref<File | null>(null)
const parsingCertFile = ref(false)
const certParseHintSummary = ref('')
const editingExistingAttachmentName = ref<string | null>(null)
const certNameSource = ref<'ocr' | 'filename_fallback' | null>(null)

const certForm = reactive({
  cert_name: '',
  cert_no: '',
  valid_until: '',
  is_long_term: false,
  warn_days: 30,
  remark: '',
})

async function fetchCerts(): Promise<void> {
  loadingCerts.value = true
  try {
    certs.value = await rulesApi.listCertificates(expiringOnly.value)
  } finally {
    loadingCerts.value = false
  }
}

function openCreateCert(): void {
  editingCertId.value = null
  certForm.cert_name = ''
  certForm.cert_no = ''
  certForm.valid_until = ''
  certForm.is_long_term = false
  certForm.warn_days = 30
  certForm.remark = ''
  pendingCertFile.value = null
  parsingCertFile.value = false
  certParseHintSummary.value = ''
  certNameSource.value = null
  editingExistingAttachmentName.value = null
  certDialogVisible.value = true
}

function openEditCert(row: Partial<CertificateOut>): void {
  editingCertId.value = row.id ?? null
  certForm.cert_name = row.cert_name ?? ''
  certForm.cert_no = row.cert_no || ''
  certForm.is_long_term = Boolean(row.is_long_term)
  certForm.valid_until = certForm.is_long_term ? '' : (row.valid_until ?? '')
  certForm.warn_days = row.warn_days ?? 30
  certForm.remark = row.remark || ''
  pendingCertFile.value = null
  parsingCertFile.value = false
  certParseHintSummary.value = ''
  certNameSource.value = null
  editingExistingAttachmentName.value = row.has_attachment ? (row.attachment_name || '已上传原件') : null
  certDialogVisible.value = true
}

function onLongTermToggle(val: boolean | string | number): void {
  if (val) {
    certForm.valid_until = ''
  }
}

async function onCertFileChange(ev: Event): Promise<void> {
  const input = ev.target as HTMLInputElement
  const f = input.files?.[0]
  input.value = ''
  if (!f) return

  const ext = f.name.includes('.') ? f.name.slice(f.name.lastIndexOf('.')).toLowerCase() : ''
  if (!['.pdf', '.png', '.jpg', '.jpeg'].includes(ext)) {
    ElMessage.warning('仅支持上传 .pdf / .png / .jpg / .jpeg 格式证照扫件')
    return
  }
  if (f.size <= 0) {
    ElMessage.warning('所选证照文件为空')
    return
  }
  if (f.size > 20 * 1024 * 1024) {
    ElMessage.warning('证照扫件单文件不能超过 20MB')
    return
  }

  pendingCertFile.value = f
  parsingCertFile.value = true
  certParseHintSummary.value = '正在识别证照文字并预填字段…'
  try {
    const parsed = await rulesApi.parseCertificateFile(f)
    const isCreate = editingCertId.value === null
    if (parsed.cert_name_hint && (isCreate || !certForm.cert_name.trim())) {
      certForm.cert_name = parsed.cert_name_hint
      certNameSource.value = parsed.cert_name_source ?? null
    }
    if (parsed.cert_no_hint && (isCreate || !certForm.cert_no.trim())) {
      certForm.cert_no = parsed.cert_no_hint
    }
    if (parsed.is_long_term_hint && (isCreate || (!certForm.valid_until.trim() && !certForm.is_long_term))) {
      certForm.is_long_term = true
      certForm.valid_until = ''
    } else if (parsed.valid_until_hint && !certForm.is_long_term && (isCreate || !certForm.valid_until.trim())) {
      certForm.valid_until = parsed.valid_until_hint
    }
    const parts: string[] = [`已识别 ${parsed.ocr_chars} 字`]
    if (parsed.cert_name_hint) {
      parts.push(
        parsed.cert_name_source === 'filename_fallback'
          ? `名称：${parsed.cert_name_hint}（文件名兜底）`
          : `名称：${parsed.cert_name_hint}`,
      )
    }
    if (parsed.cert_no_hint) parts.push(`编号：${parsed.cert_no_hint}`)
    if (parsed.is_long_term_hint) {
      parts.push('有效期：长期有效')
    } else if (parsed.valid_until_hint) {
      parts.push(`截止日：${parsed.valid_until_hint}`)
    }
    certParseHintSummary.value = parts.join(' ｜ ')
    ElMessage.success('证照文字预识别完成，请核对上方预填字段后保存')
  } catch {
    certParseHintSummary.value = '未能自动提取字段（保存时仍将留档原件，可手动填写字段）'
  } finally {
    parsingCertFile.value = false
  }
}

async function handleSaveCert(): Promise<void> {
  if (!certForm.cert_name.trim()) {
    ElMessage.warning('请填写证照名称')
    return
  }
  if (!certForm.is_long_term && !certForm.valid_until.trim()) {
    ElMessage.warning('请填写有效期截止日（YYYY-MM-DD），或勾选「长期有效」')
    return
  }
  savingCert.value = true
  try {
    const payload = {
      cert_name: certForm.cert_name.trim(),
      cert_no: certForm.cert_no.trim() || null,
      valid_until: certForm.is_long_term ? null : certForm.valid_until.trim(),
      is_long_term: certForm.is_long_term,
      warn_days: Number(certForm.warn_days) || 30,
      remark: certForm.remark.trim() || null,
    }
    let savedCert: CertificateOut
    if (editingCertId.value === null) {
      savedCert = await rulesApi.createCertificate(payload)
    } else {
      savedCert = await rulesApi.updateCertificate(editingCertId.value, payload)
    }
    if (pendingCertFile.value) {
      await rulesApi.uploadCertificateAttachment(savedCert.id, pendingCertFile.value)
    }
    ElMessage.success(
      editingCertId.value === null
        ? pendingCertFile.value
          ? '已新增企业投标证照并留档原件'
          : '已新增企业投标证照记录'
        : pendingCertFile.value
          ? '证照记录与原件附件已更新'
          : '证照记录已更新',
    )
    certDialogVisible.value = false
    pendingCertFile.value = null
    await fetchCerts()
  } finally {
    savingCert.value = false
  }
}

async function handleDownloadCertAttachment(row: Partial<CertificateOut>): Promise<void> {
  if (!row.id || !row.has_attachment) return
  const fn = await rulesApi.downloadCertificateAttachment(
    row.id,
    row.attachment_name || `certificate_${row.id}`,
  )
  ElMessage.success(`已下载证照原件：${fn}`)
}

async function handleDeleteCertAttachment(row: Partial<CertificateOut>): Promise<void> {
  if (!row.id || !row.has_attachment) return
  try {
    await ElMessageBox.confirm(
      `确认解绑并删除证照「${row.cert_name}」的已上传原件（${row.attachment_name || '原件'}）吗？`,
      '确认删除证照原件',
      {
        type: 'warning',
        confirmButtonText: '确认删除原件',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  await rulesApi.deleteCertificateAttachment(row.id)
  ElMessage.success('证照原件附件已清理')
  await fetchCerts()
}

async function handleDeleteCert(row: Partial<CertificateOut>): Promise<void> {
  if (row.id === undefined) return
  try {
    await ElMessageBox.confirm(
      `确认删除企业投标证照「${row.cert_name ?? ''}」吗？删除后不可恢复。`,
      '确认删除证照记录',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  await rulesApi.deleteCertificate(row.id)
  ElMessage.success(`已删除证照「${row.cert_name ?? ''}」`)
  await fetchCerts()
}

function certRowClassName({ row }: { row: CertificateOut }): string {
  if (row.is_expired) return 'cert-expired-row'
  if (row.is_expiring_soon) return 'cert-expiring-row'
  return ''
}

// ── 页签 ③：E4 法规库浏览、关键词检索、法条三态真伪核验与官网更新状态 ─────
const loadingLaws = ref(false)
const loadingArticles = ref(false)
const laws = ref<RegulationLawOut[]>([])
const selectedLawId = ref<number | null>(null)
const selectedLaw = ref<RegulationLawOut | null>(null)
const articles = ref<RegulationArticleOut[]>([])
const totalArticles = ref(0)
const articlePage = ref(1)
const articlePageSize = ref(30)
const articleKeyword = ref('')

// 法条真伪核验状态
const verifyInputText = ref('')
const verifying = ref(false)
const verifyResult = ref<VerifyArticleOut | null>(null)

// 官网更新与手动导入状态（admin）
const crawling = ref(false)
const crawlReport = ref<RegulationCrawlOut | null>(null)
const importingFile = ref(false)
const importLawNameHint = ref('')

async function fetchLaws(autoSelectFirst = true): Promise<void> {
  loadingLaws.value = true
  try {
    laws.value = await regulationsApi.listLaws()
    if (autoSelectFirst && laws.value.length > 0 && selectedLawId.value === null) {
      await handleSelectLaw(laws.value[0])
    } else if (selectedLawId.value !== null) {
      const matched = laws.value.find((l) => l.id === selectedLawId.value)
      if (matched) {
        selectedLaw.value = matched
        await fetchLawArticles()
      }
    }
  } finally {
    loadingLaws.value = false
  }
}

async function handleSelectLaw(law: RegulationLawOut): Promise<void> {
  selectedLawId.value = law.id
  selectedLaw.value = law
  articlePage.value = 1
  await fetchLawArticles()
}

async function handleDeleteLaw(law: RegulationLawOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `将级联删除《${law.name}》及该法全部 ${law.article_count} 条条文（种子法规删除后可通过重新上传/导入恢复），是否确认删除？`,
      '确认级联删除法规',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }

  await regulationsApi.deleteLaw(law.id)
  ElMessage.success(`已删除《${law.name}》及全部 ${law.article_count} 条条文`)

  if (selectedLawId.value === law.id) {
    selectedLawId.value = null
    selectedLaw.value = null
    articles.value = []
    totalArticles.value = 0
    await fetchLaws(true)
  } else {
    await fetchLaws(false)
  }
}

async function handleDeleteArticle(art: RegulationArticleOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认从《${selectedLaw.value?.name ?? '当前法规'}》删除条文「${art.article_no}」吗？删除后将自动重算该法规哈希与条文数。`,
      '确认删除单条法条',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }

  await regulationsApi.deleteArticle(art.id)
  ElMessage.success(`已删除条文 ${art.article_no}`)
  await fetchLaws(false)
}

async function fetchLawArticles(): Promise<void> {
  if (selectedLawId.value === null) return
  loadingArticles.value = true
  try {
    const detail = await regulationsApi.getLawDetail(selectedLawId.value, {
      keyword: articleKeyword.value.trim() || undefined,
      page: articlePage.value,
      page_size: articlePageSize.value,
    })
    selectedLaw.value = detail.law
    articles.value = detail.articles || []
    totalArticles.value = detail.total_articles || 0
  } finally {
    loadingArticles.value = false
  }
}

function handleSearchArticles(): void {
  articlePage.value = 1
  void fetchLawArticles()
}

function handleArticlePageChange(p: number): void {
  articlePage.value = p
  void fetchLawArticles()
}

async function handleVerifyArticle(): Promise<void> {
  const clean = verifyInputText.value.trim()
  if (!clean) {
    ElMessage.warning('请粘贴或输入待核验的法条文本')
    return
  }
  verifying.value = true
  try {
    verifyResult.value = await regulationsApi.verifyArticle(clean)
  } finally {
    verifying.value = false
  }
}

function fillSampleForVerify(art: RegulationArticleOut): void {
  const lawTitle = selectedLaw.value?.name || ''
  verifyInputText.value = `${lawTitle}${art.article_no} ${art.content}`
  void handleVerifyArticle()
}

async function handleCrawlUpdates(): Promise<void> {
  crawling.value = true
  try {
    crawlReport.value = await regulationsApi.crawlUpdates()
    await fetchLaws(false)
    ElMessage.info(
      `官网更新检查完成：共检查 ${crawlReport.value.total_sources} 个源，` +
        `更新 ${crawlReport.value.updated_laws_count} 部，未配置占位源 ${crawlReport.value.unconfigured_count} 个`,
    )
  } finally {
    crawling.value = false
  }
}

async function handleImportLawFile(ev: Event): Promise<void> {
  const input = ev.target as HTMLInputElement
  const f = input.files?.[0]
  if (!f) return
  importingFile.value = true
  try {
    const res = await regulationsApi.importFile(f, importLawNameHint.value)
    ElMessage.success(
      `成功导入 ${res.law_name}：共 ${res.article_count} 条（新增 ${res.added_count} 条，修订 ${res.modified_count} 条）`,
    )
    importLawNameHint.value = ''
    await fetchLaws(false)
    const found = laws.value.find((l) => l.id === res.law_id)
    if (found) {
      await handleSelectLaw(found)
    }
  } finally {
    importingFile.value = false
    input.value = ''
  }
}

// ── 页签 ④：企业指标与业务线评分库在线维护及人工终审台 ─────────────────
const loadingIndicatorLines = ref(false)
const indicatorLines = ref<IndicatorLineDetailOut[]>([])
const selectedLineKey = ref<string>('common')
const indicatorSearchKeyword = ref('')
const indicatorCategoryFilter = ref('')

const lineDialogVisible = ref(false)
const savingLine = ref(false)
const newLineForm = reactive({
  line: '',
  display_name: '',
  description: '',
})

const entryDialogVisible = ref(false)
const savingEntry = ref(false)
const editingIndicatorId = ref<string | null>(null)
const entryForm = reactive({
  line: 'common',
  indicator_id: '',
  category: '技术方案',
  indicator_name: '',
  standard_response: '',
  weight_hint: '',
  typical_points_text: '',
  high_score_tips: '',
  param_slots_text: '',
  keywords_text: '',
  source_ref: '',
})

const approvingLine = ref(false)

const currentIndicatorLine = computed<IndicatorLineDetailOut | null>(() => {
  if (!indicatorLines.value.length) return null
  return (
    indicatorLines.value.find((it) => it.line === selectedLineKey.value) ||
    indicatorLines.value[0] ||
    null
  )
})

const availableIndicatorCategories = computed<string[]>(() => {
  const line = currentIndicatorLine.value
  if (!line) return []
  const set = new Set<string>()
  for (const item of line.indicators || []) {
    if (item.category) set.add(item.category)
  }
  return Array.from(set)
})

const filteredIndicators = computed<IndicatorEntryOut[]>(() => {
  const line = currentIndicatorLine.value
  if (!line) return []
  const kw = indicatorSearchKeyword.value.trim().toLowerCase()
  const cat = indicatorCategoryFilter.value.trim()
  return (line.indicators || []).filter((item) => {
    if (cat && item.category !== cat) return false
    if (!kw) return true
    const blob = [
      item.indicator_id,
      item.category,
      item.indicator_name,
      item.standard_response,
      ...(item.keywords || []),
      ...(item.param_slots || []),
    ]
      .join(' ')
      .toLowerCase()
    return blob.includes(kw)
  })
})

function hasPendingSlotInEntry(entry: IndicatorEntryOut): boolean {
  const blob = [
    entry.standard_response,
    ...(entry.param_slots || []),
    entry.scoring_ref?.high_score_tips || '',
    ...(entry.scoring_ref?.typical_points || []),
  ].join(' ')
  return blob.includes('[待人工确认：') || blob.includes('[待人工填报：')
}

async function fetchIndicatorLines(keepSelected = true): Promise<void> {
  loadingIndicatorLines.value = true
  try {
    const res = await composerApi.listIndicatorLines()
    indicatorLines.value = res.items || []
    if (!keepSelected || !indicatorLines.value.some((it) => it.line === selectedLineKey.value)) {
      if (indicatorLines.value.length > 0) {
        selectedLineKey.value = indicatorLines.value[0].line
      }
    }
  } finally {
    loadingIndicatorLines.value = false
  }
}

function handleSelectIndicatorLine(lineKey: string): void {
  selectedLineKey.value = lineKey
  indicatorCategoryFilter.value = ''
}

function openCreateIndicatorLineDialog(): void {
  newLineForm.line = ''
  newLineForm.display_name = ''
  newLineForm.description = ''
  lineDialogVisible.value = true
}

async function handleCreateIndicatorLine(): Promise<void> {
  const keyClean = newLineForm.line.trim().toLowerCase()
  const nameClean = newLineForm.display_name.trim()
  if (!/^[a-z][a-z0-9_]{1,23}$/.test(keyClean)) {
    ElMessage.warning('业务线标识需为 2~24 位小写字母、数字或下划线，且以字母开头（如 cloud_ops）')
    return
  }
  if (!nameClean) {
    ElMessage.warning('请填写业务线显示名称')
    return
  }
  savingLine.value = true
  try {
    const created = await composerApi.createIndicatorLine({
      line: keyClean,
      display_name: nameClean,
      description: newLineForm.description.trim(),
    })
    ElMessage.success(`已创建业务线指标库「${created.display_name} (${created.line})」`)
    lineDialogVisible.value = false
    await fetchIndicatorLines(false)
    selectedLineKey.value = created.line
  } finally {
    savingLine.value = false
  }
}

function openCreateIndicatorEntry(): void {
  const curLine = currentIndicatorLine.value?.line || 'common'
  editingIndicatorId.value = null
  entryForm.line = curLine
  entryForm.indicator_id = ''
  entryForm.category = '技术方案'
  entryForm.indicator_name = ''
  entryForm.standard_response = ''
  entryForm.weight_hint = '4~8分'
  entryForm.typical_points_text = ''
  entryForm.high_score_tips = ''
  entryForm.param_slots_text = ''
  entryForm.keywords_text = ''
  entryForm.source_ref = ''
  entryDialogVisible.value = true
}

function openEditIndicatorEntry(row: IndicatorEntryOut): void {
  editingIndicatorId.value = row.indicator_id
  entryForm.line = row.line || currentIndicatorLine.value?.line || 'common'
  entryForm.indicator_id = row.indicator_id
  entryForm.category = row.category || '技术方案'
  entryForm.indicator_name = row.indicator_name || ''
  entryForm.standard_response = row.standard_response || ''
  entryForm.weight_hint = row.scoring_ref?.weight_hint || ''
  entryForm.typical_points_text = (row.scoring_ref?.typical_points || []).join('\n')
  entryForm.high_score_tips = row.scoring_ref?.high_score_tips || ''
  entryForm.param_slots_text = (row.param_slots || []).join('\n')
  entryForm.keywords_text = (row.keywords || []).join('，')
  entryForm.source_ref = row.source_ref || ''
  entryDialogVisible.value = true
}

function splitLinesClean(raw: string): string[] {
  return raw
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
}

function splitKeywordsClean(raw: string): string[] {
  return raw
    .split(/[,，;；\r\n]+/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
}

async function handleSaveIndicatorEntry(): Promise<void> {
  if (!entryForm.indicator_name.trim()) {
    ElMessage.warning('请填写指标名称')
    return
  }
  if (!entryForm.standard_response.trim()) {
    ElMessage.warning('请填写标准应答正文')
    return
  }
  savingEntry.value = true
  try {
    const payload = {
      line: entryForm.line,
      indicator_id: entryForm.indicator_id.trim() || undefined,
      category: entryForm.category.trim() || '技术方案',
      indicator_name: entryForm.indicator_name.trim(),
      standard_response: entryForm.standard_response.trim(),
      scoring_ref: {
        weight_hint: entryForm.weight_hint.trim(),
        typical_points: splitLinesClean(entryForm.typical_points_text),
        high_score_tips: entryForm.high_score_tips.trim(),
      },
      param_slots: splitLinesClean(entryForm.param_slots_text),
      keywords: splitKeywordsClean(entryForm.keywords_text),
      source_ref: entryForm.source_ref.trim(),
    }
    const res =
      editingIndicatorId.value === null
        ? await composerApi.createIndicatorEntry(payload)
        : await composerApi.updateIndicatorEntry(editingIndicatorId.value, payload)

    if (res.money_sanitized) {
      ElMessage.warning(
        '防造假红线已触发：所填内容中的具体人民币报价数字已被自动转化为「[待人工填报：...]」安全槽位！',
      )
    } else {
      ElMessage.success(
        editingIndicatorId.value === null
          ? `已新增指标条目「${res.entry.indicator_name}」`
          : `已更新指标条目「${res.entry.indicator_name}」`,
      )
    }
    entryDialogVisible.value = false
    await fetchIndicatorLines(true)
  } finally {
    savingEntry.value = false
  }
}

async function handleDeleteIndicatorEntry(row: IndicatorEntryOut): Promise<void> {
  const lineKey = currentIndicatorLine.value?.line || row.line || 'common'
  try {
    await ElMessageBox.confirm(
      `确认从业务线「${currentIndicatorLine.value?.display_name || lineKey}」删除指标「${row.indicator_name} (${row.indicator_id})」吗？`,
      '确认删除标准指标',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  await composerApi.deleteIndicatorEntry(row.indicator_id, lineKey)
  ElMessage.success(`已删除指标 ${row.indicator_id}`)
  await fetchIndicatorLines(true)
}

async function handleApproveCurrentLine(): Promise<void> {
  const line = currentIndicatorLine.value
  if (!line) return
  try {
    await ElMessageBox.confirm(
      `确认已完成对「${line.display_name}」共 ${line.total_indicators} 条指标的人工核对（当前含 ${line.pending_slot_count} 个待项目组填报槽位），并将其正式签批为「已人工终审 (approved)」吗？`,
      '签署企业指标库人工终审',
      {
        type: 'info',
        confirmButtonText: '确认签批通过',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  approvingLine.value = true
  try {
    const updated = await composerApi.approveIndicatorLine({
      line: line.line,
      review_note: '经管理员在线核对确认，所有报价与待确认参数均已规范化为安全槽位，准予作为正式标书编制底座。',
    })
    ElMessage.success(`业务线「${updated.display_name}」已签署人工终审通过`)
    await fetchIndicatorLines(true)
  } finally {
    approvingLine.value = false
  }
}

onMounted(() => {
  const qTab = typeof route.query.tab === 'string' ? route.query.tab : ''
  if (qTab === 'rules' || qTab === 'certs' || qTab === 'regulations' || qTab === 'indicators') {
    activeTab.value = qTab
  }
  void fetchRuleConfig()
  void fetchCerts()
  void fetchLaws(true)
  void fetchIndicatorLines(true)
})
</script>
<template>
  <div class="rules-certs-view">
    <div class="bg-panel rc-main-card">
      <el-tabs v-model="activeTab">
        <!-- 页签 ①：核查规则与自定义禁用词红线 -->
        <el-tab-pane label="① 封标防废标核查规则配置" name="rules">
          <div v-loading="loadingRules" class="rule-pane">
            <div class="rp-top-bar">
              <div>
                <h3>部门默认防废标体检基准参数（F2 / F3 确定性核验底座）</h3>
                <p>配置最高投标限价、计划开标日、采购人名称、社保证明必含月份与自定义红线禁用词。</p>
              </div>
              <el-button type="primary" :loading="savingRules" @click="handleSaveRuleConfig">
                保存核查规则配置
              </el-button>
            </div>

            <div class="rule-grid-2">
              <div class="rf-field">
                <label>规则配置方案名称</label>
                <el-input v-model="ruleForm.name" placeholder="例如：政企集成部默认核查规则" />
              </div>

              <div class="rf-field">
                <label>招标最高限价 / 预算上限 (元)</label>
                <el-input v-model="ruleForm.price_cap" placeholder="例如：1450000.00" />
                <div class="money-preview mono-num">
                  千分位预览：¥{{ formatMoneyDisplay(ruleForm.price_cap) }}
                </div>
              </div>

              <div class="rf-field">
                <label>默认计划开标日期 (YYYY-MM-DD · F3 证照有效期核验基准日)</label>
                <el-input v-model="ruleForm.open_date" placeholder="例如：2026-11-05" />
              </div>

              <div class="rf-field">
                <label>本项目采购人 / 招标单位全称（F3 跨项目串名扫描防自伤保护）</label>
                <el-input v-model="ruleForm.buyer_name" placeholder="例如：某省政务云管理中心" />
              </div>
            </div>

            <div class="rf-field" style="margin-top: 16px">
              <label>招标要求的社保证明覆盖月份（多选或直接输入 YYYY-MM 后回车）</label>
              <el-select
                v-model="ruleForm.required_social_months_json"
                multiple
                filterable
                allow-create
                default-first-option
                placeholder="选择或输入社保所属期月份（如 2026-08）"
                style="width: 100%"
              >
                <el-option v-for="m in AVAILABLE_MONTHS" :key="m" :label="m" :value="m" />
              </el-select>
            </div>

            <!-- 自定义红线动态行编辑 -->
            <div class="custom-redlines-section">
              <div class="crs-hd">
                <div>
                  <h4>部门自定义禁用词与跨项目串名红线列表（{{ ruleForm.custom_redlines_json.length }} 条）</h4>
                  <p>投标书全文一旦命中以下禁用关键词（如历史投标项目的采购单位名、竞争对手品牌名），立即触发告警。</p>
                </div>
                <el-button size="small" type="primary" plain @click="addRedlineRow">
                  + 新增红线词行
                </el-button>
              </div>

              <el-table
                :data="ruleForm.custom_redlines_json"
                border
                size="small"
                style="width: 100%"
              >
                <el-table-column label="禁用关键词 / 历史外部单位名称 (keyword)" min-width="260">
                  <template #default="{ row }">
                    <el-input
                      v-model="row.keyword"
                      size="small"
                      placeholder="例如：沈阳市某某大数据局 / 某某历史项目名称"
                    />
                  </template>
                </el-table-column>

                <el-table-column label="严重级别 (severity)" width="180">
                  <template #default="{ row }">
                    <el-select v-model="row.severity" size="small" style="width: 100%">
                      <el-option label="error · 废标红线" value="error" />
                      <el-option label="warning · 扣分预警" value="warning" />
                      <el-option label="info · 一般提示" value="info" />
                    </el-select>
                  </template>
                </el-table-column>

                <el-table-column label="评分后果 (consequence)" width="190">
                  <template #default="{ row }">
                    <el-select v-model="row.consequence" size="small" style="width: 100%">
                      <el-option label="veto · ★一票否决" value="veto" />
                      <el-option label="deduction · ▲重要扣分" value="deduction" />
                      <el-option label="bonus · ▲重要加分" value="bonus" />
                      <el-option label="none · 无评分后果" value="none" />
                    </el-select>
                  </template>
                </el-table-column>

                <el-table-column label="操作" width="90" align="center">
                  <template #default="{ $index }">
                    <el-button link type="danger" @click="removeRedlineRow($index)">
                      删除行
                    </el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </div>
        </el-tab-pane>

        <!-- 页签 ②：企业投标证照效期台账 -->
        <el-tab-pane label="② 企业投标证照效期台账" name="certs">
          <div v-loading="loadingCerts" class="cert-pane">
            <div class="rp-top-bar">
              <div>
                <h3>企业常备投标证照与信创互认证书效期台账</h3>
                <p>自动计算距今剩余有效天数；距离到期不足预警天数（warn_days）的行自动以橙色高亮预警。</p>
              </div>

              <div class="cert-actions">
                <el-checkbox v-model="expiringOnly" @change="fetchCerts">
                  仅看临期 / 已过期证照
                </el-checkbox>
                <el-button size="small" @click="fetchCerts">刷新</el-button>
                <el-button type="primary" size="small" @click="openCreateCert">
                  + 新增企业证照
                </el-button>
              </div>
            </div>

            <el-table
              :data="certs"
              border
              :row-class-name="certRowClassName"
              style="width: 100%"
            >
              <el-table-column prop="id" label="ID" width="70">
                <template #default="{ row }">
                  <span class="mono-num">#{{ row.id }}</span>
                </template>
              </el-table-column>

              <el-table-column prop="cert_name" label="证照 / 资质证书名称" min-width="240" />
              <el-table-column prop="cert_no" label="证书编号" width="190">
                <template #default="{ row }">
                  <span class="mono-num">{{ row.cert_no || '—' }}</span>
                </template>
              </el-table-column>

              <el-table-column prop="valid_until" label="有效期截止日" width="140">
                <template #default="{ row }">
                  <span v-if="row.is_long_term" class="sev-pill ok">长期有效</span>
                  <span v-else class="mono-num">{{ row.valid_until || '—' }}</span>
                </template>
              </el-table-column>

              <el-table-column label="剩余天数与状态" width="210">
                <template #default="{ row }">
                  <span v-if="row.is_long_term" class="sev-pill ok">
                    长期有效 · 持续有效
                  </span>
                  <span v-else-if="row.is_expired" class="sev-pill error">
                    已过期 {{ Math.abs(row.days_remaining ?? 0) }} 天
                  </span>
                  <span v-else-if="row.is_expiring_soon" class="sev-pill warning">
                    临期预警 · 仅剩 {{ row.days_remaining ?? 0 }} 天
                  </span>
                  <span v-else class="sev-pill ok">
                    有效 · 剩余 {{ row.days_remaining ?? 0 }} 天
                  </span>
                </template>
              </el-table-column>

              <el-table-column prop="warn_days" label="预警阈值" width="100">
                <template #default="{ row }">
                  <span v-if="row.is_long_term" class="muted-txt">—</span>
                  <span v-else class="mono-num">≤ {{ row.warn_days }} 天</span>
                </template>
              </el-table-column>

              <el-table-column prop="remark" label="适用投标场景备注" min-width="180" />

              <el-table-column label="原件留档" min-width="170">
                <template #default="{ row }">
                  <div v-if="row.has_attachment" class="cert-att-cell">
                    <el-button link type="primary" @click="handleDownloadCertAttachment(row)">
                      下载原件 ({{ row.attachment_name || '已留档' }})
                    </el-button>
                    <el-button link type="danger" @click="handleDeleteCertAttachment(row)">
                      解绑
                    </el-button>
                  </div>
                  <span v-else class="muted-txt">未上传</span>
                </template>
              </el-table-column>

              <el-table-column label="操作" width="140" fixed="right">
                <template #default="{ row }">
                  <el-button link type="primary" @click="openEditCert(row)">编辑</el-button>
                  <el-button link type="danger" @click="handleDeleteCert(row)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>

        <!-- 页签 ③：E4 法规库浏览、法条三态真伪核验与官网更新 -->
        <el-tab-pane label="③ 法规库与法条核验" name="regulations">
          <div class="regulations-pane">
            <!-- 1. 法条三态真伪核验区（exact_match 绿勾 / fuzzy_hint 灰标 / unverifiable 红标） -->
            <div class="verify-box">
              <div class="vb-hd">
                <div>
                  <h4>法条真伪三态溯源核验台（LLM 零真伪裁决权 · 纯确定性底表对账）</h4>
                  <p>
                    粘贴任意法律依据或条文文本（自动兼容“第1条”与“第一条”变体），实时与本地权威法规库逐字对账。
                  </p>
                </div>
                <el-button type="primary" :loading="verifying" @click="handleVerifyArticle">
                  立即核验法条真伪
                </el-button>
              </div>

              <el-input
                v-model="verifyInputText"
                type="textarea"
                :rows="2"
                placeholder="粘贴待核验法条，例如：《中华人民共和国招标投标法》第1条 为了规范招标投标活动，保护国家利益、社会公共利益和招标投标活动当事人的合法权益，提高经济效益，保证项目质量，制定本法。"
              />

              <!-- 三态核验结果展示卡片 -->
              <div
                v-if="verifyResult"
                class="verify-result-card"
                :class="verifyResult.status"
              >
                <div class="vr-top">
                  <!-- 状态 1：exact_match 绿勾 -->
                  <span v-if="verifyResult.status === 'exact_match'" class="vr-badge exact">
                    ✓ 库内原文（{{ verifyResult.law }}{{ verifyResult.article_no }}）
                  </span>
                  <!-- 状态 2：fuzzy_hint 灰标 -->
                  <span v-else-if="verifyResult.status === 'fuzzy_hint'" class="vr-badge fuzzy">
                    疑似系{{ verifyResult.law }}{{ verifyResult.article_no }}改写，未经核验
                  </span>
                  <!-- 状态 3：unverifiable 红标 -->
                  <span v-else class="vr-badge unverified">
                    ✕ 库中未收录，真伪未经核验
                  </span>

                  <span class="vr-sim mono-num">
                    匹配度：{{ (verifyResult.similarity * 100).toFixed(1) }}%
                  </span>
                </div>

                <div class="vr-msg">{{ verifyResult.message }}</div>

                <div v-if="verifyResult.official_content" class="vr-official">
                  <b>库内权威原文（{{ verifyResult.law }} · {{ verifyResult.article_no }}）：</b>
                  <span>{{ verifyResult.official_content }}</span>
                </div>
              </div>
            </div>

            <!-- 2. 顶部管理员操作条：检查官网更新 + 手动上传 json/txt/docx/pdf 四格式法规 -->
            <div class="rp-top-bar" style="margin-top: 18px">
              <div>
                <h3>招投标权威法律法规底表（已收录 {{ laws.length }} 部法规）</h3>
                <p>点击左侧法规卡片可浏览全部条文并按关键词检索；点击任意条文可一键填入上方核验台测试。</p>
              </div>

              <div v-if="authStore.isAdmin" class="admin-reg-actions">
                <el-input
                  v-model.trim="importLawNameHint"
                  size="small"
                  placeholder="导入法规名称（可选）"
                  style="width: 180px"
                />
                <label class="upload-law-btn">
                  <span>{{ importingFile ? '导入解析中…' : '+ 上传法规 (json/txt/docx/pdf)' }}</span>
                  <input
                    type="file"
                    accept=".json,.txt,.docx,.pdf"
                    style="display: none"
                    :disabled="importingFile"
                    @change="handleImportLawFile"
                  />
                </label>
                <el-button
                  size="small"
                  type="primary"
                  plain
                  :loading="crawling"
                  @click="handleCrawlUpdates"
                >
                  检查官网更新
                </el-button>
              </div>
            </div>

            <!-- 官网更新检查结果面板 -->
            <div v-if="crawlReport" class="crawl-report-box">
              <div class="crb-title">
                官网更新检查报告：共检查 {{ crawlReport.total_sources }} 个配置源，
                更新 {{ crawlReport.updated_laws_count }} 部，待配置占位源 {{ crawlReport.unconfigured_count }} 个
              </div>
              <div class="crb-grid">
                <div v-for="item in crawlReport.results" :key="item.name" class="crb-item">
                  <span
                    class="sev-pill"
                    :class="
                      item.status === 'updated' || item.status === 'unchanged'
                        ? 'ok'
                        : item.status === 'unconfigured'
                          ? 'info'
                          : 'warning'
                    "
                  >
                    {{ item.status }}
                  </span>
                  <b>{{ item.name }}</b>
                  <span class="crb-msg">{{ item.message }}</span>
                </div>
              </div>
            </div>

            <!-- 3. 左法规列表卡 + 右条文浏览与关键词检索分栏 -->
            <div v-loading="loadingLaws" class="reg-split-layout">
              <!-- 左栏：法规列表卡片 -->
              <div class="law-cards-col">
                <div
                  v-for="law in laws"
                  :key="law.id"
                  class="law-card-item"
                  :class="{ active: law.id === selectedLawId }"
                  @click="handleSelectLaw(law)"
                >
                  <div class="lc-row1">
                    <span class="lc-name">{{ law.name }}</span>
                    <div class="lc-row1-right">
                      <span class="sev-pill info">{{ law.article_count }} 条</span>
                      <el-button
                        v-if="authStore.isAdmin"
                        type="danger"
                        link
                        size="small"
                        title="删除该部法规及全部条文"
                        @click.stop="handleDeleteLaw(law)"
                      >
                        删除
                      </el-button>
                    </div>
                  </div>
                  <div class="lc-meta">
                    <div>发文机关：{{ law.issuing_body || '国家立法机关' }}</div>
                    <div class="lc-source" :title="law.source_url || '内置种子'">
                      来源：<code>{{ law.source_url || '内置种子' }}</code>
                    </div>
                    <div v-if="law.updated_at">
                      更新：{{ law.updated_at.slice(0, 19).replace('T', ' ') }}
                    </div>
                  </div>
                </div>

                <el-empty v-if="laws.length === 0" description="法规库为空" :image-size="60" />
              </div>

              <!-- 右栏：选中法规的条文浏览与关键词检索 -->
              <div v-loading="loadingArticles" class="law-articles-col">
                <div class="la-toolbar">
                  <div class="la-current-title">
                    <b>{{ selectedLaw?.name || '请选择左侧法规' }}</b>
                    <span class="mono-num">（共 {{ totalArticles }} 条匹配）</span>
                  </div>

                  <div class="la-search">
                    <el-input
                      v-model="articleKeyword"
                      size="small"
                      clearable
                      placeholder="按条号（如 第三十三条）或正文关键词检索…"
                      style="width: 260px"
                      @keyup.enter="handleSearchArticles"
                      @clear="handleSearchArticles"
                    />
                    <el-button size="small" type="primary" @click="handleSearchArticles">
                      检索条文
                    </el-button>
                  </div>
                </div>

                <div class="articles-scroll-list">
                  <div
                    v-for="art in articles"
                    :key="art.id"
                    class="art-item"
                  >
                    <div class="art-head">
                      <span class="art-no">{{ art.article_no }}</span>
                      <span class="art-hash mono-num">SHA256: {{ art.content_hash.slice(0, 10) }}</span>
                      <el-button
                        size="small"
                        link
                        type="primary"
                        style="margin-left: auto"
                        @click="fillSampleForVerify(art)"
                      >
                        送入核验台测试 ↑
                      </el-button>
                      <el-button
                        v-if="authStore.isAdmin"
                        size="small"
                        link
                        type="danger"
                        title="管理员删除该条法规条文"
                        @click="handleDeleteArticle(art)"
                      >
                        删除
                      </el-button>
                    </div>
                    <div class="art-content">{{ art.content }}</div>
                  </div>

                  <el-empty
                    v-if="articles.length === 0"
                    description="未检索到匹配的法条记录"
                    :image-size="64"
                  />
                </div>

                <div v-if="totalArticles > articlePageSize" class="art-pager">
                  <el-pagination
                    small
                    background
                    layout="total, prev, pager, next"
                    :total="totalArticles"
                    :page-size="articlePageSize"
                    :current-page="articlePage"
                    @current-change="handleArticlePageChange"
                  />
                </div>
              </div>
            </div>
          </div>
        </el-tab-pane>

        <!-- 页签 ④：企业指标与业务线评分库在线维护及人工终审台 -->
        <el-tab-pane label="④ 企业指标与业务线评分库" name="indicators">
          <div v-loading="loadingIndicatorLines" class="indicators-pane">
            <div class="rp-top-bar">
              <div>
                <h3>企业指标与业务线评分库管理（F8 标书编制标准应答底座）</h3>
                <p>
                  在线维护跨业务线通用与垂直专精指标应答、评分参考及待确认槽位；所有保存操作均强制经过金额防造假锁。
                </p>
              </div>
              <div class="cert-actions">
                <el-button size="small" @click="fetchIndicatorLines(true)">刷新</el-button>
                <el-button
                  v-if="authStore.isAdmin"
                  size="small"
                  type="primary"
                  plain
                  @click="openCreateIndicatorLineDialog"
                >
                  + 新增业务线
                </el-button>
              </div>
            </div>

            <div class="reg-split-layout">
              <!-- 左栏：业务线列表 -->
              <div class="law-cards-col">
                <div
                  v-for="lineItem in indicatorLines"
                  :key="lineItem.line"
                  class="law-card-item"
                  :class="{ active: lineItem.line === selectedLineKey }"
                  @click="handleSelectIndicatorLine(lineItem.line)"
                >
                  <div class="lc-row1">
                    <span class="lc-name">{{ lineItem.display_name }}</span>
                    <span
                      class="sev-pill"
                      :class="lineItem.review_status === 'approved' ? 'ok' : 'warning'"
                    >
                      {{ lineItem.review_status === 'approved' ? '✓ 已人工终审' : '待人工终审' }}
                    </span>
                  </div>
                  <div class="lc-meta">
                    <div>
                      业务线标识：<code>{{ lineItem.line }}</code>
                      <span v-if="lineItem.is_builtin" class="ind-builtin-tag">内置</span>
                    </div>
                    <div>
                      标准指标：<b>{{ lineItem.total_indicators }}</b> 条 ｜ 含待确认槽位：<b>{{
                        lineItem.pending_slot_count
                      }}</b> 条
                    </div>
                    <div v-if="lineItem.reviewed_at">
                      终审：{{ lineItem.reviewed_by || 'admin' }} ·
                      {{ lineItem.reviewed_at.slice(0, 19).replace('T', ' ') }}
                    </div>
                  </div>
                </div>
              </div>

              <!-- 右栏：当前业务线详情与条目 CRUD -->
              <div v-if="currentIndicatorLine" class="law-articles-col">
                <div class="ind-line-banner" :class="currentIndicatorLine.review_status">
                  <div class="ilb-left">
                    <div class="ilb-title-row">
                      <h4>{{ currentIndicatorLine.display_name }}</h4>
                      <code>{{ currentIndicatorLine.line }}.json</code>
                      <span
                        class="sev-pill"
                        :class="currentIndicatorLine.review_status === 'approved' ? 'ok' : 'warning'"
                      >
                        {{
                          currentIndicatorLine.review_status === 'approved'
                            ? '✓ 已签署人工终审 (approved)'
                            : '⚠ 待人工终审 (pending_human_review)'
                        }}
                      </span>
                    </div>
                    <p class="ilb-desc">{{ currentIndicatorLine.description }}</p>
                    <div class="ilb-meta-row">
                      <span>共 <b>{{ currentIndicatorLine.total_indicators }}</b> 条标准指标</span>
                      <span>
                        含 <code>[待人工确认/填报：...]</code> 安全槽位
                        <b>{{ currentIndicatorLine.pending_slot_count }}</b> 条
                      </span>
                      <span v-if="currentIndicatorLine.content_hash" class="mono-num">
                        SHA256: {{ currentIndicatorLine.content_hash }}
                      </span>
                      <span v-if="currentIndicatorLine.reviewed_at">
                        签批人：<b>{{ currentIndicatorLine.reviewed_by || 'admin' }}</b> ({{
                          currentIndicatorLine.reviewed_at.slice(0, 19).replace('T', ' ')
                        }})
                      </span>
                    </div>
                  </div>
                  <div v-if="authStore.isAdmin" class="ilb-actions">
                    <el-button
                      size="small"
                      type="success"
                      :plain="currentIndicatorLine.review_status === 'approved'"
                      :loading="approvingLine"
                      @click="handleApproveCurrentLine"
                    >
                      {{
                        currentIndicatorLine.review_status === 'approved'
                          ? '重新签署终审'
                          : '✓ 签署人工终审通过'
                      }}
                    </el-button>
                    <el-button size="small" type="primary" @click="openCreateIndicatorEntry">
                      + 新增标准指标
                    </el-button>
                  </div>
                </div>

                <!-- 典型评分权重参考 -->
                <div
                  v-if="currentIndicatorLine.line_scoring_summary?.length"
                  class="ind-scoring-summary-bar"
                >
                  <div
                    v-for="(sc, sIdx) in currentIndicatorLine.line_scoring_summary"
                    :key="sIdx"
                    class="iss-chip"
                  >
                    <div class="iss-top">
                      <b>{{ sc.dimension }}</b>
                      <span class="mono-num">{{ sc.typical_weight }}</span>
                    </div>
                    <div class="iss-focus">{{ sc.key_focus }}</div>
                  </div>
                </div>

                <!-- 检索与分类筛选栏 -->
                <div class="la-toolbar">
                  <div class="la-current-title">
                    <b>指标与标准应答列表</b>
                    <span class="mono-num">（当前显示 {{ filteredIndicators.length }} 条）</span>
                  </div>
                  <div class="la-search">
                    <el-select
                      v-model="indicatorCategoryFilter"
                      size="small"
                      clearable
                      placeholder="按章节分类筛选"
                      style="width: 155px"
                    >
                      <el-option
                        v-for="cat in availableIndicatorCategories"
                        :key="cat"
                        :label="cat"
                        :value="cat"
                      />
                    </el-select>
                    <el-input
                      v-model="indicatorSearchKeyword"
                      size="small"
                      clearable
                      placeholder="按指标名称 / 应答正文 / 关键词检索…"
                      style="width: 240px"
                    />
                  </div>
                </div>

                <!-- 指标卡片列表 -->
                <div class="articles-scroll-list">
                  <div
                    v-for="ind in filteredIndicators"
                    :key="ind.indicator_id"
                    class="art-item ind-entry-card"
                  >
                    <div class="art-head">
                      <span class="art-no">{{ ind.indicator_id }}</span>
                      <span class="sev-pill info">{{ ind.category }}</span>
                      <b class="ind-entry-title">{{ ind.indicator_name }}</b>
                      <span v-if="ind.scoring_ref?.weight_hint" class="ind-weight-pill mono-num">
                        典型分值：{{ ind.scoring_ref.weight_hint }}
                      </span>
                      <span v-if="hasPendingSlotInEntry(ind)" class="sev-pill warning">
                        含待人工确认槽位
                      </span>
                      <div v-if="authStore.isAdmin" style="margin-left: auto; display: flex; gap: 6px">
                        <el-button
                          size="small"
                          link
                          type="primary"
                          @click="openEditIndicatorEntry(ind)"
                        >
                          编辑
                        </el-button>
                        <el-button
                          size="small"
                          link
                          type="danger"
                          @click="handleDeleteIndicatorEntry(ind)"
                        >
                          删除
                        </el-button>
                      </div>
                    </div>

                    <div class="art-content ind-resp-body">{{ ind.standard_response }}</div>

                    <div
                      v-if="ind.scoring_ref?.typical_points?.length || ind.scoring_ref?.high_score_tips"
                      class="ind-scoring-ref-box"
                    >
                      <div v-if="ind.scoring_ref?.typical_points?.length">
                        <b>典型评分点：</b>{{ ind.scoring_ref.typical_points.join('；') }}
                      </div>
                      <div v-if="ind.scoring_ref?.high_score_tips">
                        <b>高分编制要领：</b>{{ ind.scoring_ref.high_score_tips }}
                      </div>
                    </div>

                    <div class="ind-footer-tags">
                      <span
                        v-for="(slot, pIdx) in ind.param_slots || []"
                        :key="'s-' + pIdx"
                        class="ind-slot-chip"
                      >
                        {{ slot }}
                      </span>
                      <span
                        v-for="(kw, kIdx) in ind.keywords || []"
                        :key="'k-' + kIdx"
                        class="ind-kw-chip"
                      >
                        #{{ kw }}
                      </span>
                    </div>
                  </div>

                  <el-empty
                    v-if="filteredIndicators.length === 0"
                    description="当前业务线暂无匹配的指标条目"
                    :image-size="60"
                  />
                </div>
              </div>
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>

    <!-- 新增自定义业务线弹窗 -->
    <el-dialog v-model="lineDialogVisible" title="新增自定义业务线指标库" width="480px">
      <el-form label-position="top">
        <el-form-item label="业务线英文标识 (line · 如 cloud_ops / gov_data)" required>
          <el-input
            v-model="newLineForm.line"
            placeholder="2~24 位小写字母、数字或下划线，以字母开头"
          />
        </el-form-item>
        <el-form-item label="业务线显示名称 (display_name)" required>
          <el-input
            v-model="newLineForm.display_name"
            placeholder="例如：政务云与算力底座专精线"
          />
        </el-form-item>
        <el-form-item label="适用范围与说明 (description)">
          <el-input
            v-model="newLineForm.description"
            type="textarea"
            :rows="2"
            placeholder="简要说明该业务线适用的招标项目类型与核心技术栈…"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="lineDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingLine" @click="handleCreateIndicatorLine">
          创建业务线
        </el-button>
      </template>
    </el-dialog>

    <!-- 新增/编辑标准指标条目弹窗 -->
    <el-dialog
      v-model="entryDialogVisible"
      :title="editingIndicatorId === null ? '新增企业标准指标应答' : `编辑标准指标 (${editingIndicatorId})`"
      width="680px"
    >
      <div class="ind-anti-forgery-alert">
        <b>防造假红线锁已启用：</b>标准指标库严禁写入具体人民币投标报价数字（如
        <code>46000元</code>、<code>￥12.8万元</code>、<code>15元/M Tokens</code>）；任何包含具体价格的内容在保存时将由系统确定性替换为
        <code>[待人工填报：价格/商务数值须由项目组按实际报价填报]</code>。未经公司证实的硬指标请使用
        <code>[待人工确认：...]</code> 槽位。
      </div>
      <el-form label-position="top" style="margin-top: 12px">
        <div class="rule-grid-2">
          <el-form-item label="所属章节分类 (category)" required>
            <el-input
              v-model="entryForm.category"
              placeholder="例如：技术方案 / 实施方案 / 售后服务 / 培训方案"
            />
          </el-form-item>
          <el-form-item label="指标编号 (indicator_id · 留空自动生成)">
            <el-input
              v-model="entryForm.indicator_id"
              :disabled="editingIndicatorId !== null"
              placeholder="例如：SEC-009（留空自动编号）"
            />
          </el-form-item>
        </div>

        <el-form-item label="标准指标名称 (indicator_name)" required>
          <el-input
            v-model="entryForm.indicator_name"
            placeholder="例如：零信任架构与多因子身份鉴别技术方案"
          />
        </el-form-item>

        <el-form-item label="标准应答正文 (standard_response)" required>
          <el-input
            v-model="entryForm.standard_response"
            type="textarea"
            :rows="5"
            placeholder="填写规范化投标技术/商务应答正文，待项目组确定的参数请以 [待人工确认：...] 标注…"
          />
        </el-form-item>

        <div class="rule-grid-2">
          <el-form-item label="典型分值权重参考 (weight_hint)">
            <el-input v-model="entryForm.weight_hint" placeholder="例如：6~10分" />
          </el-form-item>
          <el-form-item label="检索触发关键词 (keywords · 逗号分隔)">
            <el-input
              v-model="entryForm.keywords_text"
              placeholder="例如：零信任，身份鉴别，IAM，最小权限"
            />
          </el-form-item>
        </div>

        <div class="rule-grid-2">
          <el-form-item label="典型评分考察点 (typical_points · 每行一条)">
            <el-input
              v-model="entryForm.typical_points_text"
              type="textarea"
              :rows="3"
              placeholder="每行填写一条招标常见评分点…"
            />
          </el-form-item>
          <el-form-item label="待人工确认参数槽 (param_slots · 每行一个)">
            <el-input
              v-model="entryForm.param_slots_text"
              type="textarea"
              :rows="3"
              placeholder="例如：[待人工确认：并发吞吐性能指标]"
            />
          </el-form-item>
        </div>

        <el-form-item label="高分编制要领 (high_score_tips)">
          <el-input
            v-model="entryForm.high_score_tips"
            placeholder="例如：建议配合架构流转表与权限矩阵表佐证"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="entryDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingEntry" @click="handleSaveIndicatorEntry">
          保存指标条目
        </el-button>
      </template>
    </el-dialog>

    <!-- 新增/编辑证照弹窗 -->
    <el-dialog
      v-model="certDialogVisible"
      :title="editingCertId === null ? '新增企业投标证照' : '编辑企业投标证照'"
      width="520px"
    >
      <el-form label-position="top">
        <el-form-item label="证照名称" required>
          <el-input
            v-model="certForm.cert_name"
            placeholder="例如：ISO/IEC 27001 信息安全管理体系认证证书"
            @input="certNameSource = null"
          />
          <div v-if="certNameSource === 'filename_fallback'" class="cert-source-warn">
            名称来自文件名兜底，请人工核对
          </div>
        </el-form-item>

        <el-form-item label="证书编号">
          <el-input v-model="certForm.cert_no" placeholder="例如：ISO27001-CN-2026001" />
        </el-form-item>

        <div style="margin-bottom: 10px">
          <el-checkbox v-model="certForm.is_long_term" @change="onLongTermToggle">
            长期有效（无固定到期日，如新版营业执照）
          </el-checkbox>
        </div>

        <div class="rule-grid-2">
          <el-form-item label="有效期截止日期 (YYYY-MM-DD)" :required="!certForm.is_long_term">
            <el-input
              v-model="certForm.valid_until"
              :disabled="certForm.is_long_term"
              :placeholder="certForm.is_long_term ? '长期有效，无需填写截止日' : '例如：2028-12-31'"
            />
          </el-form-item>

          <el-form-item label="提前预警天数 (warn_days)">
            <el-input v-model.number="certForm.warn_days" type="number" :disabled="certForm.is_long_term" />
          </el-form-item>
        </div>

        <el-form-item label="备注说明">
          <el-input v-model="certForm.remark" placeholder="例如：政企安全项目资格审查必附项" />
        </el-form-item>

        <el-form-item label="上传证照原件扫件（可选 · PDF / PNG / JPG ≤20MB，支持 OCR 自动预填）">
          <div class="cert-upload-box">
            <label class="cert-upload-btn">
              <span>
                {{
                  parsingCertFile
                    ? 'OCR 识别预填中…'
                    : pendingCertFile
                      ? `已选：${pendingCertFile.name}`
                      : '+ 选择 PDF / 图片扫件自动预填并留档'
                }}
              </span>
              <input
                type="file"
                accept=".pdf,.png,.jpg,.jpeg"
                style="display: none"
                :disabled="parsingCertFile || savingCert"
                @change="onCertFileChange"
              />
            </label>
            <div v-if="editingExistingAttachmentName && !pendingCertFile" class="cert-upload-hint">
              当前已绑定原件：<b>{{ editingExistingAttachmentName }}</b>（重新选择文件可覆盖更新）
            </div>
            <div v-if="certParseHintSummary" class="cert-upload-hint ok">
              {{ certParseHintSummary }}
            </div>
          </div>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="certDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingCert" @click="handleSaveCert">
          保存证照
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.rc-main-card {
  padding: 20px 24px;
}

.rp-top-bar {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  flex-wrap: wrap;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--border-line);
  margin-bottom: 18px;
}

.rp-top-bar h3 {
  font-size: 16px;
  font-weight: 900;
  color: var(--txt-strong);
}

.rp-top-bar p {
  font-size: 12.5px;
  color: var(--txt-sec);
  margin-top: 3px;
}

.rule-grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.rf-field label {
  display: block;
  font-size: 12.5px;
  font-weight: 800;
  color: var(--txt-strong);
  margin-bottom: 6px;
}

.money-preview {
  font-size: 11.5px;
  color: var(--brand);
  margin-top: 4px;
  font-weight: 700;
}

.custom-redlines-section {
  margin-top: 24px;
  padding-top: 18px;
  border-top: 1px dashed var(--border-strong);
}

.crs-hd {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  gap: 12px;
  flex-wrap: wrap;
}

.crs-hd h4 {
  font-size: 14.5px;
  font-weight: 900;
  color: var(--txt-strong);
}

.crs-hd p {
  font-size: 12px;
  color: var(--txt-muted);
  margin-top: 2px;
}

.cert-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

:deep(.cert-expiring-row) {
  background-color: #fff7ed !important;
}

:deep(.cert-expired-row) {
  background-color: #fef2f2 !important;
}

/* ── 页签 ③：法规库与三态核验样式 ── */
.verify-box {
  padding: 16px 18px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-line);
  border-radius: 10px;
}

.vb-hd {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 10px;
}

.vb-hd h4 {
  font-size: 14.5px;
  font-weight: 900;
  color: var(--txt-strong);
}

.vb-hd p {
  font-size: 12px;
  color: var(--txt-sec);
  margin-top: 2px;
}

.verify-result-card {
  margin-top: 12px;
  padding: 12px 15px;
  border-radius: 8px;
  border: 1px solid var(--border-line);
  border-left: 4px solid var(--txt-muted);
  background: #ffffff;
}

.verify-result-card.exact_match {
  border-left-color: var(--sev-ok);
  background: #f0fdf4;
}

.verify-result-card.fuzzy_hint {
  border-left-color: #64748b;
  background: #f8fafc;
}

.verify-result-card.unverifiable {
  border-left-color: var(--sev-error);
  background: #fef2f2;
}

.vr-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.vr-badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 800;
}

.vr-badge.exact {
  background: var(--sev-ok);
  color: #ffffff;
}

.vr-badge.fuzzy {
  background: #cbd5e1;
  color: #1e293b;
}

.vr-badge.unverified {
  background: var(--sev-error);
  color: #ffffff;
}

.vr-sim {
  font-size: 12px;
  font-weight: 800;
  color: var(--txt-sec);
}

.vr-msg {
  font-size: 13px;
  font-weight: 700;
  color: var(--txt-strong);
  margin-bottom: 6px;
}

.vr-official {
  font-size: 12.5px;
  color: var(--txt-body);
  background: rgba(255, 255, 255, 0.75);
  padding: 8px 10px;
  border-radius: 6px;
  border: 1px dashed var(--border-strong);
  line-height: 1.6;
}

.admin-reg-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.upload-law-btn {
  display: inline-flex;
  align-items: center;
  height: 28px;
  padding: 0 11px;
  border-radius: 4px;
  border: 1px solid var(--brand);
  background: var(--brand-soft);
  color: var(--brand);
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}

.crawl-report-box {
  margin-bottom: 16px;
  padding: 12px 16px;
  background: #eff6ff;
  border: 1px solid #bfdbfe;
  border-radius: 8px;
}

.crb-title {
  font-size: 13px;
  font-weight: 800;
  color: #1e3a8a;
  margin-bottom: 8px;
}

.crb-grid {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.crb-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}

.crb-msg {
  color: var(--txt-sec);
}

.reg-split-layout {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 16px;
  align-items: start;
}

.law-cards-col {
  position: sticky;
  top: 16px;
  align-self: start;
  max-height: calc(100vh - 120px);
  overflow-y: auto;
  padding-right: 4px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

@media (max-width: 1100px) {
  .reg-split-layout {
    grid-template-columns: 1fr;
  }

  .law-cards-col {
    position: static;
    max-height: none;
    overflow-y: visible;
    padding-right: 0;
  }
}

.law-card-item {
  padding: 12px 14px;
  border-radius: 8px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
  cursor: pointer;
  transition: all 0.15s;
}

.law-card-item:hover {
  border-color: var(--brand);
  background: #ffffff;
}

.law-card-item.active {
  border-color: var(--brand);
  background: var(--brand-soft);
  box-shadow: 0 0 0 2px rgba(29, 78, 216, 0.12);
}

.lc-row1 {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 6px;
}

.lc-row1-right {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.lc-name {
  font-size: 13.5px;
  font-weight: 900;
  color: var(--txt-strong);
}

.lc-meta {
  font-size: 11.5px;
  color: var(--txt-sec);
  line-height: 1.5;
  min-width: 0;
}

.lc-source {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.lc-meta code {
  font-size: 11px;
}

.law-articles-col {
  min-width: 0;
  border: 1px solid var(--border-line);
  border-radius: 8px;
  padding: 14px 16px;
  background: #ffffff;
}

.la-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-line);
  margin-bottom: 12px;
}

.la-search {
  display: flex;
  gap: 8px;
  align-items: center;
}

.articles-scroll-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.art-item {
  padding: 10px 12px;
  border-radius: 6px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
}

.art-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 4px;
}

.art-no {
  font-size: 13px;
  font-weight: 900;
  color: var(--brand);
}

.art-hash {
  font-size: 11px;
  color: var(--txt-muted);
}

.art-content {
  font-size: 13px;
  color: var(--txt-body);
  line-height: 1.65;
}

.art-pager {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}

.cert-upload-box {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.cert-upload-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 7px 14px;
  border-radius: 6px;
  border: 1px dashed var(--brand);
  background: var(--brand-soft);
  color: var(--brand);
  font-size: 12.5px;
  font-weight: 700;
  cursor: pointer;
}

.cert-upload-hint {
  font-size: 12px;
  color: var(--txt-sec);
}

.cert-upload-hint.ok {
  color: var(--sev-ok);
  font-weight: 600;
}

.cert-att-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.muted-txt {
  font-size: 12px;
  color: var(--txt-muted);
}

.cert-source-warn {
  margin-top: 4px;
  font-size: 12px;
  color: #b45309;
  font-weight: 600;
}

/* ── 页签 ④：企业指标与业务线评分库样式 ── */
.ind-builtin-tag {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 4px;
  background: #e2e8f0;
  color: #334155;
  font-size: 10.5px;
  font-weight: 700;
}

.ind-line-banner {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 14px;
  flex-wrap: wrap;
  padding: 14px 16px;
  border-radius: 8px;
  border: 1px solid var(--border-line);
  border-left: 4px solid #f59e0b;
  background: #fffbeb;
  margin-bottom: 14px;
}

.ind-line-banner.approved {
  border-left-color: var(--sev-ok);
  background: #f0fdf4;
}

.ilb-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}

.ilb-title-row h4 {
  font-size: 15px;
  font-weight: 900;
  color: var(--txt-strong);
  margin: 0;
}

.ilb-desc {
  font-size: 12.5px;
  color: var(--txt-body);
  margin: 4px 0 6px;
  line-height: 1.55;
}

.ilb-meta-row {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--txt-sec);
}

.ilb-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.ind-scoring-summary-bar {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 10px;
  margin-bottom: 14px;
}

.iss-chip {
  padding: 10px 12px;
  border-radius: 6px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
}

.iss-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12.5px;
  color: var(--txt-strong);
  margin-bottom: 4px;
}

.iss-top span {
  color: var(--brand);
  font-weight: 800;
}

.iss-focus {
  font-size: 11.5px;
  color: var(--txt-sec);
  line-height: 1.45;
}

.ind-entry-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.ind-entry-title {
  font-size: 13.5px;
  font-weight: 900;
  color: var(--txt-strong);
}

.ind-weight-pill {
  padding: 1px 8px;
  border-radius: 4px;
  background: #eff6ff;
  color: #1d4ed8;
  font-size: 11.5px;
  font-weight: 800;
}

.ind-resp-body {
  white-space: pre-wrap;
}

.ind-scoring-ref-box {
  margin-top: 2px;
  padding: 8px 10px;
  border-radius: 6px;
  background: #ffffff;
  border: 1px dashed var(--border-strong);
  font-size: 12px;
  color: var(--txt-sec);
  line-height: 1.55;
}

.ind-footer-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 2px;
}

.ind-slot-chip {
  padding: 2px 8px;
  border-radius: 4px;
  background: #fef3c7;
  color: #92400e;
  border: 1px solid #fde68a;
  font-size: 11px;
  font-weight: 700;
}

.ind-kw-chip {
  padding: 2px 7px;
  border-radius: 4px;
  background: #f1f5f9;
  color: #475569;
  font-size: 11px;
}

.ind-anti-forgery-alert {
  padding: 10px 12px;
  border-radius: 6px;
  border: 1px solid #fde68a;
  background: #fffbeb;
  color: #92400e;
  font-size: 12px;
  line-height: 1.6;
}
</style>
