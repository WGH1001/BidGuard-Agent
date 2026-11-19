<script setup lang="ts">
import { computed, nextTick, onActivated, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { SSEProgressFrame } from '@/api/types'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { checksApi, reportsApi } from '@/api'
import type {
  BidFindingOut,
  BidTaskOut,
  BidTaskStats,
  PriceManualBackfillOut,
  ScoreCriterionItemOut,
  ScoreEstimateOut,
} from '@/api/types'
import { useAppStore } from '@/stores/app'
import { useNotificationStore } from '@/stores/notifications'

import CheckTaskList from '@/components/CheckTaskList.vue'
import SseProgress from '@/components/SseProgress.vue'
import RiskCard from '@/components/RiskCard.vue'
import PriceManualDialog from '@/components/PriceManualDialog.vue'

defineOptions({ name: 'CheckDetailView' })

interface TaskDurationRecord {
  f1_ms?: number | null
  f_parse_ms?: number | null
  f2_ms?: number | null
  f3_ms?: number | null
  f4_ms?: number | null
  grading_ms?: number | null
  total_ms?: number | null
}

type TaskWithDuration = BidTaskOut & {
  started_at?: string | null
  finished_at?: string | null
  duration?: TaskDurationRecord | null
}

interface StageDurationItem {
  key: string
  code: string
  label: string
  ms: number | null
  formatted: string
  pct: number
  isSlowest: boolean
  color: string
}

const router = useRouter()
const route = useRoute()
const appStore = useAppStore()
const notificationStore = useNotificationStore()

const listLoading = ref(false)
const detailLoading = ref(false)
const runningTrigger = ref(false)
const cancellingTask = ref(false)
const deletingTaskId = ref<number | null>(null)
const renamingTask = ref(false)
let selectReqSeq = 0
const exportingClean = ref(false)
const editingDetailTitle = ref(false)
const detailTitleDraft = ref('')
const tasks = ref<BidTaskOut[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(12)

const currentTask = ref<BidTaskOut | null>(null)
const findings = ref<BidFindingOut[]>([])
const priceModalVisible = ref(false)
const scoreEstimate = ref<ScoreEstimateOut | null>(null)
const triggeringScore = ref(false)
const exportingScore = ref(false)
let scorePollTimer: ReturnType<typeof setInterval> | null = null

const taskListRef = ref<InstanceType<typeof CheckTaskList> | null>(null)
const sseRef = ref<InstanceType<typeof SseProgress> | null>(null)

function formatDurationMs(ms?: number | null): string {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0) {
    return '—'
  }
  if (ms < 1000) {
    const sec = Math.max(0.1, Math.round(ms / 100) / 10)
    return `${sec.toFixed(1)}s`
  }
  const totalSec = ms / 1000
  if (totalSec < 60) {
    return `${totalSec.toFixed(1)}s`
  }
  const mins = Math.floor(totalSec / 60)
  const remSec = Math.round(totalSec % 60)
  return remSec > 0 ? `${mins}分${remSec}秒` : `${mins}分`
}

function formatLocalDateTime(isoStr?: string | null): string {
  if (!isoStr) return '—'
  const normalized = isoStr.endsWith('Z') || isoStr.includes('+') ? isoStr : `${isoStr}Z`
  const dt = new Date(normalized)
  if (Number.isNaN(dt.getTime())) return isoStr
  return dt.toLocaleString('zh-CN', { hour12: false })
}

const currentTaskExt = computed<TaskWithDuration | null>(() => {
  return (currentTask.value as TaskWithDuration | null) ?? null
})

const isTerminalTaskWithDuration = computed<boolean>(() => {
  const t = currentTaskExt.value
  if (!t || !['delivered', 'failed', 'need_manual'].includes(t.status)) {
    return false
  }
  return Boolean(t.duration && typeof t.duration.total_ms === 'number')
})

const totalDurationLabel = computed<string>(() => {
  const t = currentTaskExt.value
  if (!t?.duration) return '—'
  return formatDurationMs(t.duration.total_ms)
})

const nowTick = ref<number>(Date.now())
let detailTickTimer: ReturnType<typeof setInterval> | null = null

const isRunningTask = computed<boolean>(() => {
  const st = currentTask.value?.status
  return Boolean(st && ['extracting', 'checking', 'grading'].includes(st))
})

const runningElapsedLabel = computed<string>(() => {
  const t = currentTaskExt.value
  if (!t || !isRunningTask.value) return ''
  const rawIso = t.started_at || t.updated_at || t.created_at
  if (!rawIso) return '00:00'
  const normalized = rawIso.endsWith('Z') || rawIso.includes('+') ? rawIso : `${rawIso}Z`
  const startMs = new Date(normalized).getTime()
  if (!Number.isFinite(startMs)) return '00:00'
  const totalSec = Math.max(0, Math.floor((nowTick.value - startMs) / 1000))
  const mins = String(Math.floor(totalSec / 60)).padStart(2, '0')
  const secs = String(totalSec % 60).padStart(2, '0')
  return `${mins}:${secs}`
})

const runningIdleSec = computed<number>(() => {
  const t = currentTaskExt.value
  if (!t || !isRunningTask.value) return 0
  const rawIso = t.updated_at || t.started_at || t.created_at
  if (!rawIso) return 0
  const normalized = rawIso.endsWith('Z') || rawIso.includes('+') ? rawIso : `${rawIso}Z`
  const actMs = new Date(normalized).getTime()
  if (!Number.isFinite(actMs)) return 0
  return Math.max(0, Math.floor((nowTick.value - actMs) / 1000))
})

function handleSseProgress(frame: SSEProgressFrame): void {
  const nextUpdated = frame.updated_at || new Date().toISOString()
  const itemInList = tasks.value.find((t) => t.id === frame.task_id)
  if (itemInList) {
    itemInList.status = frame.status
    if (typeof frame.progress === 'number') {
      itemInList.progress = frame.progress
    }
    if (frame.stage_detail) {
      itemInList.stage_detail = frame.stage_detail
    }
    itemInList.updated_at = nextUpdated
    if (frame.started_at) {
      itemInList.started_at = frame.started_at
    }
  }
  if (!currentTask.value || currentTask.value.id !== frame.task_id) return
  currentTask.value.status = frame.status
  currentTask.value.updated_at = nextUpdated
  if (typeof frame.progress === 'number') {
    currentTask.value.progress = frame.progress
  }
  if (frame.stage_detail) {
    currentTask.value.stage_detail = frame.stage_detail
  }
  if (frame.started_at && currentTaskExt.value) {
    currentTaskExt.value.started_at = frame.started_at
  }
}

const OPEN_DATE_SOURCE_LABELS: Record<string, string> = {
  manual: '用户手填',
  tender_extracted: '标书正文识别',
  tender_llm: 'AI 局部窗提取',
  rule_default: '规则默认值',
  fallback_default: '默认兜底·请核对',
}

const openDateMeta = computed(() => {
  const cfg = (currentTask.value?.config_json ?? null) as Record<string, unknown> | null
  if (!cfg) return null
  const dateVal = String(cfg.open_date_effective || cfg.open_date || '').trim()
  if (!dateVal) return null
  const srcKey = String(cfg.open_date_source || '').trim()
  const srcLabel = OPEN_DATE_SOURCE_LABELS[srcKey] || '任务配置'
  const quote = String(cfg.open_date_quote || '').trim()
  const detected = String(cfg.open_date_detected || '').trim()
  let title = `开标日期来源：${srcLabel}`
  if (quote) {
    title += `\n原文摘录：${quote}`
  }
  if (detected && detected !== dateVal) {
    title += `\n⚠️ 标书原文识别为 ${detected}，与当前设定不一致`
  }
  const pillClass =
    srcKey === 'fallback_default' || (detected && detected !== dateVal)
      ? 'warning'
      : srcKey === 'manual'
        ? 'ok'
        : 'info'
  return {
    text: `开标日期：${dateVal}（${srcLabel}）`,
    pillClass,
    title,
  }
})

const railCollapsed = ref(false)
const DURATION_COLLAPSED_KEY = 'bidguard_checks_duration_collapsed'
const SCORE_COLLAPSED_KEY = 'bidguard_checks_score_collapsed'

function readStoredCollapsed(key: string): boolean {
  try {
    return localStorage.getItem(key) === '1'
  } catch {
    return false
  }
}

function writeStoredCollapsed(key: string, collapsed: boolean): void {
  try {
    localStorage.setItem(key, collapsed ? '1' : '0')
  } catch {
    // ignore storage errors
  }
}

const durationCollapsed = ref(readStoredCollapsed(DURATION_COLLAPSED_KEY))
const scoreCollapsed = ref(readStoredCollapsed(SCORE_COLLAPSED_KEY))
const forceExpandScoreOnce = ref(route.query.panel === 'score')
const scoreFocusPulse = ref(false)
const findingsFocusPulse = ref(false)
let focusPulseTimer: number | null = null
let lastHandledFocusSeq = 0

function triggerCardFocusScroll(target: 'score' | 'findings'): void {
  if (focusPulseTimer !== null) {
    window.clearTimeout(focusPulseTimer)
    focusPulseTimer = null
  }
  scoreFocusPulse.value = false
  findingsFocusPulse.value = false
  void nextTick(() => {
    const elId = target === 'score' ? 'bg-check-score-card' : 'bg-check-findings-anchor'
    const el = document.getElementById(elId)
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
    if (target === 'score') {
      scoreFocusPulse.value = true
    } else {
      findingsFocusPulse.value = true
    }
    focusPulseTimer = window.setTimeout(() => {
      scoreFocusPulse.value = false
      findingsFocusPulse.value = false
      focusPulseTimer = null
    }, 1700)
  })
}

function toggleDurationCollapsed(): void {
  durationCollapsed.value = !durationCollapsed.value
  writeStoredCollapsed(DURATION_COLLAPSED_KEY, durationCollapsed.value)
}

function toggleScoreCollapsed(): void {
  scoreCollapsed.value = !scoreCollapsed.value
  writeStoredCollapsed(SCORE_COLLAPSED_KEY, scoreCollapsed.value)
}

function syncTaskScoreSnapshot(taskId: number, snap: ScoreEstimateOut | null): void {
  if (!snap) return
  const target = tasks.value.find((t) => t.id === taskId)
  if (target) {
    target.score_estimate_json = snap
  }
  if (currentTask.value && currentTask.value.id === taskId) {
    currentTask.value.score_estimate_json = snap
  }
}

const stageDurationItems = computed<StageDurationItem[]>(() => {
  const dur = currentTaskExt.value?.duration
  if (!dur) return []

  const rawStages = [
    { key: 'f1_ms', code: '红线', label: '招标红线与★提取', ms: dur.f1_ms ?? null, color: '#2563eb' },
    ...(dur.f_parse_ms !== undefined && dur.f_parse_ms !== null
      ? [{ key: 'f_parse_ms', code: '解析', label: '投标书解析与OCR', ms: dur.f_parse_ms ?? null, color: '#7c3aed' }]
      : []),
    { key: 'f2_ms', code: '报价', label: '报价算术沙盒', ms: dur.f2_ms ?? null, color: '#0891b2' },
    { key: 'f3_ms', code: '证照', label: '证照效期与串名', ms: dur.f3_ms ?? null, color: '#059669' },
    { key: 'f4_ms', code: '勾稽', label: '★逐条勾稽与矛盾', ms: dur.f4_ms ?? null, color: '#d97706' },
    { key: 'grading_ms', code: '汇总', label: '风险分级落库', ms: dur.grading_ms ?? null, color: '#64748b' },
  ]

  const validValues = rawStages
    .map((s) => (typeof s.ms === 'number' && s.ms >= 0 ? s.ms : -1))
    .filter((v) => v >= 0)
  const maxMs = validValues.length > 0 ? Math.max(...validValues) : -1
  const sumMs = validValues.reduce((acc, v) => acc + v, 0)
  const denom = sumMs > 0 ? sumMs : 1

  let slowestMarked = false
  return rawStages.map((s) => {
    const val = typeof s.ms === 'number' && s.ms >= 0 ? s.ms : null
    const isSlowest = Boolean(val !== null && maxMs > 0 && val === maxMs && !slowestMarked)
    if (isSlowest) {
      slowestMarked = true
    }
    const pct = val !== null && sumMs > 0 ? Math.max(4, Math.round((val / denom) * 100)) : 0
    return {
      key: s.key,
      code: s.code,
      label: s.label,
      ms: val,
      formatted: formatDurationMs(val),
      pct,
      isSlowest,
      color: isSlowest ? '#dc2626' : s.color,
    }
  })
})

const slowestStageItem = computed<StageDurationItem | null>(() => {
  return stageDurationItems.value.find((s) => s.isSlowest) || null
})

async function loadTaskList(selectFirst = false): Promise<void> {
  listLoading.value = true
  let preferred: BidTaskOut | undefined
  try {
    const res = await checksApi.list({
      task_type: 'pre_check',
      page: page.value,
      page_size: pageSize.value,
    })
    tasks.value = res.items || []
    total.value = res.total || 0

    if (selectFirst && tasks.value.length > 0 && !currentTask.value) {
      const qTaskId = Number(route.query.task_id || 0)
      preferred =
        (qTaskId > 0 ? tasks.value.find((t) => t.id === qTaskId) : undefined) ||
        tasks.value.find((t) => t.id === appStore.activeTaskId) ||
        tasks.value[0]
    }
  } finally {
    listLoading.value = false
  }
  if (preferred) {
    await selectTask(preferred)
  }
}

async function selectTask(task: BidTaskOut): Promise<void> {
  const reqSeq = ++selectReqSeq
  cancelRenameDetailTask()
  stopScorePoll()
  appStore.setActiveTaskId(task.id)
  detailLoading.value = true
  try {
    const detail = await checksApi.detail(task.id)
    if (reqSeq !== selectReqSeq) return
    currentTask.value = detail.task
    const listIdx = tasks.value.findIndex((t) => t.id === detail.task.id)
    if (listIdx >= 0) {
      tasks.value[listIdx] = { ...tasks.value[listIdx], ...detail.task }
    }
    findings.value = detail.findings || []
    await loadScoreEstimate(detail.task.id, reqSeq)
    if (reqSeq !== selectReqSeq) return
    checkAutoPopupPriceManual(detail.task)
  } finally {
    if (reqSeq === selectReqSeq) {
      detailLoading.value = false
    }
  }
}

function stopScorePoll(): void {
  if (scorePollTimer) {
    clearInterval(scorePollTimer)
    scorePollTimer = null
  }
}

function scheduleScorePoll(taskId: number): void {
  stopScorePoll()
  scorePollTimer = setInterval(async () => {
    if (!currentTask.value || currentTask.value.id !== taskId) {
      stopScorePoll()
      return
    }
    try {
      const snap = await checksApi.getScoreEstimate(taskId)
      scoreEstimate.value = snap
      syncTaskScoreSnapshot(taskId, snap)
      if (!['queued', 'running'].includes(snap.status)) {
        stopScorePoll()
        const taskTitle = currentTask.value?.title || `任务 #${taskId}`
        if (snap.status === 'completed') {
          const vetoTxt = snap.has_veto_risk ? `（含 ${snap.veto_alerts.length} 项一票否决警示）` : ''
          notificationStore.notifyTaskTerminal({
            kind: 'score',
            targetId: taskId,
            title: taskTitle,
            summary: `非价格项预估 ${snap.assessed_estimated_score}/${snap.assessed_max_score} 分${vetoTxt}`,
            tone: snap.has_veto_risk ? 'warning' : 'success',
            routePath: '/checks',
            routeQuery: { task_id: String(taskId), panel: 'score' },
            dedupSuffix: `completed:${snap.updated_at || ''}`,
          })
        } else if (snap.status === 'failed' && snap.error_msg) {
          notificationStore.notifyTaskTerminal({
            kind: 'score',
            targetId: taskId,
            title: taskTitle,
            summary: snap.error_msg,
            tone: 'error',
            routePath: '/checks',
            routeQuery: { task_id: String(taskId), panel: 'score' },
            dedupSuffix: `failed:${snap.updated_at || ''}`,
          })
        }
      }
    } catch {
      stopScorePoll()
    }
  }, 2000)
}

async function loadScoreEstimate(taskId: number, reqSeq?: number): Promise<void> {
  try {
    const snap = await checksApi.getScoreEstimate(taskId)
    if (reqSeq !== undefined && reqSeq !== selectReqSeq) return
    scoreEstimate.value = snap
    syncTaskScoreSnapshot(taskId, snap)
    if (forceExpandScoreOnce.value || ['queued', 'running'].includes(snap.status)) {
      scoreCollapsed.value = false
      if (forceExpandScoreOnce.value && !['queued', 'running'].includes(snap.status)) {
        forceExpandScoreOnce.value = false
      }
    } else if (snap.status === 'not_started') {
      // 未生成评分结果时默认折叠为单行引导条，优先把首屏空间留给 F2–F4 风险清单
      scoreCollapsed.value = true
    } else {
      scoreCollapsed.value = readStoredCollapsed(SCORE_COLLAPSED_KEY)
    }
    if (['queued', 'running'].includes(snap.status)) {
      scheduleScorePoll(taskId)
    }
  } catch {
    scoreEstimate.value = null
    scoreCollapsed.value = !forceExpandScoreOnce.value
    forceExpandScoreOnce.value = false
  }
}

async function handleRunScoreEstimate(): Promise<void> {
  if (!currentTask.value || triggeringScore.value) return
  if (isRunningTask.value) {
    ElMessage.warning('请等待防废标核验流水线完成后再执行评分预估')
    return
  }
  scoreCollapsed.value = false
  writeStoredCollapsed(SCORE_COLLAPSED_KEY, false)
  triggeringScore.value = true
  try {
    const res = await checksApi.runScoreEstimate(currentTask.value.id, true)
    scoreEstimate.value = res
    syncTaskScoreSnapshot(currentTask.value.id, res)
    if (['queued', 'running'].includes(res.status)) {
      ElMessage.info('标书评分预估已在后台启动，切页不中断，完成后顶部将自动提醒')
      notificationStore.registerTrackedJob({
        kind: 'score',
        targetId: currentTask.value.id,
        title: currentTask.value.title,
        status: res.status,
        progress: res.progress,
        stageDetail: res.stage_detail || '评分预估中…',
        routePath: '/checks',
        routeQuery: { task_id: String(currentTask.value.id), panel: 'score' },
      })
      scheduleScorePoll(currentTask.value.id)
    } else if (res.status === 'completed') {
      notificationStore.notifyTaskTerminal({
        kind: 'score',
        targetId: currentTask.value.id,
        title: currentTask.value.title,
        summary: `非价格项预估 ${res.assessed_estimated_score}/${res.assessed_max_score} 分`,
        tone: res.has_veto_risk ? 'warning' : 'success',
        routePath: '/checks',
        routeQuery: { task_id: String(currentTask.value.id), panel: 'score' },
        dedupSuffix: `completed:${res.updated_at || ''}`,
      })
    }
  } finally {
    triggeringScore.value = false
  }
}

async function handleDownloadScoreReport(): Promise<void> {
  if (!currentTask.value || exportingScore.value) return
  exportingScore.value = true
  try {
    await checksApi.downloadScoreEstimateReport(
      currentTask.value.id,
      `${currentTask.value.title}_标书预估得分与提分诊断报告.docx`,
    )
    ElMessage.success('已下载《标书预估得分与提分诊断报告.docx》')
  } finally {
    exportingScore.value = false
  }
}

function scoreStatusLabel(row: ScoreCriterionItemOut): string {
  if (row.status === 'evaluated') return '已评出'
  if (row.status === 'pending_open_bid') return '待开标确定'
  if (row.status === 'veto_risk') return '否决风险'
  if (row.status_reason === 'ambiguous_deduction_rule') return '复杂规则待核'
  if (row.status_reason === 'llm_call_failed') return '模型超时待复核'
  return '待开启模型'
}

function scoreStatusTagType(row: ScoreCriterionItemOut): 'success' | 'warning' | 'danger' | 'info' {
  if (row.status === 'evaluated') return 'success'
  if (row.status === 'veto_risk') return 'danger'
  if (row.status === 'pending_open_bid') return 'info'
  return 'warning'
}

function scoreCategoryLabel(cat: string): string {
  const map: Record<string, string> = {
    price: '价格部分',
    qualification: '资质证书',
    business: '商务业绩',
    service: '售后运维',
    technical: '技术方案',
  }
  return map[cat] || cat
}

function vetoModuleLabel(mod: string): string {
  const map: Record<string, string> = {
    F2: '报价算术',
    F3: '证照与串名',
    F4: '双书勾稽',
    F6: '投前控标',
    BLIND_BID: '暗标合规',
  }
  return map[mod] || mod
}

function vetoCodeLabel(code: string): string {
  const map: Record<string, string> = {
    MISSING_RESPONSE: '★条款漏应答',
    CONTRADICTION: '前后承诺矛盾',
    NEGATIVE_DEVIATION: '实质性负偏离',
    PRICE_MISMATCH: '报价算术异常',
    PRICE_CAP_EXCEEDED: '超最高限价',
    CERT_EXPIRED: '证照已过期',
    BLIND_BID_IDENTITY_LEAK: '暗标身份泄露',
  }
  return map[code] || code
}

const scoreRangeSummary = computed(() => {
  const se = scoreEstimate.value
  if (!se) return null
  const est = Number(se.assessed_estimated_score || 0)
  const assessedMax = Number(se.assessed_max_score || 0)
  const priceMax = Number(se.price_pending_max_score || 0)
  const needManual = Number(se.need_llm_max_score || 0)
  const declaredTotal = Number(se.total_declared_max_score || 100)
  const estCents = Math.round((Number.isFinite(est) ? est : 0) * 100)
  const priceCents = Math.round((Number.isFinite(priceMax) ? priceMax : 0) * 100)
  const needCents = Math.round((Number.isFinite(needManual) ? needManual : 0) * 100)
  const highCents = estCents + priceCents
  const fmt = (cents: number) => (cents / 100).toFixed(2)
  return {
    hasPricePending: priceCents > 0,
    hasNeedManual: needCents > 0,
    minStr: fmt(estCents),
    maxStr: fmt(highCents),
    assessedMaxStr: (Number.isFinite(assessedMax) ? assessedMax : 0).toFixed(2),
    priceMaxStr: fmt(priceCents),
    needManualStr: fmt(needCents),
    declaredTotalStr: (Number.isFinite(declaredTotal) ? declaredTotal : 100).toFixed(2),
  }
})

function checkAutoPopupPriceManual(task: BidTaskOut): void {
  if (task.status !== 'delivered' || task.stats_json?.price_status !== 'NEED_MANUAL') return
  // 每个任务每个浏览器会话只自动弹一次：关闭后本会话不再打扰（页面上的人工回填入口仍在），
  // 重开浏览器后视为新会话，会再提醒一次。提交回填成功后 price_status 变 OK，永不再弹。
  const key = `price_manual_popup_dismissed_${task.id}`
  if (!sessionStorage.getItem(key)) {
    priceModalVisible.value = true
  }
}

watch(priceModalVisible, (visible) => {
  if (!visible && currentTask.value && currentTask.value.stats_json?.price_status === 'NEED_MANUAL') {
    sessionStorage.setItem(`price_manual_popup_dismissed_${currentTask.value.id}`, '1')
  }
})

async function handleTriggerRun(): Promise<void> {
  if (!currentTask.value || runningTrigger.value) return
  const taskId = currentTask.value.id
  runningTrigger.value = true
  try {
    sseRef.value?.closeActiveStream()
    stopScorePoll()
    notificationStore.untrackJob('check', taskId)
    const res = await checksApi.run(taskId, false)
    const nextStatus = res.status === 'failed' ? 'extracting' : res.status
    const nowIso = new Date().toISOString()
    currentTask.value.status = nextStatus
    currentTask.value.progress = 0
    currentTask.value.error_msg = null
    currentTask.value.stage_detail = '正在启动新一轮防废标核验流水线…'
    currentTask.value.updated_at = nowIso
    const idx = tasks.value.findIndex((t) => t.id === taskId)
    if (idx >= 0) {
      tasks.value[idx] = {
        ...tasks.value[idx],
        status: nextStatus,
        progress: 0,
        error_msg: null,
        stage_detail: '正在启动新一轮防废标核验流水线…',
        updated_at: nowIso,
      }
    }
    sseRef.value?.startStream(true)
    notificationStore.registerTrackedJob({
      kind: 'check',
      targetId: taskId,
      title: currentTask.value.title,
      status: nextStatus,
      stageDetail: '防废标核验流水线运行中…',
      routePath: '/checks',
      routeQuery: { task_id: String(taskId) },
    })
    ElMessage.info('已启动防废标核验流水线，切页不中断，完成后顶部将自动通知')
  } finally {
    runningTrigger.value = false
  }
}

async function handleTaskCreated(created: BidTaskOut, autoRun: boolean): Promise<void> {
  await loadTaskList(false)
  await selectTask(created)
  if (autoRun) {
    await handleTriggerRun()
  }
}

async function handleSseDone(_stats?: BidTaskStats): Promise<void> {
  if (!currentTask.value) return
  await selectTask(currentTask.value)
  await loadTaskList(false)
  const t = currentTask.value
  const errCnt = Number(t.stats_json?.error_count || 0)
  const warnCnt = Number(t.stats_json?.warning_count || 0)
  const needManual = t.stats_json?.price_status === 'NEED_MANUAL' || t.status === 'need_manual'
  const summaryParts = [
    errCnt > 0 ? `检出 ${errCnt} 项高危/否决风险` : '未发现高危否决风险',
    warnCnt > 0 ? `${warnCnt} 项警告` : '',
    needManual ? '报价需人工回填' : '',
  ].filter(Boolean)
  notificationStore.notifyTaskTerminal({
    kind: 'check',
    targetId: t.id,
    title: t.title,
    summary: summaryParts.join('，'),
    tone: needManual || errCnt > 0 ? 'warning' : 'success',
    routePath: '/checks',
    routeQuery: { task_id: String(t.id) },
    dedupSuffix: `done:${t.updated_at || ''}`,
  })
}

async function handleSseError(): Promise<void> {
  if (!currentTask.value) return
  await selectTask(currentTask.value)
  await loadTaskList(false)
  const t = currentTask.value
  notificationStore.notifyTaskTerminal({
    kind: 'check',
    targetId: t.id,
    title: t.title,
    summary: t.error_msg || '防废标核验流水线执行失败',
    tone: 'error',
    routePath: '/checks',
    routeQuery: { task_id: String(t.id) },
    dedupSuffix: `failed:${t.updated_at || ''}`,
  })
}

function handleFindingUpdated(updated: BidFindingOut): void {
  const idx = findings.value.findIndex((f) => f.id === updated.id)
  if (idx !== -1) {
    findings.value[idx] = updated
  }
}

async function handlePriceBackfillCompleted(_res: PriceManualBackfillOut): Promise<void> {
  if (!currentTask.value) return
  await selectTask(currentTask.value)
  await loadTaskList(false)
}

async function handleDownloadSource(url?: string | null, fallback = '标书文件.docx'): Promise<void> {
  if (!url) return
  await checksApi.downloadStoredFile(url, fallback)
}

async function handleDownloadRedline(): Promise<void> {
  if (!currentTask.value) return
  await reportsApi.downloadRedline(
    currentTask.value.id,
    `${currentTask.value.title}_原位标红批注件.docx`,
  )
  ElMessage.success('已下载投标书原位标红批注件')
}

async function handleExportCleanBid(): Promise<void> {
  if (!currentTask.value || exportingClean.value) return
  if (currentTask.value.bid_filename?.toLowerCase().endsWith('.pdf')) {
    ElMessage.warning('PDF 原件受格式保护不支持自动无损修订导出，请在 Word 原件（.docx）中修改后再核验')
    return
  }
  exportingClean.value = true
  try {
    const res = await reportsApi.exportCleanBid(currentTask.value.id, {})
    await reportsApi.downloadCleanBid(
      currentTask.value.id,
      `${currentTask.value.title}_修订定稿版投标书.docx`,
    )
    ElMessage.success(
      `Clean 定稿导出成功：原位替换 ${res.applied_count} 处，另 ${res.manual_pending_count} 项待人工补写`,
    )
  } finally {
    exportingClean.value = false
  }
}

function handlePageChange(newPage: number): void {
  page.value = newPage
  void loadTaskList(false)
}

async function handleDeleteTask(task: BidTaskOut): Promise<void> {
  if (deletingTaskId.value === task.id) return
  if (['extracting', 'checking', 'grading'].includes(task.status)) {
    ElMessage.warning('运行中任务请先中断后再删除')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确认删除体检任务「#${task.id} ${task.title}」及其全部风险发现、偏离表与上传标书原件吗？删除后不可恢复。`,
      '确认删除体检任务',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }

  deletingTaskId.value = task.id
  sseRef.value?.closeActiveStream()
  stopScorePoll()
  notificationStore.untrackJob('check', task.id)
  notificationStore.untrackJob('score', task.id)
  try {
    await checksApi.remove(task.id)
  } finally {
    deletingTaskId.value = null
  }
  ElMessage.success(`体检任务 #${task.id} 已删除`)

  tasks.value = tasks.value.filter((t) => t.id !== task.id)
  total.value = Math.max(0, total.value - 1)

  if (currentTask.value?.id === task.id) {
    selectReqSeq += 1
    currentTask.value = null
    findings.value = []
    scoreEstimate.value = null
    detailLoading.value = false
    if (appStore.activeTaskId === task.id) {
      appStore.setActiveTaskId(null)
    }
    await loadTaskList(true)
  } else {
    await loadTaskList(false)
  }
}

async function handleCancelTask(task: BidTaskOut): Promise<void> {
  if (cancellingTask.value) return
  try {
    await ElMessageBox.confirm(
      `确认中断体检任务「#${task.id} ${task.title}」吗？中断后本任务标记为失败，不保留半程报告，之后可删除或重新发起体检。`,
      '确认中断运行中任务',
      {
        type: 'warning',
        confirmButtonText: '确认中断',
        cancelButtonText: '继续运行',
      },
    )
  } catch {
    return
  }
  cancellingTask.value = true
  try {
    sseRef.value?.closeActiveStream()
    stopScorePoll()
    notificationStore.untrackJob('check', task.id)
    await checksApi.cancel(task.id)
    const idx = tasks.value.findIndex((t) => t.id === task.id)
    if (idx >= 0) {
      tasks.value[idx] = {
        ...tasks.value[idx],
        status: 'failed',
        stage_detail: '用户中断',
        error_msg: '用户中断',
      }
    }
    if (currentTask.value?.id === task.id) {
      currentTask.value = {
        ...currentTask.value,
        status: 'failed',
        stage_detail: '用户中断',
        error_msg: '用户中断',
      }
    }
    ElMessage.success(`体检任务 #${task.id} 已中断`)
    await loadTaskList(false)
    if (currentTask.value?.id === task.id) {
      await selectTask(currentTask.value)
    }
  } finally {
    cancellingTask.value = false
  }
}

async function handleRenameTask(task: BidTaskOut, nextTitle: string): Promise<void> {
  const cleanTitle = nextTitle.trim()
  if (!cleanTitle || cleanTitle === (task.title || '').trim() || renamingTask.value) {
    return
  }
  renamingTask.value = true
  try {
    const updated = await checksApi.rename(task.id, { title: cleanTitle })
    const idx = tasks.value.findIndex((t) => t.id === task.id)
    if (idx >= 0) {
      tasks.value[idx] = { ...tasks.value[idx], ...updated }
    }
    if (currentTask.value?.id === task.id) {
      currentTask.value = { ...currentTask.value, ...updated }
    }
    ElMessage.success(`体检任务 #${task.id} 已重命名为「${updated.title}」`)
  } finally {
    renamingTask.value = false
  }
}

function startRenameDetailTask(): void {
  if (!currentTask.value) return
  editingDetailTitle.value = true
  detailTitleDraft.value = currentTask.value.title || ''
  void nextTick(() => {
    const input = document.querySelector<HTMLInputElement>('.cwt-title-input')
    if (input) {
      input.focus()
      input.select()
    }
  })
}

function cancelRenameDetailTask(): void {
  editingDetailTitle.value = false
  detailTitleDraft.value = ''
}

async function commitRenameDetailTask(): Promise<void> {
  if (!editingDetailTitle.value || !currentTask.value) return
  const task = currentTask.value
  const nextTitle = detailTitleDraft.value.trim()
  editingDetailTitle.value = false
  detailTitleDraft.value = ''
  if (!nextTitle || nextTitle === (task.title || '').trim()) {
    return
  }
  await handleRenameTask(task, nextTitle)
}

async function focusCheckPanelTarget(rawId: unknown, rawPanel: unknown, withScroll = true): Promise<void> {
  if (route.path !== '/checks') return
  const panelStr = typeof rawPanel === 'string' ? rawPanel : ''
  if (panelStr === 'score') {
    forceExpandScoreOnce.value = true
    scoreCollapsed.value = false
    writeStoredCollapsed(SCORE_COLLAPSED_KEY, false)
  }
  const targetId = Number(rawId || 0)
  if (targetId > 0) {
    if (currentTask.value?.id !== targetId) {
      const hit = tasks.value.find((t) => t.id === targetId)
      if (hit) {
        await selectTask(hit)
      } else {
        try {
          const d = await checksApi.detail(targetId)
          currentTask.value = d.task
          findings.value = d.findings || []
          appStore.setActiveTaskId(d.task.id)
          await loadScoreEstimate(d.task.id)
        } catch {
          return
        }
      }
    } else if (panelStr === 'score') {
      await loadScoreEstimate(targetId)
    } else {
      await selectTask(currentTask.value)
    }
  }
  if (withScroll && (panelStr === 'score' || targetId > 0)) {
    triggerCardFocusScroll(panelStr === 'score' ? 'score' : 'findings')
  }
}

watch(
  () => [route.query.task_id, route.query.panel],
  ([rawId, rawPanel]) => {
    void focusCheckPanelTarget(rawId, rawPanel, true)
  },
)

watch(
  () => notificationStore.focusRequest.seq,
  (seq) => {
    if (!seq || seq <= lastHandledFocusSeq || notificationStore.focusRequest.path !== '/checks') return
    lastHandledFocusSeq = seq
    const q = notificationStore.focusRequest.query || {}
    void focusCheckPanelTarget(q.task_id, q.panel, true)
  },
)

onMounted(async () => {
  detailTickTimer = setInterval(() => {
    nowTick.value = Date.now()
  }, 1000)
  await loadTaskList(true)
  if (route.query.panel === 'score' || Number(route.query.task_id || 0) > 0) {
    triggerCardFocusScroll(route.query.panel === 'score' ? 'score' : 'findings')
  }
})

onActivated(() => {
  const qTaskId = Number(route.query.task_id || 0)
  const qPanel = route.query.panel
  if (qTaskId > 0 || qPanel === 'score') {
    void focusCheckPanelTarget(qTaskId, qPanel, true)
  } else if (currentTask.value) {
    void selectTask(currentTask.value)
  } else {
    void loadTaskList(true)
  }
})

onBeforeUnmount(() => {
  if (detailTickTimer) {
    clearInterval(detailTickTimer)
    detailTickTimer = null
  }
  if (focusPulseTimer !== null) {
    window.clearTimeout(focusPulseTimer)
    focusPulseTimer = null
  }
  stopScorePoll()
})

</script>

<template>
  <div class="check-workbench" :class="{ 'rail-collapsed': railCollapsed }">
    <!-- 左栏：任务台账与新建弹窗 -->
    <CheckTaskList
      v-show="!railCollapsed"
      ref="taskListRef"
      :tasks="tasks"
      :total="total"
      :page="page"
      :page-size="pageSize"
      :selected-task-id="currentTask?.id ?? null"
      :loading="listLoading"
      @select="selectTask"
      @page-change="handlePageChange"
      @created="handleTaskCreated"
      @rename="handleRenameTask"
      @cancel="handleCancelTask"
      @delete="handleDeleteTask"
      @refresh="loadTaskList(false)"
    />

    <!-- 右栏：六步进度条 + 耗时面板 + 操作舱 + 风险卡列表 -->
    <div v-loading="detailLoading" class="cw-main">
      <template v-if="currentTask">
        <!-- 顶部任务信息与操作头 -->
        <section class="bg-panel cw-top-card">
          <div class="cwt-left">
            <div class="cwt-meta">
              <button
                type="button"
                class="rail-toggle-btn"
                :title="railCollapsed ? '展开左侧封标体检任务台账' : '收起左侧任务台账以专注审阅右侧风险清单'"
                @click="railCollapsed = !railCollapsed"
              >
                {{ railCollapsed ? '» 展开任务台账' : '« 收起台账' }}
              </button>
              <span class="mono-num cwt-id">TASK #{{ currentTask.id }}</span>
              <span
                v-if="!isRunningTask && currentTask.stats_json?.llm_status"
                class="sev-pill"
                :class="currentTask.stats_json.llm_status === 'done' ? 'ok' : 'info'"
              >
                {{
                  currentTask.stats_json.llm_status === 'done'
                    ? '双轨引擎（0-Token + 大模型已启用）'
                    : '0-Token 纯确定性核验模式'
                }}
              </span>
              <span
                v-if="openDateMeta"
                class="sev-pill mono-num"
                :class="openDateMeta.pillClass"
                :title="openDateMeta.title"
              >
                {{ openDateMeta.text }}
              </span>
              <span
                v-if="currentTask.stats_json?.price_status === 'NEED_MANUAL'"
                class="sev-pill warning"
              >
                报价表待人工回填
              </span>
              <span
                v-if="isRunningTask"
                class="sev-pill warning mono-num"
              >
                ⏱ 已耗时 {{ runningElapsedLabel }} · 最近活动 {{ runningIdleSec }}s前
              </span>
              <span
                v-if="isRunningTask && runningIdleSec >= 120"
                class="sev-pill error"
              >
                疑似无响应，可点击右侧中断
              </span>
              <span
                v-else-if="isTerminalTaskWithDuration"
                class="sev-pill info mono-num"
              >
                总耗时：{{ totalDurationLabel }}
              </span>
            </div>
            <div v-if="editingDetailTitle" class="cwt-title-edit-row">
              <input
                v-model="detailTitleDraft"
                type="text"
                maxlength="256"
                class="cwt-title-input"
                @keydown.enter.prevent="commitRenameDetailTask"
                @keydown.esc.prevent="cancelRenameDetailTask"
                @blur="commitRenameDetailTask"
              />
            </div>
            <div v-else class="cwt-title-row">
              <h2
                class="cwt-title"
                title="双击或点击右侧 ✎ 重命名体检任务"
                @dblclick="startRenameDetailTask"
              >
                {{ currentTask.title }}
              </h2>
              <el-button
                link
                size="small"
                class="cwt-rename-btn"
                title="重命名体检任务"
                @click="startRenameDetailTask"
              >
                ✎
              </el-button>
            </div>
            <div class="cwt-files">
              <span>
                招标书：<b>{{ currentTask.tender_filename || '—' }}</b>
                <el-button
                  v-if="currentTask.tender_url"
                  link
                  type="primary"
                  size="small"
                  @click="handleDownloadSource(currentTask.tender_url, currentTask.tender_filename || 'tender.docx')"
                >
                  下载原件
                </el-button>
              </span>
              <span class="sep">｜</span>
              <span>
                我方投标书：<b>{{ currentTask.bid_filename || '—' }}</b>
                <el-button
                  v-if="currentTask.bid_url"
                  link
                  type="primary"
                  size="small"
                  @click="handleDownloadSource(currentTask.bid_url, currentTask.bid_filename || 'bid.docx')"
                >
                  下载原件
                </el-button>
              </span>
            </div>
          </div>

          <div class="cwt-actions">
            <el-button
              v-if="isRunningTask"
              type="danger"
              :loading="cancellingTask"
              @click="handleCancelTask(currentTask)"
            >
              ⏹ 中断核验
            </el-button>
            <el-button
              v-else
              type="primary"
              :loading="runningTrigger"
              @click="handleTriggerRun"
            >
              {{ currentTask.status === 'delivered' ? '重新执行体检' : '启动防废标核验' }}
            </el-button>
            <el-button
              type="warning"
              plain
              @click="priceModalVisible = true"
            >
              报价算术人工回填
            </el-button>
            <el-button
              type="success"
              plain
              @click="router.push({ path: '/deviation', query: { task_id: String(currentTask.id) } })"
            >
              生成 Word 偏离表
            </el-button>
            <el-button @click="handleDownloadRedline">
              下载标红批注件
            </el-button>
            <el-button
              type="primary"
              plain
              :loading="exportingClean"
              @click="handleExportCleanBid"
            >
              导出 Clean 修订正本
            </el-button>
            <el-button
              plain
              @click="router.push({ path: '/composer', query: { tab: 'clean', taskId: String(currentTask.id) } })"
            >
              精修定稿中心
            </el-button>
              <el-button
                type="danger"
                plain
                :loading="deletingTaskId === currentTask.id"
                :disabled="['extracting', 'checking', 'grading'].includes(currentTask.status)"
                @click="handleDeleteTask(currentTask)"
              >
                删除任务
              </el-button>
          </div>
        </section>

        <!-- 六步流水线 SSE 实时进度条 -->
        <SseProgress
          ref="sseRef"
          :task-id="currentTask.id"
          :status="currentTask.status"
          :progress="currentTask.progress"
          :stage-detail="currentTask.stage_detail"
          :error-msg="currentTask.error_msg"
          @progress="handleSseProgress"
          @done="handleSseDone"
          @error="handleSseError"
        />

        <!-- 五阶段耗时可观测性面板（支持一键收起，终态且有 duration 记录时展示） -->
        <section v-if="isTerminalTaskWithDuration" class="bg-panel dur-breakdown-card">
          <div class="dbc-head">
            <div class="dbc-title-row">
              <span class="dbc-title">核验流水线分阶段耗时透视</span>
              <span class="dbc-total mono-num">总计 {{ totalDurationLabel }}</span>
              <span v-if="durationCollapsed && slowestStageItem" class="slowest-badge mono-num">
                最慢 {{ slowestStageItem.code }} · {{ slowestStageItem.formatted }}
              </span>
            </div>
            <div class="dbc-head-right">
              <div class="dbc-timestamps mono-num">
                <span>启动：{{ formatLocalDateTime(currentTaskExt?.started_at) }}</span>
                <span class="sep">→</span>
                <span>完成：{{ formatLocalDateTime(currentTaskExt?.finished_at) }}</span>
              </div>
              <button
                type="button"
                class="collapse-pill-btn"
                @click="toggleDurationCollapsed"
              >
                {{ durationCollapsed ? '展开阶段 ▼' : '收起 ▲' }}
              </button>
            </div>
          </div>

          <template v-if="!durationCollapsed">
            <!-- 横向比例堆叠条 -->
            <div class="dbc-stack-bar">
              <div
                v-for="st in stageDurationItems"
                :key="`bar_${st.key}`"
                v-show="st.pct > 0"
                class="dbc-stack-seg"
                :style="{ width: `${st.pct}%`, backgroundColor: st.color }"
                :title="`${st.code} ${st.label}：${st.formatted}`"
              ></div>
            </div>

            <!-- 五阶段明细卡片（最慢阶段红色高亮） -->
            <div class="dbc-stages-grid">
              <div
                v-for="st in stageDurationItems"
                :key="st.key"
                class="dbc-stage-item"
                :class="{ 'is-slowest': st.isSlowest }"
              >
                <div class="dsi-top">
                  <span class="dsi-code mono-num" :style="{ backgroundColor: st.color }">
                    {{ st.code }}
                  </span>
                  <span class="dsi-label">{{ st.label }}</span>
                  <span v-if="st.isSlowest" class="slowest-badge">最慢阶段</span>
                </div>
                <div class="dsi-val mono-num">
                  {{ st.formatted }}
                  <span v-if="st.ms !== null" class="dsi-ms">({{ st.ms }}ms)</span>
                </div>
              </div>
            </div>
          </template>
        </section>

        <!-- 标书评分预估与提分诊断面板 -->
        <el-card
          id="bg-check-score-card"
          shadow="never"
          class="bg-panel score-est-card"
          :class="{ 'bg-focus-flash': scoreFocusPulse }"
        >
          <template #header>
            <div class="sec-head-row">
              <div class="sec-title-grp">
                <span class="sec-main-title">标书评分预估与提分诊断</span>
                <el-tag
                  v-if="scoreEstimate && scoreEstimate.status === 'completed'"
                  size="small"
                  :type="scoreEstimate.llm_status === 'done' ? 'success' : 'info'"
                >
                  {{
                    scoreEstimate.llm_status === 'done'
                      ? '大模型 + 确定性引擎已评估'
                      : '确定性规则预筛模式'
                  }}
                </el-tag>
              </div>
              <div class="sec-act-grp">
                <el-button
                  type="primary"
                  size="small"
                  :loading="triggeringScore || (scoreEstimate?.status === 'running' || scoreEstimate?.status === 'queued')"
                  :disabled="isRunningTask"
                  @click="handleRunScoreEstimate"
                >
                  {{
                    scoreEstimate?.status === 'completed'
                      ? '重新评估标书得分'
                      : scoreEstimate?.status === 'running' || scoreEstimate?.status === 'queued'
                        ? '正在评估中…'
                        : '一键发起评分预估'
                  }}
                </el-button>
                <el-button
                  v-if="scoreEstimate?.status === 'completed'"
                  type="success"
                  size="small"
                  plain
                  :loading="exportingScore"
                  @click="handleDownloadScoreReport"
                >
                  下载诊断报告 (.docx)
                </el-button>
                <button
                  type="button"
                  class="collapse-pill-btn"
                  @click="toggleScoreCollapsed"
                >
                  {{ scoreCollapsed ? '展开分析 ▼' : '收起分析 ▲' }}
                </button>
              </div>
            </div>
          </template>

          <!-- 收起态：保留常驻简明免责标识 + 核心分数摘要条，优先让出首屏空间给 F2–F4 风险清单 -->
          <div v-if="scoreCollapsed" class="score-collapsed-bar">
            <span class="scb-disclaimer" title="本预估仅供参考，价格分依赖开标现场基准，不代表评委最终打分">
              ⚠ AI 质量自评预估（非最终评委实际打分）
            </span>
            <template v-if="scoreEstimate?.status === 'completed'">
              <span class="scb-pill primary mono-num">
                非价格项预估 {{ scoreEstimate.assessed_estimated_score }} / {{ scoreEstimate.assessed_max_score }} 分
                <template v-if="scoreEstimate.assessed_ratio_pct != null">
                  ({{ scoreEstimate.assessed_ratio_pct }}%)
                </template>
              </span>
              <span v-if="scoreRangeSummary?.hasPricePending" class="scb-pill mono-num">
                综合预估总分参考 {{ scoreRangeSummary.minStr }} ~ {{ scoreRangeSummary.maxStr }} / {{ scoreRangeSummary.declaredTotalStr }} 分
              </span>
              <span
                v-if="scoreEstimate.veto_alerts.length > 0"
                class="scb-pill danger mono-num"
              >
                一票否决预警 {{ scoreEstimate.veto_alerts.length }} 项
              </span>
              <span v-if="scoreEstimate.score_scale_warning" class="scb-pill warn">
                需核对总分口径
              </span>
            </template>
            <span
              v-else-if="scoreEstimate?.status === 'running' || scoreEstimate?.status === 'queued'"
              class="scb-muted"
            >
              {{ scoreEstimate.stage_detail || '后台评分预估计算中…' }} ({{ scoreEstimate.progress || 15 }}%)
            </span>
            <span v-else-if="scoreEstimate?.status === 'failed'" class="scb-pill danger">
              上次评分中断：{{ scoreEstimate.error_msg || '请点击右上角重新评估' }}
            </span>
            <span v-else class="scb-muted">
              尚未发起评分预估；点击右侧「一键发起评分预估」或「展开分析」查看。
            </span>
          </div>

          <template v-else>
            <!-- 三条防幻觉红线之三：常驻显眼免责声明（严禁省略或弱化） -->
            <el-alert
              type="warning"
              :closable="false"
              show-icon
              class="score-disclaimer-banner"
              title="AI 质量自评预估（非最终评委实际打分）"
              description="本预估仅基于招标评分细则与我方投标书局部证据窗口进行技术/商务质量诊断与查漏补缺，价格分取决于开标现场各投标人报价基准，不代表评标委员会最终打分承诺。"
            />

          <div
            v-if="scoreEstimate && (scoreEstimate.status === 'running' || scoreEstimate.status === 'queued')"
            class="score-running-box"
          >
            <div class="srb-txt">
              <b>{{ scoreEstimate.stage_detail || '正在按评分细则逐项检索投标正文并计算预估得分…' }}</b>
            </div>
            <el-progress :percentage="scoreEstimate.progress || 15" :stroke-width="8" striped striped-flow />
          </div>

          <el-alert
            v-else-if="scoreEstimate && scoreEstimate.status === 'failed'"
            type="error"
            :closable="false"
            show-icon
            class="mt-10"
            :title="`评分预估未完成：${scoreEstimate.error_msg || '执行中断，请点击右上角重新评估'}`"
          />

          <template v-else-if="scoreEstimate && scoreEstimate.status === 'completed'">
            <!-- 分值合计≠100 诚实告警（HANDOVER §5.1 已知段落型评分细则粒度制约） -->
            <el-alert
              v-if="scoreEstimate.score_scale_warning"
              type="info"
              :closable="false"
              show-icon
              class="mt-10"
              :title="scoreEstimate.score_scale_warning"
            />

            <!-- 一票否决风险先导警示 -->
            <el-alert
              v-if="scoreEstimate.has_veto_risk || scoreEstimate.veto_alerts.length > 0"
              type="error"
              :closable="false"
              show-icon
              class="mt-10"
              :title="`存在疑似一票否决项（共 ${scoreEstimate.veto_alerts.length} 项 —— 请优先清零废标风险，切勿仅凭正文得分盲目封标）`"
            >
              <ul class="score-veto-list">
                <li
                  v-for="(va, vIdx) in scoreEstimate.veto_alerts.slice(0, 6)"
                  :key="`va_${vIdx}`"
                >
                  <b>[{{ vetoModuleLabel(va.source_module) }} · {{ vetoCodeLabel(va.code) }}]</b>
                  {{ va.message }}（建议：{{ va.suggestion }}）
                </li>
              </ul>
            </el-alert>

            <!-- 顶部四格核心指标总览 -->
            <div class="score-summary-grid">
              <div class="ssg-item primary">
                <div class="ssg-label">非价格项预估总分</div>
                <div class="ssg-val mono-num">
                  {{ scoreEstimate.assessed_estimated_score }}
                  <span class="ssg-sub">/ {{ scoreEstimate.assessed_max_score }} 分</span>
                </div>
                <div class="ssg-foot">
                  得分率：{{ scoreEstimate.assessed_ratio_pct != null ? `${scoreEstimate.assessed_ratio_pct}%` : '—' }}（{{ scoreRangeSummary?.hasNeedManual ? `另有 ${scoreEstimate.need_llm_max_score} 分待人工复核` : '全部非价格项已出分' }}）
                </div>
              </div>
              <div class="ssg-item primary">
                <div class="ssg-label">综合预估总分参考（{{ scoreRangeSummary?.declaredTotalStr || '100.00' }} 分制）</div>
                <div class="ssg-val mono-num">
                  <template v-if="scoreRangeSummary?.hasPricePending">
                    {{ scoreRangeSummary.minStr }} ~ {{ scoreRangeSummary.maxStr }}
                    <span class="ssg-sub">分</span>
                  </template>
                  <template v-else>
                    {{ scoreEstimate.assessed_estimated_score }}
                    <span class="ssg-sub">/ {{ scoreEstimate.assessed_max_score }} 分</span>
                  </template>
                </div>
                <div class="ssg-foot">
                  <template v-if="scoreRangeSummary?.hasPricePending">
                    已评非价格 {{ scoreRangeSummary.minStr }} 分 + 价格区间 0~{{ scoreEstimate.price_pending_max_score }} 分
                  </template>
                  <template v-else>
                    提取满分合计 {{ scoreEstimate.items_sum_max_score }} 分
                  </template>
                </div>
              </div>
              <div class="ssg-item">
                <div class="ssg-label">
                  {{ scoreRangeSummary?.hasNeedManual ? '未自动出分待核对分值' : '价格分满分（待开标确定）' }}
                </div>
                <div class="ssg-val mono-num">
                  {{ scoreRangeSummary?.hasNeedManual ? scoreEstimate.need_llm_max_score : scoreEstimate.price_pending_max_score }}
                  <span class="ssg-sub">分</span>
                </div>
                <div class="ssg-foot">
                  {{
                    scoreRangeSummary?.hasNeedManual
                      ? `提取满分合计 ${scoreEstimate.items_sum_max_score} 分`
                      : '价格分依赖开标现场各家报价基准价（严禁虚构）'
                  }}
                </div>
              </div>
              <div class="ssg-item" :class="{ danger: scoreEstimate.veto_alerts.length > 0 }">
                <div class="ssg-label">一票否决红线预警</div>
                <div class="ssg-val mono-num">
                  {{ scoreEstimate.veto_alerts.length }}
                  <span class="ssg-sub">项</span>
                </div>
                <div class="ssg-foot">含报价 / 证照 / 勾稽 / 暗标合规</div>
              </div>
            </div>

            <!-- 重点提分抓手 Top-5 -->
            <div v-if="scoreEstimate.top_priorities.length > 0" class="score-priority-box">
              <div class="spb-head">
                <b>重点提分行动抓手（按可挽回分值空间从高到低排序）</b>
                <el-button
                  size="small"
                  type="primary"
                  link
                  @click="router.push({ path: '/composer', query: { taskId: String(currentTask.id) } })"
                >
                  前往标书编制中心整书装配 →
                </el-button>
              </div>
              <div class="spb-list">
                <div
                  v-for="(pr, pIdx) in scoreEstimate.top_priorities"
                  :key="pr.criterion_id"
                  class="spb-row"
                >
                  <span class="spb-rank mono-num">TOP {{ pIdx + 1 }}</span>
                  <span class="spb-factor">{{ pr.factor }}</span>
                  <el-tag size="small" type="danger" effect="plain">
                    可挽回 +{{ pr.gap_score }} 分（满分 {{ pr.max_score }}）
                  </el-tag>
                  <span class="spb-action">{{ pr.action_summary }}</span>
                  <el-button
                    size="small"
                    type="primary"
                    plain
                    style="margin-left: auto"
                    @click="
                      router.push({
                        path: '/composer',
                        query: {
                          taskId: String(currentTask.id),
                          compose_scope: 'section',
                          topic: pr.factor,
                          guidance: pr.action_summary,
                        },
                      })
                    "
                  >
                    定向补写本章 →
                  </el-button>
                </div>
              </div>
            </div>

            <!-- 分项评分细则明细表 -->
            <el-table
              :data="scoreEstimate.items"
              border
              size="small"
              style="width: 100%"
              class="score-items-table"
            >
              <el-table-column label="维度" width="84">
                <template #default="{ row }">
                  <el-tag size="small" effect="plain">
                    {{ scoreCategoryLabel((row as ScoreCriterionItemOut).category) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="评分因素与标准" min-width="150">
                <template #default="{ row }">
                  <div class="sit-factor">{{ (row as ScoreCriterionItemOut).factor }}</div>
                  <div class="sit-sec">{{ (row as ScoreCriterionItemOut).section }}</div>
                </template>
              </el-table-column>
              <el-table-column label="满分" width="66" align="right">
                <template #default="{ row }">
                  <span class="mono-num">{{ (row as ScoreCriterionItemOut).max_score }}</span>
                </template>
              </el-table-column>
              <el-table-column label="预估得分" width="80" align="right">
                <template #default="{ row }">
                  <b v-if="(row as ScoreCriterionItemOut).estimated_score != null" class="mono-num">
                    {{ (row as ScoreCriterionItemOut).estimated_score }}
                  </b>
                  <span v-else class="muted-txt">—</span>
                </template>
              </el-table-column>
              <el-table-column label="评估状态" width="98">
                <template #default="{ row }">
                  <el-tag size="small" :type="scoreStatusTagType(row as ScoreCriterionItemOut)">
                    {{ scoreStatusLabel(row as ScoreCriterionItemOut) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="失分原因与提分建议" min-width="280">
                <template #default="{ row }">
                  <div class="sit-reason">
                    <b>原因：</b>{{ ((row as ScoreCriterionItemOut).deduction_reasons || []).join('；') || '—' }}
                  </div>
                  <div class="sit-sug">
                    <b>建议：</b>{{ ((row as ScoreCriterionItemOut).improvement_suggestions || []).join('；') || '—' }}
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="投标书正文依据" min-width="190">
                <template #default="{ row }">
                  <el-collapse
                    v-if="((row as ScoreCriterionItemOut).evidence_quotes || []).some((q) => q.quote_verified && q.quote)"
                  >
                    <el-collapse-item title="展开核验摘录与页码" :name="(row as ScoreCriterionItemOut).criterion_id">
                      <div
                        v-for="(eq, eqIdx) in ((row as ScoreCriterionItemOut).evidence_quotes || []).filter((q) => q.quote_verified && q.quote)"
                        :key="`eq_${eqIdx}`"
                        class="sit-quote-item"
                      >
                        <el-tag size="small" type="info" class="mr-4">
                          {{ eq.page ? `P${eq.page}` : (eq.offset != null ? `@char${eq.offset}` : '正文') }}
                        </el-tag>
                        <span>{{ eq.quote }}</span>
                      </div>
                    </el-collapse-item>
                  </el-collapse>
                  <span v-else class="muted-txt">
                    {{ (row as ScoreCriterionItemOut).status === 'pending_open_bid' ? '待开标后由报价基准确定' : '未匹配到直接引用句' }}
                  </span>
                </template>
              </el-table-column>
            </el-table>
          </template>

          <div v-else class="score-idle-hint">
            尚未生成评分预估结果。点击右上角「一键发起评分预估」，系统将按招标《评分细则》逐条评估我方标书正文质量并给出提分抓手。
          </div>
          </template>
        </el-card>

        <!-- 风险卡列表 -->
        <div id="bg-check-findings-anchor" :class="{ 'bg-focus-flash': findingsFocusPulse }">
          <RiskCard
            :task-id="currentTask.id"
            :findings="findings"
            @updated="handleFindingUpdated"
          />
        </div>

        <!-- 报价回填弹窗（price_status == 'NEED_MANUAL' 时自动弹出） -->
        <PriceManualDialog
          v-model:visible="priceModalVisible"
          :task-id="currentTask.id"
          :initial-price-cap="currentTask.stats_json?.price_cap"
          :initial-bid-total="currentTask.stats_json?.bid_total"
          :initial-reference-bid-total="currentTask.stats_json?.reference_bid_total"
          :initial-table-total="currentTask.stats_json?.table_total"
          :initial-bid-total-upper="currentTask.stats_json?.bid_total_upper"
          :initial-items="currentTask.stats_json?.price_items"
          @completed="handlePriceBackfillCompleted"
        />
      </template>

      <div v-else class="bg-panel cw-empty">
        <el-empty description="请在左侧选择一条体检任务，或点击「+ 新建体检」上传招投标双书">
          <el-button type="primary" @click="taskListRef?.openCreateModal()">
            + 立即新建防废标体检任务
          </el-button>
        </el-empty>
      </div>
    </div>
  </div>
</template>

<style scoped>
.check-workbench {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 18px;
  align-items: start;
}

.check-workbench.rail-collapsed {
  grid-template-columns: 1fr;
}

.rail-toggle-btn {
  padding: 2px 9px;
  border-radius: 5px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
  color: var(--txt-sec);
  font-size: 11.5px;
  font-weight: 800;
  cursor: pointer;
  transition: all 0.15s;
}

.rail-toggle-btn:hover {
  border-color: var(--brand);
  color: var(--brand);
  background: #ffffff;
}

.cw-main {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.cw-top-card {
  padding: 18px 22px;
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  flex-wrap: wrap;
}

.cwt-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

.cwt-id {
  font-size: 12px;
  font-weight: 900;
  color: var(--brand);
}

.cwt-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.cwt-title-edit-row {
  margin-bottom: 6px;
}

.cwt-title-input {
  width: min(520px, 100%);
  padding: 4px 10px;
  font-size: 16px;
  font-weight: 800;
  color: var(--txt-strong);
  background: #ffffff;
  border: 1px solid var(--brand);
  border-radius: 6px;
  outline: none;
}

.cwt-title {
  font-size: 18px;
  font-weight: 900;
  color: var(--txt-strong);
  margin: 0;
  cursor: text;
}

.cwt-rename-btn {
  font-size: 14px !important;
  color: var(--txt-muted) !important;
}

.cwt-rename-btn:hover {
  color: var(--brand) !important;
}

.cwt-files {
  font-size: 12.5px;
  color: var(--txt-sec);
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.sep {
  color: var(--txt-muted);
}

.cwt-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.cwt-actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

.cwt-actions :deep(.el-button) {
  padding: 7px 11px;
  font-size: 12.5px;
}

.dur-breakdown-card {
  padding: 14px 20px;
}

.dbc-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}

.dbc-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.dbc-title {
  font-size: 13.5px;
  font-weight: 800;
  color: var(--txt-strong);
}

.dbc-total {
  font-size: 12.5px;
  font-weight: 900;
  color: var(--brand);
  background: var(--brand-soft);
  padding: 2px 8px;
  border-radius: 4px;
}

.dbc-timestamps {
  font-size: 11.5px;
  color: var(--txt-muted);
  display: flex;
  align-items: center;
  gap: 6px;
}

.dbc-head-right {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.collapse-pill-btn {
  padding: 3px 10px;
  border-radius: 5px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
  color: var(--txt-sec);
  font-size: 11.5px;
  font-weight: 700;
  cursor: pointer;
  transition: all 0.15s;
}

.collapse-pill-btn:hover {
  border-color: var(--brand);
  color: var(--brand);
  background: #ffffff;
}

.score-collapsed-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12px;
}

.scb-disclaimer {
  padding: 2px 8px;
  border-radius: 4px;
  background: #fef3c7;
  color: #92400e;
  font-weight: 700;
}

.scb-pill {
  padding: 2px 9px;
  border-radius: 4px;
  background: #f1f5f9;
  color: var(--txt-strong);
  font-weight: 700;
}

.scb-pill.primary {
  background: #eff6ff;
  color: #1d4ed8;
}

.scb-pill.danger {
  background: #fef2f2;
  color: #dc2626;
}

.scb-pill.warn {
  background: #fff7ed;
  color: #c2410c;
}

.scb-muted {
  color: var(--txt-muted);
}

.dbc-stack-bar {
  height: 7px;
  background: #e2e8f0;
  border-radius: 99px;
  overflow: hidden;
  display: flex;
  gap: 2px;
  margin-bottom: 12px;
}

.dbc-stack-seg {
  height: 100%;
  transition: width 0.3s ease;
}

.dbc-stages-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(145px, 1fr));
  gap: 10px;
}

.dbc-stage-item {
  padding: 9px 11px;
  border-radius: 7px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
}

.dbc-stage-item.is-slowest {
  border-color: #fca5a5;
  background: #fef2f2;
}

.dsi-top {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}

.dsi-code {
  font-size: 10.5px;
  font-weight: 900;
  color: #ffffff;
  padding: 1px 5px;
  border-radius: 4px;
}

.dsi-label {
  font-size: 12px;
  font-weight: 700;
  color: var(--txt-strong);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.slowest-badge {
  margin-left: auto;
  font-size: 10px;
  font-weight: 800;
  color: #dc2626;
  background: #fee2e2;
  padding: 1px 5px;
  border-radius: 4px;
  flex-shrink: 0;
}

.dsi-val {
  font-size: 13.5px;
  font-weight: 900;
  color: var(--txt-strong);
}

.dbc-stage-item.is-slowest .dsi-val {
  color: #dc2626;
}

.dsi-ms {
  font-size: 11px;
  font-weight: 600;
  color: var(--txt-muted);
  margin-left: 4px;
}

.cw-empty {
  padding: 80px 24px;
}

.score-est-card {
  margin-bottom: 14px;
  overflow: hidden;
  max-width: 100%;
}

.score-est-card :deep(.el-card__body) {
  min-width: 0;
  overflow-x: auto;
}

.sec-head-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.sec-title-grp {
  display: flex;
  align-items: center;
  gap: 8px;
}

.sec-main-title {
  font-size: 14px;
  font-weight: 800;
  color: var(--txt-strong);
}

.sec-act-grp {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.score-disclaimer-banner {
  border-left: 4px solid #f59e0b;
}

.mt-10 {
  margin-top: 10px;
}

.score-running-box {
  margin-top: 12px;
  padding: 12px;
  background: #f8fafc;
  border-radius: 6px;
}

.srb-txt {
  margin-bottom: 6px;
  font-size: 12.5px;
}

.score-veto-list {
  margin: 4px 0 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.5;
}

.score-summary-grid {
  margin-top: 12px;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.ssg-item {
  padding: 10px 12px;
  border: 1px solid var(--border-line);
  border-radius: 8px;
  background: #f8fafc;
}

.ssg-item.primary {
  background: #eff6ff;
  border-color: #bfdbfe;
}

.ssg-item.danger {
  background: #fef2f2;
  border-color: #fecaca;
}

.ssg-label {
  font-size: 12px;
  color: var(--txt-muted);
}

.ssg-val {
  margin: 4px 0 2px;
  font-size: 20px;
  font-weight: 900;
  color: var(--txt-strong);
}

.ssg-sub {
  font-size: 12px;
  font-weight: 600;
  color: var(--txt-muted);
}

.ssg-foot {
  font-size: 11px;
  color: var(--txt-muted);
}

.score-priority-box {
  margin-top: 12px;
  padding: 10px 12px;
  border: 1px solid #fed7aa;
  border-radius: 8px;
  background: #fffbeb;
}

.spb-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
  font-size: 12.5px;
}

.spb-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.spb-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12.5px;
}

.spb-rank {
  font-size: 11px;
  font-weight: 800;
  color: #b45309;
  background: #fde68a;
  padding: 1px 6px;
  border-radius: 4px;
}

.spb-factor {
  font-weight: 700;
  color: var(--txt-strong);
}

.spb-action {
  color: #475569;
}

.score-items-table {
  margin-top: 12px;
}

.sit-factor {
  font-weight: 700;
  color: var(--txt-strong);
}

.sit-sec {
  font-size: 11px;
  color: var(--txt-muted);
}

.sit-reason {
  font-size: 12px;
  color: #334155;
  margin-bottom: 3px;
}

.sit-sug {
  font-size: 12px;
  color: #0f766e;
}

.sit-quote-item {
  font-size: 11.5px;
  line-height: 1.45;
  margin-bottom: 4px;
}

.muted-txt {
  color: var(--txt-muted);
  font-size: 12px;
}

.score-idle-hint {
  margin-top: 10px;
  font-size: 12.5px;
  color: var(--txt-muted);
}

@media (max-width: 1280px) {
  .dbc-stages-grid {
    grid-template-columns: repeat(3, 1fr);
  }
}

@media (max-width: 1100px) {
  .check-workbench {
    grid-template-columns: 1fr;
  }
}
</style>