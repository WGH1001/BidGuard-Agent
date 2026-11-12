<script lang="ts">
import { ref } from 'vue'
import type {
  BidTaskOut,
  QaAttachmentOut,
  QaAttachmentSummary,
  QaMessageItem,
  QaSessionOut,
} from '@/api/types'

/**
 * 模块级响应式缓存（绑定 cachedOwnerUsername，切换账号或登出时立即清零）
 */
const cachedOwnerUsername = ref<string>('')
const cachedTasks = ref<BidTaskOut[]>([])
const cachedSessions = ref<QaSessionOut[]>([])
const cachedSelectedTaskId = ref<number | null>(null)
const cachedActiveSessionId = ref<number | null>(null)
const cachedMessages = ref<QaMessageItem[]>([])
const cachedAttachments = ref<QaAttachmentOut[]>([])
const cachedSelectedAttachmentIds = ref<string[]>([])
const cachedAsking = ref<boolean>(false)
let initializedOnce = false
let cancelActiveStreamFn: (() => void) | null = null

function stopActiveQaStream(): void {
  if (cancelActiveStreamFn) {
    cancelActiveStreamFn()
    cancelActiveStreamFn = null
  }
}

function resetQaModuleCache(): void {
  stopActiveQaStream()
  cachedOwnerUsername.value = ''
  cachedTasks.value = []
  cachedSessions.value = []
  cachedSelectedTaskId.value = null
  cachedActiveSessionId.value = null
  cachedMessages.value = []
  cachedAttachments.value = []
  cachedSelectedAttachmentIds.value = []
  cachedAsking.value = false
  initializedOnce = false
}
</script>

<script setup lang="ts">
import type { ReasoningEffortLevel } from '@/api/types'
import { computed, onActivated, onBeforeUnmount, onMounted, ref as localRef } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { checksApi, qaApi } from '@/api'
import { useAppStore } from '@/stores/app'
import { useAuthStore } from '@/stores/auth'
import QaMessageList from '@/components/QaMessageList.vue'

defineOptions({ name: 'QaChatView' })

const appStore = useAppStore()
const authStore = useAuthStore()
const loadingInit = localRef(false)
const creatingSession = localRef(false)
const uploadingAttachment = localRef(false)
const msgListRef = localRef<InstanceType<typeof QaMessageList> | null>(null)

const tasks = cachedTasks
const sessions = cachedSessions
const selectedTaskId = cachedSelectedTaskId
const activeSessionId = cachedActiveSessionId
const messages = cachedMessages
const attachments = cachedAttachments
const selectedAttachmentIds = cachedSelectedAttachmentIds
const asking = cachedAsking

