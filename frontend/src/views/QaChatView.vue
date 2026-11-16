<script lang="ts">
import { ref } from 'vue'
import type {
  BidTaskOut,
  QaAttachmentOut,
  QaAttachmentSummary,
  QaMessageItem,
  QaSessionOut,
  QaStreamErrorType,
} from '@/api/types'

/**
 * E28：每个会话拥有独立的并发运行态，切换项目/切换会话绝不打断后台仍在生成的 SSE 流；
 * 同时通过单会话 streamToken + asking 锁严守「同会话单流不变量」。
 */
interface QaSessionRuntime {
  sessionId: number
  messages: QaMessageItem[]
  attachments: QaAttachmentOut[]
  selectedAttachmentIds: string[]
  suggestedQuestions: string[]
  asking: boolean
  lastTurnPersisted: boolean
  hydrated: boolean
  uploadingAttachment: boolean
  cancelFn: (() => void) | null
  streamToken: number
}

const PINNED_STORAGE_PREFIX = 'bidguard_qa_pinned_sessions_v1_'

/**
 * 模块级响应式缓存（绑定 cachedOwnerUsername，切换账号或登出时立即清零并中止全部活跃流）
 */
const cachedOwnerUsername = ref<string>('')
const cachedTasks = ref<BidTaskOut[]>([])
const cachedSessions = ref<QaSessionOut[]>([])
const cachedSelectedTaskId = ref<number | null>(null)
const cachedActiveSessionId = ref<number | null>(null)
const cachedRuntimes = ref<Record<number, QaSessionRuntime>>({})
const cachedPinnedIds = ref<number[]>([])
const cachedExpandedTaskIds = ref<Record<number, boolean>>({})
let initializedOnce = false

function createEmptyRuntime(sessionId: number): QaSessionRuntime {
  return {
    sessionId,
    messages: [],
    attachments: [],
    selectedAttachmentIds: [],
    suggestedQuestions: [],
    asking: false,
    lastTurnPersisted: true,
    hydrated: false,
    uploadingAttachment: false,
    cancelFn: null,
    streamToken: 0,
  }
}

function getOrCreateRuntime(sessionId: number): QaSessionRuntime {
  if (!cachedRuntimes.value[sessionId]) {
    cachedRuntimes.value[sessionId] = createEmptyRuntime(sessionId)
  }
  return cachedRuntimes.value[sessionId]
}

function stopSessionStream(sessionId: number | null | undefined): void {
  if (!sessionId) return
  const rt = cachedRuntimes.value[sessionId]
  if (!rt) return
  rt.streamToken += 1
  if (rt.cancelFn) {
    try {
      rt.cancelFn()
    } catch {
      // ignore abort errors
    }
    rt.cancelFn = null
  }
  rt.asking = false
}

function stopAllQaStreams(): void {
  for (const rawId of Object.keys(cachedRuntimes.value)) {
    stopSessionStream(Number(rawId))
  }
}

function resetQaModuleCache(): void {
  stopAllQaStreams()
  cachedOwnerUsername.value = ''
  cachedTasks.value = []
  cachedSessions.value = []
  cachedSelectedTaskId.value = null
  cachedActiveSessionId.value = null
  cachedRuntimes.value = {}
  cachedPinnedIds.value = []
  cachedExpandedTaskIds.value = {}
  initializedOnce = false
}

function loadPinnedIdsFromStorage(username: string): number[] {
  try {
    const raw = localStorage.getItem(`${PINNED_STORAGE_PREFIX}${username || 'anon'}`)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.map((v) => Number(v)).filter((n) => Number.isInteger(n) && n > 0)
  } catch {
    return []
  }
}

function savePinnedIdsToStorage(username: string, ids: number[]): void {
  try {
    localStorage.setItem(`${PINNED_STORAGE_PREFIX}${username || 'anon'}`, JSON.stringify(ids))
  } catch {
    // ignore storage quota errors
  }
}
</script>

<script setup lang="ts">
import type { ReasoningEffortLevel } from '@/api/types'
import { computed, nextTick, onActivated, onBeforeUnmount, onMounted, ref as localRef } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { checksApi, qaApi } from '@/api'
import { useAppStore } from '@/stores/app'
import { useAuthStore } from '@/stores/auth'
import QaMessageList from '@/components/QaMessageList.vue'

defineOptions({ name: 'QaChatView' })

interface ProjectTreeGroup {
  taskId: number
  title: string
  taskStatus?: string
  unpinnedSessions: QaSessionOut[]
  pinnedSessionsInGroup: QaSessionOut[]
  totalCount: number
  hasRunningStream: boolean
}

const appStore = useAppStore()
const authStore = useAuthStore()
const loadingInit = localRef(false)
const creatingForTaskId = localRef<number | null>(null)
const editingSessionId = localRef<number | null>(null)
const editingTitleDraft = localRef('')
const editingSource = localRef<'sidebar' | 'header'>('sidebar')
const renamingBusy = localRef(false)
const sidebarFilterOpen = localRef(false)
const sidebarSearchQuery = localRef('')
const onlyActiveProjects = localRef(false)
const nowTick = localRef(Date.now())
const msgListRef = localRef<InstanceType<typeof QaMessageList> | null>(null)
let clockTimer: ReturnType<typeof setInterval> | null = null

const tasks = cachedTasks
const sessions = cachedSessions
const selectedTaskId = cachedSelectedTaskId
const activeSessionId = cachedActiveSessionId
const runtimes = cachedRuntimes
const pinnedIds = cachedPinnedIds
const expandedTaskIds = cachedExpandedTaskIds

const PRESET_QUESTIONS = [
  '招标文件要求的免费质保期是多久？我方投标书在哪响应的？',
  '评分细则表里关于业绩和证书的加分项有哪些？',
  '本项目最高投标限价是多少？我方总报价是多少？',
  '招标文件中带 ★ 号的强制性技术要求有哪些？',
  '分“资格废标门槛”和“评分加分项”列出招标文件要求提供的全部企业证照、产品认证及人员证书并标明出处页码。',
]

const EFFORT_STORAGE_KEY = 'bidguard_qa_reasoning_effort_v1'
const savedEffort = localStorage.getItem(EFFORT_STORAGE_KEY)
const reasoningEffort = localRef<ReasoningEffortLevel>(
  savedEffort === 'low' || savedEffort === 'medium' || savedEffort === 'high' ? savedEffort : 'medium',
)

function setReasoningEffort(level: ReasoningEffortLevel): void {
  reasoningEffort.value = level
  try {
    localStorage.setItem(EFFORT_STORAGE_KEY, level)
  } catch {
    // ignore storage errors
  }
}

const activeRuntime = computed<QaSessionRuntime | null>(() => {
  const sid = activeSessionId.value
  return sid != null ? runtimes.value[sid] || null : null
})

const messages = computed<QaMessageItem[]>(() => activeRuntime.value?.messages || [])
const asking = computed<boolean>(() => Boolean(activeRuntime.value?.asking))
const uploadingAttachment = computed<boolean>(() => Boolean(activeRuntime.value?.uploadingAttachment))
const attachments = computed<QaAttachmentOut[]>(() => activeRuntime.value?.attachments || [])
const selectedAttachmentIds = computed<string[]>({
  get() {
    return activeRuntime.value?.selectedAttachmentIds || []
  },
  set(nextIds: string[]) {
    const sid = activeSessionId.value
    if (!sid) return
    const rt = getOrCreateRuntime(sid)
    rt.selectedAttachmentIds = [...nextIds]
  },
})

