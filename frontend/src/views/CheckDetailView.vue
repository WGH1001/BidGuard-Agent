<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { checksApi, reportsApi } from '@/api'
import type {
  BidFindingOut,
  BidTaskOut,
  BidTaskStats,
  PriceManualBackfillOut,
} from '@/api/types'
import { useAppStore } from '@/stores/app'

import CheckTaskList from '@/components/CheckTaskList.vue'
import SseProgress from '@/components/SseProgress.vue'
import RiskCard from '@/components/RiskCard.vue'
import PriceManualDialog from '@/components/PriceManualDialog.vue'

defineOptions({ name: 'CheckDetailView' })

interface TaskDurationRecord {
  f1_ms?: number | null
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
const appStore = useAppStore()

const listLoading = ref(false)
const detailLoading = ref(false)
const runningTrigger = ref(false)
const tasks = ref<BidTaskOut[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(12)

const currentTask = ref<BidTaskOut | null>(null)
const findings = ref<BidFindingOut[]>([])
const priceModalVisible = ref(false)

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

const stageDurationItems = computed<StageDurationItem[]>(() => {
  const dur = currentTaskExt.value?.duration
  if (!dur) return []

  const rawStages = [
    { key: 'f1_ms', code: 'F1', label: '招标红线与★提取', ms: dur.f1_ms ?? null, color: '#2563eb' },
    { key: 'f2_ms', code: 'F2', label: '报价算术沙盒', ms: dur.f2_ms ?? null, color: '#0891b2' },
    { key: 'f3_ms', code: 'F3', label: '投标书解析与证照', ms: dur.f3_ms ?? null, color: '#059669' },
    { key: 'f4_ms', code: 'F4', label: '★逐条勾稽与矛盾', ms: dur.f4_ms ?? null, color: '#d97706' },
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

async function loadTaskList(selectFirst = false): Promise<void> {
  listLoading.value = true
  try {
    const res = await checksApi.list({
      task_type: 'pre_check',
      page: page.value,
      page_size: pageSize.value,
    })
    tasks.value = res.items || []
    total.value = res.total || 0

    if (selectFirst && tasks.value.length > 0 && !currentTask.value) {
      const preferred =
        tasks.value.find((t) => t.id === appStore.activeTaskId) || tasks.value[0]
      if (preferred) {
        await selectTask(preferred)
      }
    }
  } finally {
    listLoading.value = false
  }
}

async function selectTask(task: BidTaskOut): Promise<void> {
  appStore.setActiveTaskId(task.id)
  detailLoading.value = true
  try {
    const detail = await checksApi.detail(task.id)
    currentTask.value = detail.task
    findings.value = detail.findings || []
    checkAutoPopupPriceManual(detail.task)
  } finally {
    detailLoading.value = false
  }
}

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
  if (!currentTask.value) return
  runningTrigger.value = true
  try {
    const res = await checksApi.run(currentTask.value.id, false)
    currentTask.value.status = res.status
    sseRef.value?.startStream()
    ElMessage.info('已启动防废标核验流水线，正在实时接收 SSE 进度')
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
  ElMessage.success('封标前防废标核验已完成，已加载全量风险明细')
}

async function handleSseError(): Promise<void> {
  if (!currentTask.value) return
  await selectTask(currentTask.value)
  await loadTaskList(false)
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

function handlePageChange(newPage: number): void {
  page.value = newPage
  void loadTaskList(false)
}

async function handleDeleteTask(task: BidTaskOut): Promise<void> {
  if (['extracting', 'checking', 'grading'].includes(task.status)) {
    ElMessage.warning('任务正在运行中，请先等待完成')
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

  await checksApi.remove(task.id)
  ElMessage.success(`体检任务 #${task.id} 已删除`)

  if (currentTask.value?.id === task.id) {
    sseRef.value?.closeActiveStream()
    currentTask.value = null
    findings.value = []
    if (appStore.activeTaskId === task.id) {
      appStore.setActiveTaskId(null)
    }
    await loadTaskList(true)
  } else {
    await loadTaskList(false)
  }
}

onMounted(() => {
  void loadTaskList(true)
})
</script>

<template>
  <div class="check-workbench">
    <!-- 左栏：任务台账与新建弹窗 -->
    <CheckTaskList
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
              <span class="mono-num cwt-id">TASK #{{ currentTask.id }}</span>
              <span
                v-if="currentTask.stats_json?.llm_status"
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
                v-if="currentTask.stats_json?.price_status === 'NEED_MANUAL'"
                class="sev-pill warning"
              >
                报价表待人工回填
              </span>
              <span
                v-if="isTerminalTaskWithDuration"
                class="sev-pill info mono-num"
              >
                总耗时：{{ totalDurationLabel }}
              </span>
            </div>
            <h2 class="cwt-title">{{ currentTask.title }}</h2>
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
              type="danger"
              plain
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
          @done="handleSseDone"
          @error="handleSseError"
        />

        <!-- E11：五阶段耗时可观测性面板（仅在终态且有 duration 记录时展示，最慢阶段高亮） -->
        <section v-if="isTerminalTaskWithDuration" class="bg-panel dur-breakdown-card">
          <div class="dbc-head">
            <div class="dbc-title-row">
              <span class="dbc-title">核验流水线分阶段耗时透视</span>
              <span class="dbc-total mono-num">总计 {{ totalDurationLabel }}</span>
            </div>
            <div class="dbc-timestamps mono-num">
              <span>启动：{{ formatLocalDateTime(currentTaskExt?.started_at) }}</span>
              <span class="sep">→</span>
              <span>完成：{{ formatLocalDateTime(currentTaskExt?.finished_at) }}</span>
            </div>
          </div>

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
        </section>

        <!-- 风险卡列表 -->
        <RiskCard
          :task-id="currentTask.id"
          :findings="findings"
          @updated="handleFindingUpdated"
        />

        <!-- 报价回填弹窗（price_status == 'NEED_MANUAL' 时自动弹出） -->
        <PriceManualDialog
          v-model:visible="priceModalVisible"
          :task-id="currentTask.id"
          :initial-price-cap="currentTask.stats_json?.price_cap"
          :initial-bid-total="currentTask.stats_json?.bid_total"
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
  margin-bottom: 6px;
}

.cwt-id {
  font-size: 12px;
  font-weight: 900;
  color: var(--brand);
}

.cwt-title {
  font-size: 18px;
  font-weight: 900;
  color: var(--txt-strong);
  margin-bottom: 6px;
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
  gap: 8px;
  flex-wrap: wrap;
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
  grid-template-columns: repeat(5, 1fr);
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