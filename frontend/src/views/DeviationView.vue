<script setup lang="ts">
import { computed, onActivated, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus/es/components/message/index'
import { checksApi, deviationApi } from '@/api'
import type { BidTaskOut, DeviationExportOut, DeviationRowItem } from '@/api/types'
import { useAppStore } from '@/stores/app'

defineOptions({ name: 'DeviationView' })

const route = useRoute()
const appStore = useAppStore()

type DeviationFilterKind = 'ALL' | 'no_deviation' | 'negative' | 'missing' | 'contradiction'

const loadingTasks = ref(false)
const loadingDeviation = ref(false)
const regenerating = ref(false)
const isBusy = computed(() => loadingDeviation.value || regenerating.value)
const downloading = ref(false)
const runningConflictMsg = ref('')
let loadSeq = 0

const tasks = ref<BidTaskOut[]>([])
const selectedTaskId = ref<number | null>(null)
const devResult = ref<DeviationExportOut | null>(null)
const statusFilter = ref<DeviationFilterKind>('ALL')

function isTaskInFlight(task?: BidTaskOut | null): boolean {
  return Boolean(task && ['extracting', 'checking', 'grading'].includes(task.status))
}

function extractErrCodeAndMsg(err: unknown): { code?: string; message?: string } {
  const detail = (err as { response?: { data?: { detail?: { code?: string; message?: string } | string } } })
    ?.response?.data?.detail
  if (detail && typeof detail === 'object') {
    return { code: detail.code, message: detail.message }
  }
  if (typeof detail === 'string') {
    return { message: detail }
  }
  return {}
}

/**
 * E20 子项 1：按选定种类过滤预览行（仅前端视图过滤，不影响 Word 导出全量行与 F4 后端契约）
 */
const filteredRows = computed<DeviationRowItem[]>(() => {
  const rows = devResult.value?.rows || []
  if (statusFilter.value === 'no_deviation') {
    return rows.filter((r) => !r.is_negative)
  }
  if (statusFilter.value === 'negative') {
    return rows.filter((r) => Boolean(r.is_negative))
  }
  if (statusFilter.value === 'missing') {
    return rows.filter((r) => r.status === 'MISSING_RESPONSE')
  }
  if (statusFilter.value === 'contradiction') {
    return rows.filter((r) => r.status === 'CONTRADICTION')
  }
  return rows
})

function setStatusFilter(kind: DeviationFilterKind): void {
  statusFilter.value = kind
}

async function loadAvailableTasks(): Promise<void> {
  loadingTasks.value = true
  try {
    const res = await checksApi.list({ task_type: 'pre_check', page: 1, page_size: 50 })
    tasks.value = res.items || []

    const queryTid = Number(route.query.task_id)
    if (queryTid && tasks.value.some((t) => t.id === queryTid)) {
      selectedTaskId.value = queryTid
    } else if (
      selectedTaskId.value &&
      tasks.value.some((t) => t.id === selectedTaskId.value)
    ) {
      // 保留当前用户在 F5 中已手动切换选中的任务
    } else if (appStore.activeTaskId && tasks.value.some((t) => t.id === appStore.activeTaskId)) {
      selectedTaskId.value = appStore.activeTaskId
    } else if (tasks.value.length > 0) {
      selectedTaskId.value = tasks.value[0].id
    }
  } finally {
    // E25 §2.1：列表拉取完毕立即释放 loadingTasks，绝不包裹 handleLoadDeviation 导致下拉框锁死
    loadingTasks.value = false
  }

  if (selectedTaskId.value) {
    appStore.setActiveTaskId(selectedTaskId.value)
    await handleLoadDeviation()
  }
}

async function handleTaskChange(): Promise<void> {
  if (selectedTaskId.value) {
    appStore.setActiveTaskId(selectedTaskId.value)
  }
  await handleLoadDeviation()
}

/** 进页面/切换任务：调 GET，产物已存在时后端秒回复用；先清旧表防跨包切换视觉残影 */
async function handleLoadDeviation(): Promise<void> {
  const tid = selectedTaskId.value
  if (!tid) return
  const seq = ++loadSeq
  runningConflictMsg.value = ''
  if (devResult.value && devResult.value.task_id !== tid) {
    devResult.value = null
  }
  loadingDeviation.value = true
  try {
    const res = await deviationApi.get(tid)
    if (seq !== loadSeq || selectedTaskId.value !== tid) return
    devResult.value = res
    statusFilter.value = 'ALL'
  } catch (err: unknown) {
    if (seq !== loadSeq || selectedTaskId.value !== tid) return
    const { code, message } = extractErrCodeAndMsg(err)
    const status = (err as { response?: { status?: number } })?.response?.status
    if (status === 409 || code === 'CHECK_TASK_RUNNING') {
      devResult.value = null
      runningConflictMsg.value = message || '该体检任务正在核验中，请等待完成后再查看或生成偏离表'
    }
  } finally {
    if (seq === loadSeq) {
      loadingDeviation.value = false
    }
  }
}

/** 仅"重新生成"按钮：强制全量重算 */
async function handleGenerateDeviation(): Promise<void> {
  const tid = selectedTaskId.value
  if (!tid) {
    ElMessage.warning('请先选择一个防废标体检任务')
    return
  }
  const seq = ++loadSeq
  runningConflictMsg.value = ''
  regenerating.value = true
  try {
    const res = await deviationApi.generate(tid)
    if (seq !== loadSeq || selectedTaskId.value !== tid) return
    devResult.value = res
    statusFilter.value = 'ALL'
    ElMessage.success(`偏离表生成完成：共对齐 ${res.total_rows} 条实质性条款`)
  } catch (err: unknown) {
    if (seq !== loadSeq || selectedTaskId.value !== tid) return
    const { code, message } = extractErrCodeAndMsg(err)
    const status = (err as { response?: { status?: number } })?.response?.status
    if (status === 409 || code === 'CHECK_TASK_RUNNING') {
      devResult.value = null
      runningConflictMsg.value = message || '该体检任务正在核验中，请等待完成后再查看或生成偏离表'
    }
  } finally {
    if (seq === loadSeq) {
      regenerating.value = false
    }
  }
}

async function handleDownloadDocx(): Promise<void> {
  if (!devResult.value) return
  downloading.value = true
  try {
    const fn = await deviationApi.downloadByUrl(
      devResult.value.download_url,
      devResult.value.output_filename || `技术与商务偏离表_${devResult.value.task_id}.docx`,
    )
    ElMessage.success(`已下载标准 Word 偏离表：${fn}`)
  } finally {
    downloading.value = false
  }
}

function rowClassName({ row }: { row: { is_negative?: boolean } }): string {
  return row.is_negative ? 'neg-deviation-row' : ''
}

onMounted(() => {
  void loadAvailableTasks()
})

onActivated(() => {
  const queryTid = Number(route.query.task_id)
  if (queryTid && tasks.value.some((t) => t.id === queryTid) && selectedTaskId.value !== queryTid) {
    selectedTaskId.value = queryTid
    appStore.setActiveTaskId(queryTid)
    void handleLoadDeviation()
    return
  }
  if (!selectedTaskId.value) {
    void loadAvailableTasks()
    return
  }
  void checksApi.list({ task_type: 'pre_check', page: 1, page_size: 50 }).then((res) => {
    tasks.value = res.items || []
  })
  if (!devResult.value || devResult.value.task_id !== selectedTaskId.value) {
    void handleLoadDeviation()
  }
})

watch(
  () => route.query.task_id,
  (rawTid) => {
    const queryTid = Number(rawTid)
    if (queryTid && tasks.value.some((t) => t.id === queryTid) && selectedTaskId.value !== queryTid) {
      selectedTaskId.value = queryTid
      appStore.setActiveTaskId(queryTid)
      void handleLoadDeviation()
    }
  },
)
</script>

<template>
  <div class="deviation-page">
    <!-- 顶部任务选择与操作条 -->
    <section class="bg-panel dev-header-bar">
      <div class="dh-left">
        <div class="dh-tag">《技术与商务偏离表.docx》一键对齐生成器</div>
        <h2>招标技术/商务规范与我方投标响应逐条对齐制表</h2>
        <p>
          严格消费双书勾稽三态核验结果（同序对位、零二次篡改），将人工 2 天制表时间压缩至 15 分钟内。
        </p>
      </div>

      <div class="dh-controls">
        <el-select
          v-model="selectedTaskId"
          :loading="loadingTasks"
          placeholder="选择已上传双书的体检任务"
          style="width: 340px"
          @change="handleTaskChange"
        >
          <el-option
            v-for="t in tasks"
            :key="t.id"
            :value="t.id"
            :label="`#${t.id} ${t.title}${isTaskInFlight(t) ? ' [核验中]' : ''}`"
          />
        </el-select>

        <el-button
          type="primary"
          :loading="regenerating"
          :disabled="loadingTasks || loadingDeviation"
          @click="handleGenerateDeviation"
        >
          重新生成偏离表
        </el-button>

        <el-button
          v-if="devResult"
          type="success"
          :loading="downloading"
          @click="handleDownloadDocx"
        >
          下载 Word 偏离表 (.docx)
        </el-button>
      </div>
    </section>

    <div v-loading="isBusy" class="deviation-body-wrap">
      <template v-if="devResult">
        <!-- 统计概览卡片（支持点击卡片直接联动筛选对应种类的偏离行） -->
      <div class="dev-stats-grid">
        <div
          class="ds-card bg-panel clickable-card"
          :class="{ 'is-active-filter': statusFilter === 'ALL' }"
          title="点击查看全部实质性条款"
          @click="setStatusFilter('ALL')"
        >
          <div class="ds-lbl">核验实质性要求总数</div>
          <div class="ds-val mono-num">{{ devResult.total_rows }}</div>
        </div>

        <div
          class="ds-card bg-panel ok-card clickable-card"
          :class="{ 'is-active-filter': statusFilter === 'no_deviation' }"
          title="点击仅看无偏离（正面响应满足）条款"
          @click="setStatusFilter('no_deviation')"
        >
          <div class="ds-lbl">无偏离（正面响应满足）</div>
          <div class="ds-val mono-num ok-txt">{{ devResult.no_deviation_count }}</div>
        </div>

        <div
          class="ds-card bg-panel err-card clickable-card"
          :class="{ 'is-active-filter': statusFilter === 'negative' }"
          title="点击仅看全部负偏离条款"
          @click="setStatusFilter('negative')"
        >
          <div class="ds-lbl">负偏离总数（高危废标/扣分）</div>
          <div class="ds-val mono-num err-txt">{{ devResult.negative_deviation_count }}</div>
        </div>

        <div
          class="ds-card bg-panel err-card clickable-card"
          :class="{ 'is-active-filter': statusFilter === 'missing' }"
          title="点击仅看实质性条款漏应答"
          @click="setStatusFilter('missing')"
        >
          <div class="ds-lbl">其中：实质性条款漏应答</div>
          <div class="ds-val mono-num err-txt">{{ devResult.missing_count }}</div>
        </div>

        <div
          class="ds-card bg-panel warn-card clickable-card"
          :class="{ 'is-active-filter': statusFilter === 'contradiction' }"
          title="点击仅看参数响应冲突条款"
          @click="setStatusFilter('contradiction')"
        >
          <div class="ds-lbl">其中：参数响应冲突</div>
          <div class="ds-val mono-num warn-txt">{{ devResult.contradiction_count }}</div>
        </div>
      </div>

      <!-- 标准六列偏离表在线预览 -->
      <section class="bg-panel dev-table-card">
        <div class="dt-hd">
          <h3>《技术与商务偏离表》六列标准预览</h3>
          <span class="dt-note">
            注：PDF 显示真实页码；Word 页码为系统估算，可能与实际排版有出入，以字符偏移为准。
          </span>
        </div>

        <!-- E20 子项 1：偏离种类多维分类筛选栏 -->
        <div class="dt-filter-bar">
          <span class="dtf-label">偏离种类筛选：</span>
          <el-radio-group v-model="statusFilter" size="small">
            <el-radio-button value="ALL">
              全部 ({{ devResult.rows.length }})
            </el-radio-button>
            <el-radio-button value="no_deviation">
              无偏离 ({{ devResult.no_deviation_count }})
            </el-radio-button>
            <el-radio-button value="negative">
              负偏离（全部）({{ devResult.negative_deviation_count }})
            </el-radio-button>
            <el-radio-button value="missing">
              漏应答 ({{ devResult.missing_count }})
            </el-radio-button>
            <el-radio-button value="contradiction">
              参数冲突 ({{ devResult.contradiction_count }})
            </el-radio-button>
          </el-radio-group>
          <span class="dtf-count mono-num">
            当前显示 {{ filteredRows.length }} / {{ devResult.rows.length }} 条
          </span>
        </div>

        <el-table
          :data="filteredRows"
          border
          :row-class-name="rowClassName"
          style="width: 100%"
        >
          <el-table-column prop="seq" label="序号" width="70" align="center" />
          <el-table-column prop="clause_no" label="招标条款号" width="120" />
          <el-table-column prop="tender_requirement" label="招标文件要求" min-width="260" />
          <el-table-column prop="bid_response" label="我方投标响应" min-width="240">
            <template #default="{ row }">
              <span :class="{ 'missing-placeholder': row.status === 'MISSING_RESPONSE' }">
                {{ row.bid_response }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="偏离情况" width="210">
            <template #default="{ row }">
              <span :class="row.is_negative ? 'dev-neg-text' : 'dev-ok-text'">
                {{ row.deviation }}
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="location" label="位置索引（页码/字符偏移）" width="250">
            <template #default="{ row }">
              <div class="mono-num loc-cell">{{ row.location }}</div>
            </template>
          </el-table-column>
        </el-table>
      </section>
      </template>

      <div v-else class="bg-panel dev-empty">
        <el-empty
          :description="
            runningConflictMsg
              ? `${runningConflictMsg}（您可随时切换上方下拉框查看其他已完成任务）`
              : isBusy
                ? '正在加载或生成标准偏离表，请稍候…'
                : '请选择一个防废标体检任务以生成并预览《技术与商务偏离表》'
          "
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.deviation-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.deviation-body-wrap {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: 220px;
}

.dev-header-bar {
  padding: 20px 24px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 18px;
  flex-wrap: wrap;
}

.dh-tag {
  font-size: 11px;
  font-weight: 800;
  color: var(--brand);
  margin-bottom: 4px;
}

.dh-left h2 {
  font-size: 18px;
  font-weight: 900;
  color: var(--txt-strong);
}

.dh-left p {
  font-size: 12.5px;
  color: var(--txt-sec);
  margin-top: 4px;
}

.dh-controls {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.dev-stats-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 14px;
}

.ds-card {
  padding: 16px 18px;
  border-top: 3px solid var(--brand);
  transition: all 0.15s ease;
}

.clickable-card {
  cursor: pointer;
}

.clickable-card:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(15, 23, 42, 0.06);
}

.ds-card.is-active-filter {
  box-shadow: 0 0 0 2px var(--brand);
}

.ds-card.ok-card {
  border-top-color: var(--sev-ok);
}

.ds-card.err-card {
  border-top-color: var(--sev-error);
  background: #fffafa;
}

.ds-card.warn-card {
  border-top-color: var(--sev-warning);
}

.ds-lbl {
  font-size: 12px;
  font-weight: 700;
  color: var(--txt-sec);
  margin-bottom: 6px;
}

.ds-val {
  font-size: 26px;
  font-weight: 900;
  color: var(--txt-strong);
}

.ds-val.ok-txt {
  color: var(--sev-ok);
}

.ds-val.err-txt {
  color: var(--sev-error);
}

.ds-val.warn-txt {
  color: var(--sev-warning);
}

.dev-table-card {
  padding: 18px 22px;
}

.dt-hd {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 12px;
}

.dt-hd h3 {
  font-size: 15px;
  font-weight: 900;
  color: var(--txt-strong);
}

.dt-note {
  font-size: 12px;
  color: var(--txt-muted);
}

.dt-filter-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 14px;
  padding: 10px 14px;
  border-radius: 8px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-line);
}

.dtf-label {
  font-size: 12.5px;
  font-weight: 800;
  color: var(--txt-strong);
}

.dtf-count {
  margin-left: auto;
  font-size: 12px;
  font-weight: 700;
  color: var(--txt-sec);
}

.dev-neg-text {
  color: var(--sev-error);
  font-weight: 900;
}

.dev-ok-text {
  color: var(--sev-ok);
  font-weight: 700;
}

.missing-placeholder {
  color: var(--sev-error);
  font-weight: 700;
}

.loc-cell {
  font-size: 12px;
  color: var(--txt-sec);
}

.docx-est-sub {
  font-size: 10.5px;
  color: #888780;
  margin-top: 2px;
  line-height: 1.35;
}

:deep(.neg-deviation-row) {
  background-color: #fef2f2 !important;
}

.dev-empty {
  padding: 64px 24px;
}
</style>