const effectivePresetQuestions = computed<string[]>(() => {
  const cur = activeRuntime.value?.suggestedQuestions || []
  return cur.length > 0 ? cur : PRESET_QUESTIONS
})

const activeSession = computed<QaSessionOut | null>(
  () => sessions.value.find((s) => s.id === activeSessionId.value) || null,
)

const selectedTask = computed<BidTaskOut | null>(
  () => tasks.value.find((t) => t.id === selectedTaskId.value) || null,
)

const activeRunningCount = computed<number>(
  () => Object.values(runtimes.value).filter((rt) => rt.asking).length,
)

function isSessionAsking(sessionId?: number | null): boolean {
  return Boolean(sessionId && runtimes.value[sessionId]?.asking)
}

function isPinned(sessionId: number): boolean {
  return pinnedIds.value.includes(sessionId)
}

function parseUtcIsoMs(raw?: string | null): number {
  if (!raw) return 0
  const trimmed = raw.trim()
  if (!trimmed) return 0
  const hasTz = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(trimmed)
  const ms = Date.parse(hasTz ? trimmed : `${trimmed}Z`)
  return Number.isNaN(ms) ? 0 : ms
}

function formatRelativeTime(raw?: string | null, currentNowMs = nowTick.value): string {
  const ts = parseUtcIsoMs(raw)
  if (!ts) return ''
  const diffSec = Math.max(0, Math.floor((currentNowMs - ts) / 1000))
  if (diffSec < 60) return '刚刚'
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m`
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h`
  if (diffSec < 86400 * 30) return `${Math.floor(diffSec / 86400)}d`
  const dt = new Date(ts)
  const mm = String(dt.getMonth() + 1).padStart(2, '0')
  const dd = String(dt.getDate()).padStart(2, '0')
  return `${dt.getFullYear()}-${mm}-${dd}`
}

function sortSessionsByRecency(list: QaSessionOut[]): QaSessionOut[] {
  return [...list].sort((a, b) => {
    const dt = parseUtcIsoMs(b.updated_at || b.created_at) - parseUtcIsoMs(a.updated_at || a.created_at)
    return dt !== 0 ? dt : b.id - a.id
  })
}

const taskTitleById = computed<Record<number, string>>(() => {
  const map: Record<number, string> = {}
  for (const t of tasks.value) {
    map[t.id] = t.title
  }
  return map
})

function resolveProjectLabel(taskId?: number | null): string {
  if (!taskId) return '独立问答会话'
  return taskTitleById.value[taskId] || `体检任务 #${taskId}`
}

const pinnedSessions = computed<QaSessionOut[]>(() => {
  const pinSet = new Set(pinnedIds.value)
  const q = sidebarSearchQuery.value.trim().toLowerCase()
  const filtered = sessions.value.filter((s) => {
    if (!pinSet.has(s.id)) return false
    if (!q) return true
    const projName = resolveProjectLabel(s.task_id).toLowerCase()
    return s.title.toLowerCase().includes(q) || projName.includes(q)
  })
  return sortSessionsByRecency(filtered)
})

const projectGroups = computed<ProjectTreeGroup[]>(() => {
  const pinSet = new Set(pinnedIds.value)
  const q = sidebarSearchQuery.value.trim().toLowerCase()

  const sessionsByTask = new Map<number, QaSessionOut[]>()
  for (const s of sessions.value) {
    const key = s.task_id ?? -1
    const bucket = sessionsByTask.get(key) || []
    bucket.push(s)
    sessionsByTask.set(key, bucket)
  }

  const knownTaskIds = new Set<number>()
  const groups: ProjectTreeGroup[] = []

  const buildGroup = (taskId: number, title: string, taskStatus?: string): ProjectTreeGroup | null => {
    const rawList = sortSessionsByRecency(sessionsByTask.get(taskId) || [])
    const projectMatchesQuery = !q || title.toLowerCase().includes(q) || `#${taskId}`.includes(q)
    const matchedSessions = q && !projectMatchesQuery
      ? rawList.filter((s) => s.title.toLowerCase().includes(q))
      : rawList

    if (q && !projectMatchesQuery && matchedSessions.length === 0) {
      return null
    }
    if (onlyActiveProjects.value && rawList.length === 0 && selectedTaskId.value !== taskId) {
      return null
    }

    const unpinned = matchedSessions.filter((s) => !pinSet.has(s.id))
    const pinnedInGrp = matchedSessions.filter((s) => pinSet.has(s.id))
    return {
      taskId,
      title,
      taskStatus,
      unpinnedSessions: unpinned,
      pinnedSessionsInGroup: pinnedInGrp,
      totalCount: rawList.length,
      hasRunningStream: rawList.some((s) => isSessionAsking(s.id)),
    }
  }

  for (const t of tasks.value) {
    knownTaskIds.add(t.id)
    const grp = buildGroup(t.id, t.title, t.status)
    if (grp) groups.push(grp)
  }

  // 保底兜底：若存在超出分页或无 task_id 的历史会话，也保留项目分组防遗漏
  for (const tid of sessionsByTask.keys()) {
    if (!knownTaskIds.has(tid)) {
      const label = tid > 0 ? `历史体检任务 #${tid}` : '未绑定任务的独立会话'
      const grp = buildGroup(tid, label)
      if (grp) groups.push(grp)
    }
  }

  return groups
})

function isGroupExpanded(taskId: number): boolean {
  if (sidebarSearchQuery.value.trim()) return true
  if (expandedTaskIds.value[taskId] !== undefined) {
    return expandedTaskIds.value[taskId]
  }
  return false
}

function toggleProjectGroup(taskId: number): void {
  const next = !isGroupExpanded(taskId)
  expandedTaskIds.value = {
    ...expandedTaskIds.value,
    [taskId]: next,
  }
  if (taskId > 0) {
    selectedTaskId.value = taskId
    appStore.setActiveTaskId(taskId)
  }
}

function togglePinSession(sess: QaSessionOut): void {
  const username = cachedOwnerUsername.value || authStore.user?.username || ''
  const exists = pinnedIds.value.includes(sess.id)
  const next = exists
    ? pinnedIds.value.filter((id) => id !== sess.id)
    : [sess.id, ...pinnedIds.value]
  pinnedIds.value = next
  savePinnedIdsToStorage(username, next)
}

function seedRuntimeFromSummary(sess: QaSessionOut): QaSessionRuntime {
  const rt = getOrCreateRuntime(sess.id)
  if (!rt.hydrated && !rt.asking) {
    rt.messages = [...(sess.messages_json || [])]
    rt.attachments = [...(sess.attachments || [])]
    if (rt.selectedAttachmentIds.length === 0 && rt.attachments.length > 0) {
      rt.selectedAttachmentIds = rt.attachments.map((a) => a.attachment_id)
    }
    rt.suggestedQuestions = [...(sess.suggested_questions || [])]
  }
  return rt
}

function ensureAccountIsolation(): boolean {
  const curUser = authStore.user?.username || ''
  if (cachedOwnerUsername.value && cachedOwnerUsername.value !== curUser) {
    resetQaModuleCache()
  }
  cachedOwnerUsername.value = curUser
  if (cachedPinnedIds.value.length === 0) {
    cachedPinnedIds.value = loadPinnedIdsFromStorage(curUser)
  }
  return Boolean(curUser)
}

function handleAuthChanged(): void {
  resetQaModuleCache()
}

