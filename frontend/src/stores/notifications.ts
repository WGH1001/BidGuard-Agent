import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { ElNotification } from 'element-plus/es/components/notification/index'
import router from '@/router'
import { checksApi, composerApi, radarApi, similarityApi } from '@/api'

export type TrackedTaskKind = 'check' | 'score' | 'composer' | 'similarity' | 'radar'
export type NotificationTone = 'success' | 'warning' | 'error'

export interface TrackedBackgroundJob {
  key: string
  kind: TrackedTaskKind
  kindLabel: string
  targetId: number
  title: string
  status: string
  progress?: number
  stageDetail?: string
  startedAt: string
  routePath: string
  routeQuery?: Record<string, string>
}

export interface TaskNotificationItem {
  id: string
  dedupKey: string
  kind: TrackedTaskKind
  kindLabel: string
  targetId: number
  title: string
  summary: string
  tone: NotificationTone
  routePath: string
  routeQuery?: Record<string, string>
  createdAt: string
  read: boolean
}

const MAX_HISTORY_ITEMS = 20
const POLL_INTERVAL_MS = 2500

const KIND_LABELS: Record<TrackedTaskKind, string> = {
  check: '防废标体检',
  score: '评分预估',
  composer: '标书编制',
  similarity: '交叉比对',
  radar: '控标雷达',
}

const DEFAULT_ROUTES: Record<TrackedTaskKind, string> = {
  check: '/checks',
  score: '/checks',
  composer: '/composer',
  similarity: '/similarity',
  radar: '/radar',
}

function makeJobKey(kind: TrackedTaskKind, targetId: number): string {
  return `${kind}:${targetId}`
}

function buildStorageKey(scopeKey: string): string {
  return `bidguard_task_notifications_v1_${scopeKey}`
}

function loadHistoryFromStorage(scopeKey: string): TaskNotificationItem[] {
  if (!scopeKey) return []
  try {
    const raw = localStorage.getItem(buildStorageKey(scopeKey))
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter((x) => x && typeof x === 'object' && typeof x.id === 'string' && typeof x.title === 'string')
      .slice(0, MAX_HISTORY_ITEMS)
  } catch {
    return []
  }
}

function saveHistoryToStorage(scopeKey: string, items: TaskNotificationItem[]): void {
  if (!scopeKey) return
  try {
    localStorage.setItem(buildStorageKey(scopeKey), JSON.stringify(items.slice(0, MAX_HISTORY_ITEMS)))
  } catch {
    // ignore storage quota failures
  }
}