const PRESET_QUESTIONS = [
  '招标文件要求的免费质保期是多久？我方投标书在哪响应的？',
  '评分细则表里关于业绩和证书的加分项有哪些？',
  '本项目最高投标限价是多少？我方总报价是多少？',
  '招标文件中带 ★ 号的强制性技术要求有哪些？',
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

const activeSession = computed<QaSessionOut | null>(
  () => sessions.value.find((s) => s.id === activeSessionId.value) || null,
)

function ensureAccountIsolation(): boolean {
  const curUser = authStore.user?.username || ''
  if (cachedOwnerUsername.value && cachedOwnerUsername.value !== curUser) {
    resetQaModuleCache()
    cachedOwnerUsername.value = curUser
    return true
  }
  cachedOwnerUsername.value = curUser
  return false
}

function handleAuthChanged(): void {
  resetQaModuleCache()
}

async function loadTasksAndSessions(force = false): Promise<void> {
  const switched = ensureAccountIsolation()
  if (initializedOnce && !force && !switched && sessions.value.length > 0) {
    return
  }
  loadingInit.value = true
  try {
    const taskRes = await checksApi.list({ task_type: 'pre_check', page: 1, page_size: 50 })
    tasks.value = taskRes.items || []

    if (!selectedTaskId.value && tasks.value.length > 0) {
      selectedTaskId.value = appStore.activeTaskId || tasks.value[0].id
    }

    await refreshSessionsList()
    initializedOnce = true
  } finally {
    loadingInit.value = false
  }
}

async function refreshSessionsList(): Promise<void> {
  const list = await qaApi.listSessions(selectedTaskId.value ?? undefined)
  sessions.value = list || []
  if (sessions.value.length > 0) {
    const stillExists = sessions.value.some((s) => s.id === activeSessionId.value)
    if (!stillExists && !asking.value) {
      await selectSession(sessions.value[0])
    }
  } else if (!asking.value) {
    activeSessionId.value = null
    messages.value = []
    attachments.value = []
    selectedAttachmentIds.value = []
  }
}

async function selectSession(sess: QaSessionOut): Promise<void> {
  if (asking.value) return
  activeSessionId.value = sess.id
  const detail = await qaApi.getSession(sess.id)
  messages.value = [...(detail.messages_json || [])]
  attachments.value = [...(detail.attachments || [])]
  selectedAttachmentIds.value = attachments.value.map((a) => a.attachment_id)
}

async function handleCreateSession(): Promise<void> {
  if (asking.value || creatingSession.value) return
  if (!selectedTaskId.value) {
    ElMessage.warning('请先在上方下拉框选择一个关联的招投标体检任务')
    return
  }
  creatingSession.value = true
  try {
    const created = await qaApi.createSession({
      task_id: selectedTaskId.value,
    })
    sessions.value = [created, ...sessions.value]
    activeSessionId.value = created.id
    messages.value = []
    attachments.value = []
    selectedAttachmentIds.value = []
    ElMessage.success(`已新建问答会话 #${created.id}`)
  } finally {
    creatingSession.value = false
  }
}

async function handleDeleteSession(sess: QaSessionOut): Promise<void> {
  if (asking.value) return
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
  if (activeSessionId.value === sess.id) {
    if (sessions.value.length > 0) {
      await selectSession(sessions.value[0])
    } else {
      activeSessionId.value = null
      messages.value = []
      attachments.value = []
      selectedAttachmentIds.value = []
    }
  }
  ElMessage.success('问答会话已删除')
}

async function handleUploadAttachment(file: File): Promise<void> {
  if (asking.value || uploadingAttachment.value) return
  if (!activeSessionId.value) {
    if (!selectedTaskId.value) {
      ElMessage.warning('请先选择关联的投标体检任务后再上传附件')
      return
    }
    const created = await qaApi.createSession({ task_id: selectedTaskId.value })
    sessions.value = [created, ...sessions.value]
    activeSessionId.value = created.id
    messages.value = []
    attachments.value = []
    selectedAttachmentIds.value = []
  }
  const sid = activeSessionId.value
  if (!sid) return

  uploadingAttachment.value = true
  try {
    const uploaded = await qaApi.uploadAttachment(sid, file)
    attachments.value = [...attachments.value, uploaded]
    if (!selectedAttachmentIds.value.includes(uploaded.attachment_id)) {
      selectedAttachmentIds.value = [...selectedAttachmentIds.value, uploaded.attachment_id]
    }
    const desc = uploaded.is_image
      ? uploaded.has_text
        ? `OCR 识别 ${uploaded.ocr_chars} 字`
        : '纯图片（无文字）'
      : '文档解析就绪'
    ElMessage.success(`已上传附件「${uploaded.filename}」（${desc}）`)
  } finally {
    uploadingAttachment.value = false
  }
}

async function handleDeleteAttachment(att: QaAttachmentOut): Promise<void> {
  if (!activeSessionId.value || asking.value) return
  await qaApi.deleteAttachment(activeSessionId.value, att.attachment_id)
  attachments.value = attachments.value.filter((a) => a.attachment_id !== att.attachment_id)
  selectedAttachmentIds.value = selectedAttachmentIds.value.filter((id) => id !== att.attachment_id)
  ElMessage.success(`已移除附件「${att.filename}」`)
}

async function handleTaskChange(): Promise<void> {
  if (asking.value) return
  await refreshSessionsList()
}

async function handleSendQuestion(question: string, attachmentIds: string[] = []): Promise<void> {
  if (asking.value) {
    ElMessage.warning('当前有一条追问正在检索核验中，请等待返回后再提交')
    return
  }

  const pickedSummaries: QaAttachmentSummary[] = []
  const validIds: string[] = []
  let imgCount = 0
  for (const id of attachmentIds || []) {
    const matched = attachments.value.find((a) => a.attachment_id === id)
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

  if (!activeSessionId.value) {
    if (!selectedTaskId.value) {
      ElMessage.warning('请先选择关联的投标体检任务后再发起提问')
      return
    }
    const created = await qaApi.createSession({ task_id: selectedTaskId.value })
    sessions.value = [created, ...sessions.value]
    activeSessionId.value = created.id
    attachments.value = []
    selectedAttachmentIds.value = []
  }

  const targetSessionId = activeSessionId.value
  if (!targetSessionId) return

  messages.value.push({
    role: 'user',
    content: question,
    attachment_ids: validIds,
    attachments: pickedSummaries,
  })
  messages.value.push({
    role: 'assistant',
    content: '',
    steps: [],
    quotes: [],
    loading: true,
    streaming: true,
  })
  const assistantMsgIdx = messages.value.length - 1

  asking.value = true
  stopActiveQaStream()

  const payload = {
    question,
    max_rounds: 7,
    reasoning_effort: reasoningEffort.value,
    attachment_ids: validIds,
  }

  cancelActiveStreamFn = qaApi.askStream(targetSessionId, payload, {
    onStep(stepItem) {
      const msg = messages.value[assistantMsgIdx]
      if (!msg || msg.role !== 'assistant') return
      msg.steps = [...(msg.steps || []), stepItem]
      msgListRef.value?.scrollToBottom()
    },
    onDelta(deltaText) {
      const msg = messages.value[assistantMsgIdx]
      if (!msg || msg.role !== 'assistant') return
      msg.content = (msg.content || '') + deltaText
      msgListRef.value?.scrollToBottom()
    },
    async onDone(finalRes) {
      cancelActiveStreamFn = null
      const msg = messages.value[assistantMsgIdx]
      if (msg && msg.role === 'assistant') {
        messages.value[assistantMsgIdx] = {
          role: 'assistant',
          content: finalRes.answer,
          mode: finalRes.mode,
          quotes: finalRes.quotes || [],
          steps: finalRes.steps || msg.steps || [],
          loading: false,
          streaming: false,
        }
      }
      asking.value = false
      msgListRef.value?.scrollToBottom()
      const updatedList = await qaApi.listSessions(selectedTaskId.value ?? undefined)
      sessions.value = updatedList || []
    },
    async onError() {
      cancelActiveStreamFn = null
      // 流式连接失败时优雅降级调用同步 qaApi.ask 兜底
      try {
        const res = await qaApi.ask(targetSessionId, payload)
        if (messages.value[assistantMsgIdx]?.role === 'assistant') {
          messages.value[assistantMsgIdx] = {
            role: 'assistant',
            content: res.answer,
            mode: res.mode,
            quotes: res.quotes || [],
            steps: res.steps || [],
            loading: false,
            streaming: false,
          }
        }
        const updatedList = await qaApi.listSessions(selectedTaskId.value ?? undefined)
        sessions.value = updatedList || []
      } catch {
        if (messages.value[assistantMsgIdx]?.loading) {
          messages.value.splice(assistantMsgIdx, 1)
        }
      } finally {
        asking.value = false
      }
    },
  })
}

onMounted(() => {
  window.addEventListener('bidguard-auth-changed', handleAuthChanged)
  void loadTasksAndSessions(false)
})

onActivated(() => {
  const switched = ensureAccountIsolation()
  if (switched || (!asking.value && sessions.value.length === 0)) {
    void loadTasksAndSessions(true)
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('bidguard-auth-changed', handleAuthChanged)
  stopActiveQaStream()
})
</script>

<template>
  <div v-loading="loadingInit" class="qa-chat-layout">
    <!-- 左栏：任务绑定与会话侧栏 -->
    <aside class="qa-sidebar bg-panel">
      <div class="qs-top">
        <div class="qs-label">绑定招投标项目双书上下文</div>
        <el-select
          v-model="selectedTaskId"
          placeholder="选择已上传双书的体检任务"
          size="small"
          style="width: 100%"
          :disabled="asking"
          @change="handleTaskChange"
        >
          <el-option
            v-for="t in tasks"
            :key="t.id"
            :value="t.id"
            :label="`#${t.id} ${t.title}`"
          />
        </el-select>

        <el-button
          type="primary"
          size="small"
          class="new-sess-btn"
          :loading="creatingSession"
          :disabled="asking || !selectedTaskId"
          @click="handleCreateSession"
        >
          + 新建标书专家会话
        </el-button>
      </div>

      <div class="qs-list">
        <div
          v-for="s in sessions"
          :key="s.id"
          class="qs-item"
          :class="{ active: s.id === activeSessionId }"
          @click="selectSession(s)"
        >
          <div class="qsi-main">
            <div class="qsi-title" :title="s.title">{{ s.title }}</div>
            <div class="qsi-meta mono-num">
              会话 #{{ s.id }} · 任务 #{{ s.task_id ?? '—' }}
            </div>
          </div>
          <button
            type="button"
            class="qsi-del-btn"
            title="删除该会话"
            :disabled="asking"
            @click.stop="handleDeleteSession(s)"
          >
            删除
          </button>
        </div>

        <el-empty
          v-if="sessions.length === 0"
          description="暂无历史会话，点击上方按钮新建或直接右侧提问"
          :image-size="60"
        />
      </div>
    </aside>

    <!-- 右栏：单行紧凑标题栏 + 高屏占比问答消息流（三挡深度切换已下沉至底部发送按钮左侧） -->
    <section class="qa-stage bg-panel">
      <div class="qst-header">
        <div class="qst-title-line">
          <h2>F7 · 标书专家多轮 ReAct 追问工作台</h2>
          <span v-if="activeSession" class="sess-tag mono-num">
            #{{ activeSession.id }} {{ activeSession.title }}
          </span>
        </div>
        <span
          class="qst-hint-pill"
          title="支持跨《招标文件》《我方投标书》及会话补充附件/图片调用检索、章节定位、F2 报价沙盒、F3 证照效期、E4 法规库与 search_attachment 7 大工具；所有原文摘录强制通过 verify_quote 逐字校验"
        >
          ⓘ 7 工具联动 · 多文件/图片溯源
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
          :preset-questions="PRESET_QUESTIONS"
          :reasoning-effort="reasoningEffort"
          @send="handleSendQuestion"
          @upload-attachment="handleUploadAttachment"
          @delete-attachment="handleDeleteAttachment"
          @update:reasoning-effort="setReasoningEffort"
        />
      </div>
    </section>
  </div>
</template>

<style scoped>
.qa-chat-layout {
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 14px;
  height: calc(100vh - 76px);
  min-height: 580px;
}

.qa-sidebar {
  display: flex;
  flex-direction: column;
  padding: 14px;
  height: 100%;
  overflow: hidden;
}

.qs-top {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-line);
  margin-bottom: 10px;
}

.qs-label {
  font-size: 12px;
  font-weight: 800;
  color: var(--txt-strong);
}

.new-sess-btn {
  width: 100%;
  font-weight: 800;
}

.qs-list {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.qs-item {
  padding: 9px 11px;
  border-radius: 8px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  cursor: pointer;
  transition: all 0.15s;
}

.qs-item:hover {
  border-color: var(--brand);
  background: #ffffff;
}

.qs-item.active {
  border-color: var(--brand);
  background: var(--brand-soft);
}

.qsi-main {
  min-width: 0;
  flex: 1;
}

.qsi-title {
  font-size: 13px;
  font-weight: 800;
  color: var(--txt-strong);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.qsi-meta {
  font-size: 11px;
  color: var(--txt-muted);
  margin-top: 2px;
}

.qsi-del-btn {
  border: none;
  background: transparent;
  color: var(--sev-error);
  font-size: 11.5px;
  font-weight: 700;
  cursor: pointer;
  flex-shrink: 0;
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