async function loadTasksAndSessions(force = false): Promise<void> {
  ensureAccountIsolation()
  if (initializedOnce && !force && sessions.value.length > 0) {
    return
  }
  loadingInit.value = true
  try {
    const [taskRes, sessList] = await Promise.all([
      checksApi.list({ task_type: 'pre_check', page: 1, page_size: 50 }),
      qaApi.listSessions(),
    ])
    tasks.value = taskRes.items || []
    sessions.value = sessList || []

    // 清理已不存在的置顶残留 ID
    const validIds = new Set(sessions.value.map((s) => s.id))
    const cleanedPins = pinnedIds.value.filter((id) => validIds.has(id))
    if (cleanedPins.length !== pinnedIds.value.length) {
      pinnedIds.value = cleanedPins
      savePinnedIdsToStorage(cachedOwnerUsername.value, cleanedPins)
    }

    // 初始化项目展开状态：默认展开已有会话的项目与当前活跃任务
    const nextExpanded = { ...expandedTaskIds.value }
    for (const s of sessions.value) {
      const k = s.task_id ?? -1
      if (nextExpanded[k] === undefined) nextExpanded[k] = true
      seedRuntimeFromSummary(s)
    }

    if (!selectedTaskId.value && tasks.value.length > 0) {
      const preferredId = appStore.activeTaskId
      selectedTaskId.value =
        preferredId && tasks.value.some((t) => t.id === preferredId)
          ? preferredId
          : tasks.value[0].id
    }
    if (selectedTaskId.value && nextExpanded[selectedTaskId.value] === undefined) {
      nextExpanded[selectedTaskId.value] = true
    }
    expandedTaskIds.value = nextExpanded

    // 默认选中：保留当前仍存在会话 -> 置顶首条 -> 当前任务最新会话 -> 全局最新会话
    if (!activeSessionId.value || !validIds.has(activeSessionId.value)) {
      const sorted = sortSessionsByRecency(sessions.value)
      const taskPrefer = sorted.find((s) => s.task_id === selectedTaskId.value)
      const fallback = taskPrefer || sorted[0] || null
      if (fallback) {
        await selectSession(fallback)
      } else {
        activeSessionId.value = null
      }
    }

    initializedOnce = true
  } finally {
    loadingInit.value = false
  }
}

async function refreshAllSessionsMeta(): Promise<void> {
  const list = await qaApi.listSessions()
  sessions.value = list || []
  for (const s of sessions.value) {
    seedRuntimeFromSummary(s)
  }
}

async function selectSession(sess: QaSessionOut): Promise<void> {
  // E28：支持在流式回答进行中随时切换至其他会话或项目，绝不阻塞或打断其他会话的后台流
  activeSessionId.value = sess.id
  if (sess.task_id && sess.task_id > 0) {
    selectedTaskId.value = sess.task_id
    appStore.setActiveTaskId(sess.task_id)
    expandedTaskIds.value = { ...expandedTaskIds.value, [sess.task_id]: true }
  }

  const rt = seedRuntimeFromSummary(sess)
  void nextTick(() => msgListRef.value?.scrollToBottom())

  if (rt.asking || rt.hydrated) {
    return
  }

  const tokenSnapshot = rt.streamToken
  try {
    const detail = await qaApi.getSession(sess.id)
    const latestRt = runtimes.value[sess.id]
    if (!latestRt || latestRt.asking || latestRt.streamToken !== tokenSnapshot) {
      return
    }
    latestRt.messages = [...(detail.messages_json || [])]
    latestRt.attachments = [...(detail.attachments || [])]
    latestRt.selectedAttachmentIds = latestRt.attachments.map((a) => a.attachment_id)
    latestRt.suggestedQuestions = [...(detail.suggested_questions || [])]
    latestRt.hydrated = true
    if (activeSessionId.value === sess.id) {
      void nextTick(() => msgListRef.value?.scrollToBottom())
    }
  } catch {
    // 已预填充摘要历史，详情刷新失败时保留可浏览状态
  }
}

async function handleCreateSessionForTask(taskId: number): Promise<void> {
  if (creatingForTaskId.value !== null || taskId <= 0) return
  creatingForTaskId.value = taskId
  try {
    const created = await qaApi.createSession({ task_id: taskId })
    sessions.value = [created, ...sessions.value]
    selectedTaskId.value = taskId
    appStore.setActiveTaskId(taskId)
    expandedTaskIds.value = { ...expandedTaskIds.value, [taskId]: true }

    const rt = getOrCreateRuntime(created.id)
    rt.messages = []
    rt.attachments = []
    rt.selectedAttachmentIds = []
    rt.suggestedQuestions = [...(created.suggested_questions || [])]
    rt.hydrated = true
    rt.lastTurnPersisted = true
    activeSessionId.value = created.id
    ElMessage.success(`已新建问答会话 #${created.id}`)
  } finally {
    creatingForTaskId.value = null
  }
}

async function handleCreateSessionInActiveProject(): Promise<void> {
  const tid = selectedTaskId.value || tasks.value[0]?.id
  if (!tid) {
    ElMessage.warning('请先创建或选择一个招投标体检任务')
    return
  }
  await handleCreateSessionForTask(tid)
}

async function handleDeleteSession(sess: QaSessionOut): Promise<void> {
  if (isSessionAsking(sess.id)) {
    ElMessage.warning('该会话正在生成回答，请先停止生成后再删除')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确认删除专家问答会话「#${sess.id} ${sess.title}」及其全部对话记录与会话附件吗？删除后不可恢复。`,
      '确认删除问答会话',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }

  await qaApi.deleteSession(sess.id)
  sessions.value = sessions.value.filter((s) => s.id !== sess.id)
  if (pinnedIds.value.includes(sess.id)) {
    const nextPins = pinnedIds.value.filter((id) => id !== sess.id)
    pinnedIds.value = nextPins
    savePinnedIdsToStorage(cachedOwnerUsername.value, nextPins)
  }
  stopSessionStream(sess.id)
  const copy = { ...runtimes.value }
  delete copy[sess.id]
  runtimes.value = copy

  if (activeSessionId.value === sess.id) {
    const sorted = sortSessionsByRecency(sessions.value)
    const sameTaskNext = sorted.find((s) => s.task_id === sess.task_id) || sorted[0] || null
    if (sameTaskNext) {
      await selectSession(sameTaskNext)
    } else {
      activeSessionId.value = null
    }
  }
  ElMessage.success('问答会话已删除')
}

function isDefaultSessionTitle(title?: string | null): boolean {
  const t = (title || '').trim()
  if (!t) return true
  if (t === '新对话' || t === '标书专家追问会话') return true
  if (t.endsWith(' · 专家追问') || t.endsWith('·专家追问')) return true
  return false
}

function startRenameSession(sess: QaSessionOut, source: 'sidebar' | 'header' = 'sidebar'): void {
  editingSessionId.value = sess.id
  editingTitleDraft.value = sess.title || ''
  editingSource.value = source
  void nextTick(() => {
    const input = document.querySelector<HTMLInputElement>('.sess-rename-input')
    if (input) {
      input.focus()
      input.select()
    }
  })
}

function cancelRenameSession(): void {
  editingSessionId.value = null
  editingTitleDraft.value = ''
}