export const useNotificationStore = defineStore('notifications', () => {
  const activeScopeKey = ref<string>('')
  const trackedJobs = ref<TrackedBackgroundJob[]>([])
  const notifications = ref<TaskNotificationItem[]>([])

  const seenDedupKeys = new Set<string>()
  let pollTimer: number | null = null
  let pollingBusy = false

  const runningCount = computed(() => trackedJobs.value.length)
  const unreadCount = computed(() => notifications.value.filter((n) => !n.read).length)

  function syncUserScope(username?: string | null, tenantId?: number | null): void {
    const cleanUser = (username || '').trim()
    if (!cleanUser) {
      stopAndReset()
      return
    }
    const nextScope = `${tenantId ?? 0}_${cleanUser}`
    if (nextScope === activeScopeKey.value) return
    stopPoller()
    trackedJobs.value = []
    seenDedupKeys.clear()
    activeScopeKey.value = nextScope
    notifications.value = loadHistoryFromStorage(nextScope)
    for (const item of notifications.value) {
      if (item.dedupKey) seenDedupKeys.add(item.dedupKey)
    }
  }

  function persistNotifications(): void {
    if (activeScopeKey.value) {
      saveHistoryToStorage(activeScopeKey.value, notifications.value)
    }
  }

  function stopPoller(): void {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer)
      pollTimer = null
    }
  }

  function ensurePoller(): void {
    if (pollTimer !== null || trackedJobs.value.length === 0) return
    pollTimer = window.setInterval(() => {
      void pollTrackedJobs()
    }, POLL_INTERVAL_MS)
  }

  function registerTrackedJob(payload: {
    kind: TrackedTaskKind
    targetId: number
    title: string
    status?: string
    progress?: number
    stageDetail?: string
    routePath?: string
    routeQuery?: Record<string, string>
  }): void {
    if (!payload.targetId || payload.targetId <= 0) return
    const key = makeJobKey(payload.kind, payload.targetId)
    // 允许用户针对同一任务重新发起运行，清除该任务历史去重键以便新一次完成能再次提示
    for (const dk of Array.from(seenDedupKeys)) {
      if (dk.startsWith(`${key}:`)) {
        seenDedupKeys.delete(dk)
      }
    }

    const nextItem: TrackedBackgroundJob = {
      key,
      kind: payload.kind,
      kindLabel: KIND_LABELS[payload.kind],
      targetId: payload.targetId,
      title: (payload.title || `${KIND_LABELS[payload.kind]} #${payload.targetId}`).trim(),
      status: payload.status || 'running',
      progress: payload.progress,
      stageDetail: payload.stageDetail || '后台处理中…',
      startedAt: new Date().toISOString(),
      routePath: payload.routePath || DEFAULT_ROUTES[payload.kind],
      routeQuery: payload.routeQuery,
    }
    const idx = trackedJobs.value.findIndex((j) => j.key === key)
    if (idx >= 0) {
      trackedJobs.value[idx] = nextItem
    } else {
      trackedJobs.value = [nextItem, ...trackedJobs.value]
    }
    ensurePoller()
  }

  function untrackJob(kind: TrackedTaskKind, targetId: number): void {
    const key = makeJobKey(kind, targetId)
    trackedJobs.value = trackedJobs.value.filter((j) => j.key !== key)
    if (trackedJobs.value.length === 0) {
      stopPoller()
    }
  }

  function navigateToTarget(routePath: string, routeQuery?: Record<string, string>): void {
    void router.push({
      path: routePath,
      query: routeQuery || undefined,
    })
  }

  function openNotification(item: TaskNotificationItem): void {
    markRead(item.id)
    navigateToTarget(item.routePath, item.routeQuery)
  }

  function notifyTaskTerminal(payload: {
    kind: TrackedTaskKind
    targetId: number
    title: string
    summary: string
    tone: NotificationTone
    routePath?: string
    routeQuery?: Record<string, string>
    dedupSuffix?: string
  }): void {
    const jobKey = makeJobKey(payload.kind, payload.targetId)
    untrackJob(payload.kind, payload.targetId)

    const dedupKey = `${jobKey}:${payload.dedupSuffix || payload.tone}`
    if (seenDedupKeys.has(dedupKey)) {
      return
    }
    seenDedupKeys.add(dedupKey)

    const kindLabel = KIND_LABELS[payload.kind]
    const routePath = payload.routePath || DEFAULT_ROUTES[payload.kind]
    const cleanTitle = (payload.title || `${kindLabel} #${payload.targetId}`).trim()
    const item: TaskNotificationItem = {
      id: `${dedupKey}_${Date.now()}`,
      dedupKey,
      kind: payload.kind,
      kindLabel,
      targetId: payload.targetId,
      title: cleanTitle,
      summary: payload.summary,
      tone: payload.tone,
      routePath,
      routeQuery: payload.routeQuery,
      createdAt: new Date().toISOString(),
      read: false,
    }

    notifications.value = [item, ...notifications.value].slice(0, MAX_HISTORY_ITEMS)
    persistNotifications()

    const statusTag =
      payload.tone === 'error' ? '执行异常' : payload.tone === 'warning' ? '需关注' : '已完成'
    ElNotification({
      title: `【${kindLabel}】${statusTag}`,
      message: `${cleanTitle}：${payload.summary}（点击查看详情）`,
      type: payload.tone,
      position: 'top-right',
      duration: 6500,
      onClick: () => {
        openNotification(item)
      },
    })
  }

  async function pollTrackedJobs(): Promise<void> {
    if (pollingBusy || trackedJobs.value.length === 0) {
      if (trackedJobs.value.length === 0) stopPoller()
      return
    }
    pollingBusy = true
    try {
      const snapshot = [...trackedJobs.value]
      for (const job of snapshot) {
        try {
          if (job.kind === 'check') {
            const detail = await checksApi.detail(job.targetId)
            const t = detail.task
            if (['extracting', 'checking', 'grading', 'created'].includes(t.status)) {
              job.status = t.status
              job.progress = t.progress
              job.stageDetail = t.stage_detail || '正在核验…'
              continue
            }
            const errCnt = Number(t.stats_json?.error_count || 0)
            const warnCnt = Number(t.stats_json?.warning_count || 0)
            const needManual = t.stats_json?.price_status === 'NEED_MANUAL' || t.status === 'need_manual'
            if (t.status === 'failed') {
              notifyTaskTerminal({
                kind: 'check',
                targetId: t.id,
                title: t.title || job.title,
                summary: t.error_msg || '防废标体检流水线执行失败',
                tone: 'error',
                routePath: '/checks',
                routeQuery: { task_id: String(t.id) },
                dedupSuffix: `failed:${t.updated_at || ''}`,
              })
            } else {
              const summaryParts = [
                errCnt > 0 ? `检出 ${errCnt} 项高危/否决风险` : '未发现高危否决风险',
                warnCnt > 0 ? `${warnCnt} 项警告` : '',
                needManual ? '报价需人工回填' : '',
              ].filter(Boolean)
              notifyTaskTerminal({
                kind: 'check',
                targetId: t.id,
                title: t.title || job.title,
                summary: summaryParts.join('，'),
                tone: needManual || errCnt > 0 ? 'warning' : 'success',
                routePath: '/checks',
                routeQuery: { task_id: String(t.id) },
                dedupSuffix: `done:${t.updated_at || ''}`,
              })
            }
          } else if (job.kind === 'score') {
            const est = await checksApi.getScoreEstimate(job.targetId)
            if (['queued', 'running'].includes(est.status)) {
              job.status = est.status
              job.progress = est.progress
              job.stageDetail = est.stage_detail || '评分预估中…'
              continue
            }
            if (est.status === 'completed') {
              const vetoTxt = est.has_veto_risk ? `（含 ${est.veto_alerts.length} 项一票否决警示）` : ''
              notifyTaskTerminal({
                kind: 'score',
                targetId: job.targetId,
                title: job.title,
                summary: `非价格项预估 ${est.assessed_estimated_score}/${est.assessed_max_score} 分${vetoTxt}`,
                tone: est.has_veto_risk ? 'warning' : 'success',
                routePath: '/checks',
                routeQuery: { task_id: String(job.targetId) },
                dedupSuffix: `completed:${est.updated_at || ''}`,
              })
            } else if (est.status === 'failed') {
              notifyTaskTerminal({
                kind: 'score',
                targetId: job.targetId,
                title: job.title,
                summary: est.error_msg || '标书评分预估执行失败',
                tone: 'error',
                routePath: '/checks',
                routeQuery: { task_id: String(job.targetId) },
                dedupSuffix: `failed:${est.updated_at || ''}`,
              })
            } else {
              untrackJob('score', job.targetId)
            }
          } else if (job.kind === 'composer') {
            const draft = await composerApi.getDraft(job.targetId)
            const dStatus = draft.status || 'completed'
            if (['queued', 'running'].includes(dStatus)) {
              job.status = dStatus
              job.progress = draft.progress
              job.stageDetail = draft.stage_detail || '标书初稿编制中…'
              continue
            }
            if (dStatus === 'completed') {
              notifyTaskTerminal({
                kind: 'composer',
                targetId: draft.id,
                title: draft.title || job.title,
                summary: '投标文件初稿已生成，可前往预览并下载 .docx',
                tone: 'success',
                routePath: '/composer',
                dedupSuffix: `completed:${draft.created_at || ''}`,
              })
            } else if (dStatus === 'cancelled') {
              untrackJob('composer', job.targetId)
            } else {
              notifyTaskTerminal({
                kind: 'composer',
                targetId: draft.id,
                title: draft.title || job.title,
                summary: draft.error_msg || '标书初稿生成失败',
                tone: 'error',
                routePath: '/composer',
                dedupSuffix: `failed:${draft.created_at || ''}`,
              })
            }
          } else if (job.kind === 'similarity') {
            const rep = await similarityApi.detail(job.targetId)
            const rStatus = rep.status || 'completed'
            if (['queued', 'running'].includes(rStatus)) {
              job.status = rStatus
              job.progress = rep.progress
              job.stageDetail = rep.stage_detail || '多标书交叉比对中…'
              continue
            }
            if (rStatus === 'completed') {
              notifyTaskTerminal({
                kind: 'similarity',
                targetId: rep.id,
                title: rep.title || job.title,
                summary: `最高净文本相似度 ${rep.max_similarity_pct}%（风险等级：${rep.risk_level}）`,
                tone: rep.risk_level === 'high' ? 'warning' : 'success',
                routePath: '/similarity',
                dedupSuffix: `completed:${rep.created_at || ''}`,
              })
            } else if (rStatus === 'cancelled') {
              untrackJob('similarity', job.targetId)
            } else {
              notifyTaskTerminal({
                kind: 'similarity',
                targetId: rep.id,
                title: rep.title || job.title,
                summary: rep.error_msg || '多标书交叉比对失败',
                tone: 'error',
                routePath: '/similarity',
                dedupSuffix: `failed:${rep.created_at || ''}`,
              })
            }
          } else if (job.kind === 'radar') {
            const rad = await radarApi.detail(job.targetId)
            notifyTaskTerminal({
              kind: 'radar',
              targetId: rad.task_id,
              title: rad.title || job.title,
              summary: `结论：${rad.conclusion?.recommendation || '已完成'}（疑似控标参数 ${rad.suspicious_params?.length || 0} 处）`,
              tone: rad.conclusion?.recommendation === '疑似控标高' ? 'warning' : 'success',
              routePath: '/radar',
              dedupSuffix: `done:${rad.task_id}`,
            })
          }
        } catch (err: unknown) {
          const status = (err as { response?: { status?: number } })?.response?.status
          if (status === 404 || status === 401 || status === 403) {
            untrackJob(job.kind, job.targetId)
          }
        }
      }
    } finally {
      pollingBusy = false
      if (trackedJobs.value.length === 0) {
        stopPoller()
      }
    }
  }

  function markRead(id: string): void {
    const found = notifications.value.find((n) => n.id === id)
    if (found && !found.read) {
      found.read = true
      persistNotifications()
    }
  }

  function markAllRead(): void {
    let changed = false
    for (const item of notifications.value) {
      if (!item.read) {
        item.read = true
        changed = true
      }
    }
    if (changed) {
      persistNotifications()
    }
  }

  function clearNotifications(): void {
    notifications.value = []
    persistNotifications()
  }

  function stopAndReset(): void {
    stopPoller()
    trackedJobs.value = []
    notifications.value = []
    seenDedupKeys.clear()
    activeScopeKey.value = ''
  }

  return {
    trackedJobs,
    notifications,
    runningCount,
    unreadCount,
    syncUserScope,
    registerTrackedJob,
    untrackJob,
    notifyTaskTerminal,
    openNotification,
    navigateToTarget,
    markRead,
    markAllRead,
    clearNotifications,
    stopAndReset,
  }
})