async function commitRenameSession(sess: QaSessionOut): Promise<void> {
  if (editingSessionId.value !== sess.id || renamingBusy.value) return
  const nextTitle = editingTitleDraft.value.trim()
  editingSessionId.value = null
  editingTitleDraft.value = ''
  if (!nextTitle || nextTitle === (sess.title || '').trim()) {
    return
  }
  renamingBusy.value = true
  try {
    const updated = await qaApi.renameSession(sess.id, { title: nextTitle })
    const idx = sessions.value.findIndex((item) => item.id === sess.id)
    if (idx >= 0) {
      sessions.value[idx] = {
        ...sessions.value[idx],
        title: updated.title,
        updated_at: updated.updated_at || sessions.value[idx].updated_at,
      }
    }
    ElMessage.success('会话标题已更新')
  } finally {
    renamingBusy.value = false
  }
}

async function handleUploadAttachment(file: File): Promise<void> {
  let sid = activeSessionId.value
  if (sid && isSessionAsking(sid)) return

  if (!sid) {
    const tid = selectedTaskId.value || tasks.value[0]?.id
    if (!tid) {
      ElMessage.warning('请先选择关联的投标体检任务后再上传附件')
      return
    }
    const created = await qaApi.createSession({ task_id: tid })
    sessions.value = [created, ...sessions.value]
    expandedTaskIds.value = { ...expandedTaskIds.value, [tid]: true }
    const newRt = getOrCreateRuntime(created.id)
    newRt.suggestedQuestions = [...(created.suggested_questions || [])]
    newRt.hydrated = true
    activeSessionId.value = created.id
    sid = created.id
  }

  const rt = getOrCreateRuntime(sid)
  if (rt.asking || rt.uploadingAttachment) return

  rt.uploadingAttachment = true
  try {
    const uploaded = await qaApi.uploadAttachment(sid, file)
    const latestRt = getOrCreateRuntime(sid)
    latestRt.attachments = [...latestRt.attachments, uploaded]
    if (!latestRt.selectedAttachmentIds.includes(uploaded.attachment_id)) {
      latestRt.selectedAttachmentIds = [...latestRt.selectedAttachmentIds, uploaded.attachment_id]
    }
    try {
      const refreshed = await qaApi.getSession(sid)
      latestRt.suggestedQuestions = [...(refreshed.suggested_questions || [])]
    } catch {
      // ignore suggestion refresh failure
    }
    const desc = uploaded.is_image
      ? uploaded.has_text
        ? `OCR 识别 ${uploaded.ocr_chars} 字`
        : '纯图片（无文字）'
      : '文档解析就绪'
    ElMessage.success(`已上传附件「${uploaded.filename}」（${desc}）`)
  } finally {
    const finalRt = runtimes.value[sid]
    if (finalRt) finalRt.uploadingAttachment = false
  }
}

async function handleDeleteAttachment(att: QaAttachmentOut): Promise<void> {
  const sid = activeSessionId.value
  if (!sid || isSessionAsking(sid)) return
  const rt = getOrCreateRuntime(sid)
  await qaApi.deleteAttachment(sid, att.attachment_id)
  rt.attachments = rt.attachments.filter((a) => a.attachment_id !== att.attachment_id)
  rt.selectedAttachmentIds = rt.selectedAttachmentIds.filter((id) => id !== att.attachment_id)
  try {
    const refreshed = await qaApi.getSession(sid)
    rt.suggestedQuestions = [...(refreshed.suggested_questions || [])]
  } catch {
    // ignore suggestion refresh failure
  }
  ElMessage.success(`已移除附件「${att.filename}」`)
}

async function handleSendQuestion(question: string, attachmentIds: string[] = []): Promise<void> {
  let targetSessionId = activeSessionId.value
  if (!targetSessionId) {
    const tid = selectedTaskId.value || tasks.value[0]?.id
    if (!tid) {
      ElMessage.warning('请先选择左侧项目后再发起提问')
      return
    }
    const created = await qaApi.createSession({ task_id: tid })
    sessions.value = [created, ...sessions.value]
    expandedTaskIds.value = { ...expandedTaskIds.value, [tid]: true }
    const createdRt = getOrCreateRuntime(created.id)
    createdRt.suggestedQuestions = [...(created.suggested_questions || [])]
    createdRt.hydrated = true
    activeSessionId.value = created.id
    targetSessionId = created.id
  }

  const rt = getOrCreateRuntime(targetSessionId)
  // 同会话单流不变量：同一会话流式未结束前严禁重复并发提交
  if (rt.asking) {
    ElMessage.warning('当前会话正在检索生成中，请先停止生成或切换到其他会话并行提问')
    return
  }

  const pickedSummaries: QaAttachmentSummary[] = []
  const validIds: string[] = []
  let imgCount = 0
  for (const id of attachmentIds || []) {
    const matched = rt.attachments.find((a) => a.attachment_id === id)
    if (matched) {
      validIds.push(matched.attachment_id)
      if (matched.is_image) imgCount += 1
      pickedSummaries.push({
        attachment_id: matched.attachment_id,
        filename: matched.filename,
        is_image: matched.is_image,
        ocr_chars: matched.ocr_chars,
      })
    }
  }
  if (imgCount > 4) {
    ElMessage.warning(`单轮追问最多允许勾选 4 张图片附件（当前选择了 ${imgCount} 张）`)
    return
  }

  rt.messages.push({
    role: 'user',
    content: question,
    attachment_ids: validIds,
    attachments: pickedSummaries,
  })
  const targetSessRow = sessions.value.find((s) => s.id === targetSessionId)
  const trimmedQuestion = question.trim()
  if (targetSessRow && isDefaultSessionTitle(targetSessRow.title) && trimmedQuestion) {
    targetSessRow.title = trimmedQuestion.slice(0, 30)
  }
  rt.messages.push({
    role: 'assistant',
    content: '',
    steps: [],
    quotes: [],
    loading: true,
    streaming: true,
  })
  const assistantMsgIdx = rt.messages.length - 1

  rt.asking = true
  rt.hydrated = true
  rt.lastTurnPersisted = false
  rt.streamToken += 1
  const myToken = rt.streamToken

  if (activeSessionId.value === targetSessionId) {
    void nextTick(() => msgListRef.value?.scrollToBottom())
  }

  const payload = {
    question,
    max_rounds: 7,
    reasoning_effort: reasoningEffort.value,
    attachment_ids: validIds,
  }

  rt.cancelFn = qaApi.askStream(targetSessionId, payload, {
    onStep(stepItem) {
      const cur = runtimes.value[targetSessionId]
      if (!cur || cur.streamToken !== myToken) return
      const msg = cur.messages[assistantMsgIdx]
      if (!msg || msg.role !== 'assistant') return
      msg.steps = [...(msg.steps || []), stepItem]
      if (activeSessionId.value === targetSessionId) {
        msgListRef.value?.scrollToBottom()
      }
    },
    onDelta(deltaText) {
      const cur = runtimes.value[targetSessionId]
      if (!cur || cur.streamToken !== myToken) return
      const msg = cur.messages[assistantMsgIdx]
      if (!msg || msg.role !== 'assistant') return
      msg.content = (msg.content || '') + deltaText
      if (activeSessionId.value === targetSessionId) {
        msgListRef.value?.scrollToBottom()
      }
    },
    async onDone(finalRes) {
      const cur = runtimes.value[targetSessionId]
      if (!cur || cur.streamToken !== myToken) return
      cur.cancelFn = null
      cur.lastTurnPersisted = true
      const msg = cur.messages[assistantMsgIdx]
      if (msg && msg.role === 'assistant') {
        cur.messages[assistantMsgIdx] = {
          role: 'assistant',
          content: finalRes.answer,
          mode: finalRes.mode,
          quotes: finalRes.quotes || [],
          steps: finalRes.steps || msg.steps || [],
          loading: false,
          streaming: false,
        }
      }
      cur.asking = false
      if (activeSessionId.value === targetSessionId) {
        msgListRef.value?.scrollToBottom()
      } else {
        const finishedSess = sessions.value.find((s) => s.id === targetSessionId)
        ElMessage.success({
          message: `后台会话「${finishedSess?.title || `#${targetSessionId}`}」已生成完成`,
          duration: 2600,
        })
      }
      await refreshAllSessionsMeta()
    },
    async onError(errMsg, errorType) {
      const cur = runtimes.value[targetSessionId]
      if (!cur || cur.streamToken !== myToken) return
      cur.cancelFn = null
      const msg = cur.messages[assistantMsgIdx]
      const completedSteps = msg?.steps ? msg.steps.length : 0
      const hasPartialAnswer = Boolean(msg?.content && msg.content.trim().length > 0)

      const resolveQaErrorKind = (
        text?: string,
        explicit?: QaStreamErrorType,
        errObj?: unknown,
      ): QaStreamErrorType => {
        if (explicit === 'gateway' || explicit === 'tool_or_internal') {
          return explicit
        }
        const rawErr = errObj as {
          response?: { data?: { detail?: { error_type?: string } } }
          detail?: { error_type?: string }
        } | null
        const nestedType =
          rawErr?.response?.data?.detail?.error_type || rawErr?.detail?.error_type
        if (nestedType === 'gateway' || nestedType === 'tool_or_internal') {
          return nestedType
        }
        // 兼容未传 error_type 的旧版错误响应：仅作兜底关键字识别
        const lower = (text || '').toLowerCase()
        if (
          lower.includes('qa_execution_error') ||
          lower.includes('问答工具或内部解析执行异常') ||
          lower.includes('invalidoperation') ||
          lower.includes('conversionsyntax') ||
          lower.includes('valueerror') ||
          lower.includes('typeerror') ||
          lower.includes('keyerror') ||
          lower.includes('indexerror') ||
          lower.includes('zerodivisionerror')
        ) {
          return 'tool_or_internal'
        }
        if (
          lower.includes('timeout') ||
          lower.includes('timed out') ||
          lower.includes('connection') ||
          lower.includes('tun') ||
          lower.includes('proxy') ||
          lower.includes('network') ||
          lower.includes('fetch') ||
          lower.includes('502') ||
          lower.includes('503') ||
          lower.includes('504') ||
          lower.includes('429') ||
          lower.includes('网关') ||
          lower.includes('超时') ||
          lower.includes('连接')
        ) {
          return 'gateway'
        }
        return 'tool_or_internal'
      }

      if (msg && msg.role === 'assistant' && (completedSteps > 0 || hasPartialAnswer)) {
        msg.loading = false
        msg.streaming = false
        msg.mode = 'react'
        const kind = resolveQaErrorKind(errMsg, errorType)
        if (!hasPartialAnswer) {
          msg.content =
            kind === 'gateway'
              ? `⚠️ 模型网关响应超时或连接中断（已完成 ${completedSteps} 步工具检索：${errMsg || 'Connection timeout'}）。已为您保留上方检索到的工具轨迹，可点击下方「重新生成回答」重试或「撤回本轮回答」调整提问。`
              : `⚠️ 问答工具或内部解析执行异常（已完成 ${completedSteps} 步工具检索：${errMsg || 'Tool execution error'}）。非网关连接超时，已为您保留上方工具轨迹，可核对提问格式或点击「重新生成回答」/「撤回本轮回答」调整提问。`
        } else {
          msg.content =
            kind === 'gateway'
              ? `${msg.content}\n\n[提示：模型网关流式响应中途断开（${errMsg || 'Connection interrupted'}），可点击下方「重新生成回答」重试]`
              : `${msg.content}\n\n[提示：问答工具或内部执行异常（${errMsg || 'Internal execution error'}），可点击下方「重新生成回答」或调整提问]`
        }
        cur.asking = false
        if (activeSessionId.value === targetSessionId) {
          msgListRef.value?.scrollToBottom()
        }
        return
      }

      try {
        const res = await qaApi.ask(targetSessionId, payload)
        const afterAskRt = runtimes.value[targetSessionId]
        if (!afterAskRt || afterAskRt.streamToken !== myToken) return
        afterAskRt.lastTurnPersisted = true
        if (afterAskRt.messages[assistantMsgIdx]?.role === 'assistant') {
          afterAskRt.messages[assistantMsgIdx] = {
            role: 'assistant',
            content: res.answer,
            mode: res.mode,
            quotes: res.quotes || [],
            steps: res.steps || [],
            loading: false,
            streaming: false,
          }
        }
        await refreshAllSessionsMeta()
      } catch (askErr: unknown) {
        const errRt = runtimes.value[targetSessionId]
        if (!errRt || errRt.streamToken !== myToken) return
        const curMsg = errRt.messages[assistantMsgIdx]
        const fallbackDetail =
          askErr instanceof Error && askErr.message ? askErr.message : errMsg || '执行异常'
        const fallbackKind = resolveQaErrorKind(fallbackDetail, undefined, askErr)
        if (curMsg && curMsg.role === 'assistant') {
          curMsg.loading = false
          curMsg.streaming = false
          curMsg.content =
            fallbackKind === 'gateway'
              ? `⚠️ 模型网关连接超时或不可达（${fallbackDetail}）。已保护会话不被 0-Token 原始碎片污染，可排查 TUN 代理后点击下方「重新生成回答」，或点击「撤回本轮回答」调整提问。`
              : `⚠️ 问答工具或内部解析执行异常（${fallbackDetail}）。非模型网关连接故障，已保护会话不被无效内容污染，可调整提问后点击「重新生成回答」或「撤回本轮回答」。`
        }
      } finally {
        const doneRt = runtimes.value[targetSessionId]
        if (doneRt && doneRt.streamToken === myToken) {
          doneRt.asking = false
          if (activeSessionId.value === targetSessionId) {
            msgListRef.value?.scrollToBottom()
          }
        }
      }
    },
  })
}

function handleStopGeneration(sessionId = activeSessionId.value): void {
  if (!sessionId) return
  stopSessionStream(sessionId)
  const rt = runtimes.value[sessionId]
  if (!rt) return
  const lastIdx = rt.messages.length - 1
  const lastMsg = lastIdx >= 0 ? rt.messages[lastIdx] : null
  if (lastMsg && lastMsg.role === 'assistant' && (lastMsg.loading || lastMsg.streaming)) {
    lastMsg.loading = false
    lastMsg.streaming = false
    if (!lastMsg.content || !lastMsg.content.trim()) {
      const stepCount = lastMsg.steps ? lastMsg.steps.length : 0
      lastMsg.content =
        stepCount > 0
          ? `⏹ 已手动停止生成（已保留上方 ${stepCount} 步工具检索结果，可点击「重新生成回答」重试或「撤回本轮回答」重新编辑）。`
          : '⏹ 已手动停止本轮生成（可点击「重新生成回答」重试或「撤回本轮回答」重新编辑提问）。'
    }
  }
}

async function handleWithdrawLastTurn(fillInput = true): Promise<{
  question: string
  attachmentIds: string[]
}> {
  const sid = activeSessionId.value
  if (!sid) return { question: '', attachmentIds: [] }
  stopSessionStream(sid)
  const rt = getOrCreateRuntime(sid)

  let withdrawnQuestion = ''
  let withdrawnAttIds: string[] = []

  if (rt.messages.length > 0 && rt.messages[rt.messages.length - 1]?.role === 'assistant') {
    rt.messages.pop()
  }
  if (rt.messages.length > 0 && rt.messages[rt.messages.length - 1]?.role === 'user') {
    const lastUser = rt.messages.pop()
    withdrawnQuestion = String(lastUser?.content || '')
    withdrawnAttIds = [...(lastUser?.attachment_ids || [])]
  }

  if (rt.lastTurnPersisted) {
    try {
      const out = await qaApi.deleteLastTurn(sid)
      if (!withdrawnQuestion && out.withdrawn_question) {
        withdrawnQuestion = out.withdrawn_question
      }
      if (withdrawnAttIds.length === 0 && out.withdrawn_attachment_ids?.length) {
        withdrawnAttIds = [...out.withdrawn_attachment_ids]
      }
    } catch {
      // 忽略网络异常，保证前端本地状态可撤回
    }
  }
  rt.lastTurnPersisted = true

  if (fillInput) {
    if (withdrawnQuestion) {
      msgListRef.value?.setQuestionInput(withdrawnQuestion)
    }
    if (withdrawnAttIds.length > 0) {
      rt.selectedAttachmentIds = withdrawnAttIds
    }
    ElMessage.success('已撤回最后一轮回答，原问题已回填至输入框')
  }

  return { question: withdrawnQuestion, attachmentIds: withdrawnAttIds }
}

async function handleRegenerateLastTurn(): Promise<void> {
  const sid = activeSessionId.value
  if (!sid || isSessionAsking(sid)) return
  const { question, attachmentIds } = await handleWithdrawLastTurn(false)
  if (!question.trim()) {
    ElMessage.warning('未找到可重新生成的上一轮提问')
    return
  }
  await handleSendQuestion(question, attachmentIds)
}

function handleJumpToFirstPinned(grp: ProjectTreeGroup): void {
  if (grp.pinnedSessionsInGroup.length > 0) {
    void selectSession(grp.pinnedSessionsInGroup[0])
  }
}

onMounted(() => {
  window.addEventListener('bidguard-auth-changed', handleAuthChanged)
  clockTimer = setInterval(() => {
    nowTick.value = Date.now()
  }, 30_000)
  void loadTasksAndSessions(false)
})

onActivated(() => {
  ensureAccountIsolation()
  if (!initializedOnce || sessions.value.length === 0) {
    void loadTasksAndSessions(true)
  } else {
    void nextTick(() => msgListRef.value?.scrollToBottom())
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('bidguard-auth-changed', handleAuthChanged)
  if (clockTimer !== null) {
    clearInterval(clockTimer)
    clockTimer = null
  }
  if (!authStore.isAuthenticated) {
    stopAllQaStreams()
  }
})
</script>

<template>
  <div v-loading="loadingInit" class="qa-chat-layout">
    <!-- 左栏：Antigravity 风格 Pinned Conversations + Projects 树形导航（多会话并发互不阻塞） -->
    <aside class="qa-sidebar bg-panel">
      <div class="qs-scroll">
        <!-- Pinned Conversations -->
        <div v-if="pinnedSessions.length > 0" class="qs-section">
          <div class="qs-section-hd">
            <span class="qs-section-title">Pinned Conversations</span>
            <span class="qs-count-pill mono-num">{{ pinnedSessions.length }}</span>
          </div>

          <div class="pinned-list">
            <div
              v-for="s in pinnedSessions"
              :key="`pin-${s.id}`"
              class="tree-sess-card"
              :class="{ active: s.id === activeSessionId }"
              @click="selectSession(s)"
            >
              <div class="tsc-top">
                <input
                  v-if="editingSessionId === s.id && editingSource === 'sidebar'"
                  v-model="editingTitleDraft"
                  type="text"
                  class="sess-rename-input"
                  maxlength="256"
                  @click.stop
                  @dblclick.stop
                  @keydown.enter.prevent="commitRenameSession(s)"
                  @keydown.esc.prevent="cancelRenameSession"
                  @blur="commitRenameSession(s)"
                />
                <template v-else>
                  <span
                    class="tsc-title"
                    :title="`${s.title}（双击可重命名）`"
                    @dblclick.stop="startRenameSession(s, 'sidebar')"
                  >
                    {{ s.title }}
                  </span>
                  <div class="tsc-right">
                    <span
                      v-if="isSessionAsking(s.id)"
                      class="stream-grid-spinner"
                      title="该会话正在流式检索生成中（可随时切去其他会话）"
                    >
                      <i></i><i></i><i></i><i></i>
                    </span>
                    <span v-else class="tsc-time mono-num">
                      {{ formatRelativeTime(s.updated_at || s.created_at) }}
                    </span>
                    <div class="tsc-actions">
                      <button
                        type="button"
                        class="tsc-icon-btn"
                        title="重命名会话"
                        @click.stop="startRenameSession(s, 'sidebar')"
                      >
                        ✎
                      </button>
                      <button
                        type="button"
                        class="tsc-icon-btn is-pinned"
                        title="取消置顶"
                        @click.stop="togglePinSession(s)"
                      >
                        📌
                      </button>
                      <button
                        type="button"
                        class="tsc-icon-btn is-del"
                        title="删除会话"
                        :disabled="isSessionAsking(s.id)"
                        @click.stop="handleDeleteSession(s)"
                      >
                        ×
                      </button>
                    </div>
                  </div>
                </template>
              </div>
              <div class="tsc-proj">
                <span class="folder-glyph">📁</span>
                <span class="tsc-proj-name" :title="resolveProjectLabel(s.task_id)">
                  {{ resolveProjectLabel(s.task_id) }}
                </span>
              </div>
            </div>
          </div>
        </div>

        <!-- Projects Tree -->
        <div class="qs-section">
          <div class="qs-section-hd">
            <div class="qs-sec-left">
              <span class="qs-section-title">Projects</span>
              <span v-if="activeRunningCount > 0" class="running-badge mono-num">
                {{ activeRunningCount }} 并发生成中
              </span>
            </div>
            <div class="qs-sec-tools">
              <button
                type="button"
                class="qs-hdr-icon-btn"
                :class="{ active: sidebarFilterOpen || onlyActiveProjects }"
                title="筛选项目或搜索历史对话"
                @click="sidebarFilterOpen = !sidebarFilterOpen"
              >
                <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5">
                  <path d="M2.5 4h11M4.5 8h7M6.5 12h3" stroke-linecap="round" />
                </svg>
              </button>
              <button
                type="button"
                class="qs-hdr-icon-btn"
                :disabled="creatingForTaskId !== null"
                title="在当前选中项目下新建问答会话"
                @click="handleCreateSessionInActiveProject"
              >
                <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5">
                  <path d="M2 4.5a1 1 0 0 1 1-1h3.2l1.2 1.3H13a1 1 0 0 1 1 1v5.7a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V4.5Z" />
                  <path d="M8 7v3.4M6.3 8.7h3.4" stroke-linecap="round" />
                </svg>
              </button>
            </div>
          </div>

          <div v-if="sidebarFilterOpen" class="qs-filter-box">
            <el-input
              v-model="sidebarSearchQuery"
              size="small"
              clearable
              placeholder="搜索项目名称或对话标题…"
            />
            <label class="only-active-chk">
              <input v-model="onlyActiveProjects" type="checkbox" />
              <span>仅显示已有会话项目</span>
            </label>
          </div>

          <div class="proj-tree-list">
            <div v-for="grp in projectGroups" :key="`grp-${grp.taskId}`" class="proj-node">
              <div
                class="proj-row"
                :class="{ selected: grp.taskId === selectedTaskId }"
                @click="toggleProjectGroup(grp.taskId)"
              >
                <span class="proj-caret" :class="{ open: isGroupExpanded(grp.taskId) }">▸</span>
                <span class="folder-glyph">📁</span>
                <span class="proj-name" :title="`#${grp.taskId} ${grp.title}`">
                  {{ grp.taskId > 0 ? `#${grp.taskId} ${grp.title}` : grp.title }}
                </span>
                <span
                  v-if="grp.hasRunningStream && !isGroupExpanded(grp.taskId)"
                  class="stream-grid-spinner mini"
                  title="该项目下有会话正在后台生成"
                >
                  <i></i><i></i><i></i><i></i>
                </span>
                <button
                  v-if="grp.taskId > 0"
                  type="button"
                  class="proj-add-btn"
                  title="在该项目下新建问答会话"
                  :disabled="creatingForTaskId === grp.taskId"
                  @click.stop="handleCreateSessionForTask(grp.taskId)"
                >
                  +
                </button>
              </div>

              <div v-if="isGroupExpanded(grp.taskId)" class="proj-children">
                <div
                  v-for="s in grp.unpinnedSessions"
                  :key="`sess-${s.id}`"
                  class="tree-sess-row"
                  :class="{ active: s.id === activeSessionId }"
                  @click="selectSession(s)"
                >
                  <input
                    v-if="editingSessionId === s.id && editingSource === 'sidebar'"
                    v-model="editingTitleDraft"
                    type="text"
                    class="sess-rename-input"
                    maxlength="256"
                    @click.stop
                    @dblclick.stop
                    @keydown.enter.prevent="commitRenameSession(s)"
                    @keydown.esc.prevent="cancelRenameSession"
                    @blur="commitRenameSession(s)"
                  />
                  <template v-else>
                    <span
                      class="tsr-title"
                      :title="`${s.title}（双击可重命名）`"
                      @dblclick.stop="startRenameSession(s, 'sidebar')"
                    >
                      {{ s.title }}
                    </span>
                    <div class="tsr-right">
                      <span
                        v-if="isSessionAsking(s.id)"
                        class="stream-grid-spinner"
                        title="正在流式检索生成中"
                      >
                        <i></i><i></i><i></i><i></i>
                      </span>
                      <span v-else class="tsc-time mono-num">
                        {{ formatRelativeTime(s.updated_at || s.created_at) }}
                      </span>
                      <div class="tsc-actions">
                        <button
                          type="button"
                          class="tsc-icon-btn"
                          title="重命名会话"
                          @click.stop="startRenameSession(s, 'sidebar')"
                        >
                          ✎
                        </button>
                        <button
                          type="button"
                          class="tsc-icon-btn"
                          title="置顶到 Pinned Conversations"
                          @click.stop="togglePinSession(s)"
                        >
                          📌
                        </button>
                        <button
                          type="button"
                          class="tsc-icon-btn is-del"
                          title="删除该会话"
                          :disabled="isSessionAsking(s.id)"
                          @click.stop="handleDeleteSession(s)"
                        >
                          ×
                        </button>
                      </div>
                    </div>
                  </template>
                </div>

                <div
                  v-if="grp.pinnedSessionsInGroup.length > 0 && grp.unpinnedSessions.length === 0"
                  class="proj-pinned-note"
                  title="点击定位到该项目置顶会话"
                  @click="handleJumpToFirstPinned(grp)"
                >
                  {{ grp.pinnedSessionsInGroup.length }} 个置顶会话
                </div>

                <div
                  v-else-if="grp.totalCount === 0"
                  class="proj-empty-note"
                  @click="grp.taskId > 0 && handleCreateSessionForTask(grp.taskId)"
                >
                  暂无会话 · 点击发起新追问
                </div>
              </div>
            </div>

            <el-empty
              v-if="projectGroups.length === 0"
              description="暂无匹配的项目或会话"
              :image-size="56"
            />
          </div>
        </div>
      </div>
    </aside>

    <!-- 右栏：单行紧凑标题栏 + 高屏占比独立并发问答工作台 -->
    <section class="qa-stage bg-panel">
      <div class="qst-header">
        <div class="qst-title-line">
          <h2>F7 · 标书专家多轮 ReAct 追问工作台</h2>
          <template v-if="activeSession">
            <input
              v-if="editingSessionId === activeSession.id && editingSource === 'header'"
              v-model="editingTitleDraft"
              type="text"
              class="sess-rename-input in-header"
              maxlength="256"
              @keydown.enter.prevent="commitRenameSession(activeSession)"
              @keydown.esc.prevent="cancelRenameSession"
              @blur="commitRenameSession(activeSession)"
            />
            <span
              v-else
              class="sess-tag mono-num"
              :title="`${activeSession.title}（双击可重命名）`"
              @dblclick="startRenameSession(activeSession, 'header')"
            >
              #{{ activeSession.id }} {{ activeSession.title }}
            </span>
            <button
              v-if="editingSessionId !== activeSession.id || editingSource !== 'header'"
              type="button"
              class="tsc-icon-btn header-rename-btn"
              title="重命名当前会话"
              @click="startRenameSession(activeSession, 'header')"
            >
              ✎
            </button>
          </template>
          <span v-else-if="selectedTask" class="sess-tag mono-num">
            当前项目：#{{ selectedTask.id }} {{ selectedTask.title }}（直接提问自动新建会话）
          </span>
        </div>
        <span
          class="qst-hint-pill"
          title="支持多项目多会话后台并发流式追问（切换会话绝不中断生成），并支持跨《招标文件》《我方投标书》及会话补充附件/图片调用 7 大工具；所有原文摘录强制通过 verify_quote 逐字校验"
        >
          ⓘ 多会话并发 · 7 工具溯源
        </span>
      </div>

      <div class="qst-body">
        <QaMessageList
          ref="msgListRef"
          :messages="messages"
          :sending="asking"
          :uploading-attachment="uploadingAttachment"
          :attachments="attachments"
          v-model:selected-attachment-ids="selectedAttachmentIds"
          :preset-questions="effectivePresetQuestions"
          :reasoning-effort="reasoningEffort"
          @send="handleSendQuestion"
          @upload-attachment="handleUploadAttachment"
          @delete-attachment="handleDeleteAttachment"
          @update:reasoning-effort="setReasoningEffort"
          @stop-stream="handleStopGeneration()"
          @withdraw-last="handleWithdrawLastTurn(true)"
          @regenerate="handleRegenerateLastTurn"
        />
      </div>
    </section>
  </div>
</template>

<style scoped>
.qa-chat-layout {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: 14px;
  height: calc(100vh - 76px);
  min-height: 580px;
}

.qa-sidebar {
  display: flex;
  flex-direction: column;
  padding: 12px 10px;
  height: 100%;
  overflow: hidden;
}

.qs-scroll {
  flex: 1;
  overflow-y: auto;
  padding-right: 2px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.qs-section {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.qs-section-hd {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 6px;
  gap: 6px;
}

.qs-sec-left {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.qs-section-title {
  font-size: 11.5px;
  font-weight: 800;
  color: var(--txt-muted);
  letter-spacing: 0.02em;
}

.qs-count-pill {
  font-size: 10.5px;
  font-weight: 800;
  color: var(--txt-sec);
  background: var(--bg-subtle);
  border: 1px solid var(--border-line);
  padding: 0 6px;
  border-radius: 99px;
}

.running-badge {
  font-size: 10.5px;
  font-weight: 800;
  color: var(--brand);
  background: var(--brand-soft);
  padding: 1px 7px;
  border-radius: 99px;
  white-space: nowrap;
}

.qs-sec-tools {
  display: flex;
  align-items: center;
  gap: 4px;
}

.qs-hdr-icon-btn {
  width: 24px;
  height: 24px;
  border-radius: 6px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--txt-sec);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all 0.14s;
}

.qs-hdr-icon-btn:hover:not(:disabled),
.qs-hdr-icon-btn.active {
  background: var(--bg-subtle);
  border-color: var(--border-line);
  color: var(--brand);
}

.qs-hdr-icon-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.qs-filter-box {
  padding: 6px 6px 2px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.only-active-chk {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--txt-sec);
  cursor: pointer;
  user-select: none;
}

.pinned-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.tree-sess-card {
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
  cursor: pointer;
  transition: all 0.14s;
}

.tree-sess-card:hover {
  border-color: var(--brand);
  background: #ffffff;
}

.tree-sess-card.active {
  border-color: var(--brand);
  background: var(--brand-soft);
  box-shadow: 0 0 0 1px rgba(29, 78, 216, 0.12);
}

.tsc-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.tsc-title {
  font-size: 12.5px;
  font-weight: 800;
  color: var(--txt-strong);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}

.tsc-proj {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-top: 3px;
  font-size: 11px;
  color: var(--txt-muted);
}

.tsc-proj-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.folder-glyph {
  font-size: 12px;
  flex-shrink: 0;
  opacity: 0.85;
}

.proj-tree-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.proj-node {
  display: flex;
  flex-direction: column;
}

.proj-row {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 6px 8px;
  border-radius: 7px;
  cursor: pointer;
  transition: background 0.14s;
  user-select: none;
}

.proj-row:hover {
  background: var(--bg-subtle);
}

.proj-row.selected {
  background: rgba(29, 78, 216, 0.06);
}

.proj-caret {
  font-size: 11px;
  color: var(--txt-muted);
  transition: transform 0.15s;
  width: 10px;
  text-align: center;
  flex-shrink: 0;
}

.proj-caret.open {
  transform: rotate(90deg);
}

.proj-name {
  flex: 1;
  min-width: 0;
  font-size: 12.5px;
  font-weight: 700;
  color: var(--txt-sec);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.proj-row.selected .proj-name {
  color: var(--txt-strong);
}

.proj-add-btn {
  width: 20px;
  height: 20px;
  border-radius: 5px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--txt-sec);
  font-size: 14px;
  font-weight: 800;
  line-height: 1;
  cursor: pointer;
  opacity: 0;
  transition: all 0.14s;
  flex-shrink: 0;
}

.proj-row:hover .proj-add-btn,
.proj-row.selected .proj-add-btn {
  opacity: 1;
}

.proj-add-btn:hover:not(:disabled) {
  background: #ffffff;
  border-color: var(--border-line);
  color: var(--brand);
}

.proj-children {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding-left: 18px;
  margin-top: 2px;
}

.tree-sess-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding: 6px 8px;
  border-radius: 7px;
  border: 1px solid transparent;
  cursor: pointer;
  transition: all 0.14s;
}

.tree-sess-row:hover {
  background: var(--bg-subtle);
}

.tree-sess-row.active {
  background: var(--brand-soft);
  border-color: rgba(29, 78, 216, 0.28);
}

.tsr-title {
  flex: 1;
  min-width: 0;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--txt-strong);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tree-sess-row.active .tsr-title {
  font-weight: 800;
  color: var(--brand);
}

.tsc-right,
.tsr-right {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.tsc-time {
  font-size: 11px;
  color: var(--txt-muted);
}

.tsc-actions {
  display: none;
  align-items: center;
  gap: 2px;
}

.tree-sess-card:hover .tsc-actions,
.tree-sess-row:hover .tsc-actions {
  display: inline-flex;
}

.tree-sess-card:hover .tsc-time,
.tree-sess-row:hover .tsc-time {
  display: none;
}

.tsc-icon-btn {
  border: none;
  background: transparent;
  padding: 0 3px;
  font-size: 11.5px;
  line-height: 1.2;
  color: var(--txt-sec);
  cursor: pointer;
  border-radius: 4px;
  opacity: 0.78;
}

.tsc-icon-btn:hover:not(:disabled) {
  opacity: 1;
  background: rgba(15, 23, 42, 0.08);
}

.tsc-icon-btn.is-pinned {
  opacity: 1;
}

.tsc-icon-btn.is-del {
  font-size: 14px;
  font-weight: 800;
  color: var(--sev-error);
}

.tsc-icon-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.sess-rename-input {
  flex: 1;
  min-width: 0;
  height: 22px;
  padding: 0 6px;
  border-radius: 5px;
  border: 1px solid var(--brand);
  background: #ffffff;
  color: var(--txt-strong);
  font-size: 12px;
  font-weight: 600;
  outline: none;
}

.sess-rename-input.in-header {
  max-width: 320px;
  height: 26px;
  font-size: 12.5px;
}

.header-rename-btn {
  flex-shrink: 0;
  font-size: 12.5px;
}

/* Antigravity 风格 2×2 呼吸并发运行徽标 */
.stream-grid-spinner {
  display: grid;
  grid-template-columns: repeat(2, 4px);
  gap: 2px;
  padding: 2px;
}

.stream-grid-spinner.mini {
  grid-template-columns: repeat(2, 3.5px);
  gap: 1.5px;
}

.stream-grid-spinner i {
  display: block;
  width: 4px;
  height: 4px;
  border-radius: 99px;
  background: var(--brand);
  animation: qaDotPulse 1s infinite ease-in-out;
}

.stream-grid-spinner.mini i {
  width: 3.5px;
  height: 3.5px;
}

.stream-grid-spinner i:nth-child(2) {
  animation-delay: 0.2s;
}
.stream-grid-spinner i:nth-child(3) {
  animation-delay: 0.4s;
}
.stream-grid-spinner i:nth-child(4) {
  animation-delay: 0.6s;
}

@keyframes qaDotPulse {
  0%, 100% {
    opacity: 0.3;
    transform: scale(0.85);
  }
  50% {
    opacity: 1;
    transform: scale(1.1);
  }
}

.proj-pinned-note,
.proj-empty-note {
  font-size: 11.5px;
  color: var(--txt-muted);
  padding: 4px 8px;
  font-style: italic;
  cursor: pointer;
}

.proj-pinned-note:hover,
.proj-empty-note:hover {
  color: var(--brand);
}

.qa-stage {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.qst-header {
  height: 44px;
  padding: 0 18px;
  border-bottom: 1px solid var(--border-line);
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
}

.qst-title-line {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.qst-header h2 {
  font-size: 15px;
  font-weight: 900;
  color: var(--txt-strong);
  white-space: nowrap;
}

.sess-tag {
  font-size: 12.5px;
  font-weight: 700;
  color: var(--brand);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.qst-hint-pill {
  font-size: 11.5px;
  font-weight: 700;
  color: var(--txt-sec);
  background: var(--bg-subtle);
  border: 1px solid var(--border-line);
  padding: 2px 9px;
  border-radius: 99px;
  cursor: help;
  white-space: nowrap;
  flex-shrink: 0;
}

.qst-body {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
</style>
