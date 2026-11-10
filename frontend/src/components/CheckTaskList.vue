<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { checksApi, validateUploadFile } from '@/api'
import type { BidTaskOut } from '@/api/types'

const props = defineProps<{
  tasks: BidTaskOut[]
  total: number
  page: number
  pageSize: number
  selectedTaskId: number | null
  loading: boolean
}>()

const emit = defineEmits<{
  (e: 'select', task: BidTaskOut): void
  (e: 'page-change', page: number): void
  (e: 'created', task: BidTaskOut, autoRun: boolean): void
  (e: 'delete', task: BidTaskOut): void
  (e: 'refresh'): void
}>()

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

const dialogVisible = ref(false)
const creating = ref(false)
const tenderFile = ref<File | null>(null)
const bidFile = ref<File | null>(null)

const form = reactive({
  title: '',
  openDate: '',
  buyerName: '',
  autoRun: true,
})

function statusMeta(st: string): { label: string; cls: string } {
  const map: Record<string, { label: string; cls: string }> = {
    created: { label: '待启动', cls: 'info' },
    extracting: { label: 'F1提取中', cls: 'info' },
    checking: { label: '双轨核查', cls: 'warning' },
    grading: { label: '定级汇总', cls: 'warning' },
    delivered: { label: '已完成', cls: 'ok' },
    need_manual: { label: '待回填报价', cls: 'warning' },
    failed: { label: '执行失败', cls: 'error' },
  }
  return map[st] || { label: st, cls: 'info' }
}

/**
 * E11 §2.4：格式化毫秒耗时为易读文本（<1s 显示 0.8s，1~60s 显示 12.3s，>=60s 显示 2分18秒）
 */
function formatDurationMs(ms?: number | null): string {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0) {
    return ''
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

const nowTick = ref<number>(Date.now())
let tickTimer: ReturnType<typeof setInterval> | null = null

function parseNaiveUtcMs(isoStr?: string | null): number | null {
  if (!isoStr) return null
  const normalized = isoStr.endsWith('Z') || isoStr.includes('+') ? isoStr : `${isoStr}Z`
  const ms = new Date(normalized).getTime()
  return Number.isFinite(ms) ? ms : null
}

function formatLiveStopwatch(elapsedMs: number): string {
  const totalSec = Math.max(0, Math.floor(elapsedMs / 1000))
  const mins = String(Math.floor(totalSec / 60)).padStart(2, '0')
  const secs = String(totalSec % 60).padStart(2, '0')
  return `已耗时 ${mins}:${secs}`
}

/**
 * E19 §2 Commit1.4：在途任务（extracting/checking/grading）实时跳秒；终态任务回落显示 E11 真实总耗时
 */
function getTerminalDurationLabel(item: BidTaskOut): string {
  const ext = item as TaskWithDuration
  if (['extracting', 'checking', 'grading'].includes(item.status)) {
    const startMs = parseNaiveUtcMs(ext.started_at || ext.updated_at || ext.created_at)
    if (startMs !== null) {
      return formatLiveStopwatch(Math.max(0, nowTick.value - startMs))
    }
    return ''
  }
  if (!['delivered', 'failed', 'need_manual'].includes(item.status)) {
    return ''
  }
  const totalMs = ext.duration?.total_ms
  if (typeof totalMs === 'number' && totalMs >= 0) {
    return formatDurationMs(totalMs)
  }
  const sMs = parseNaiveUtcMs(ext.started_at)
  const fMs = parseNaiveUtcMs(ext.finished_at)
  if (sMs !== null && fMs !== null && fMs >= sMs) {
    return formatDurationMs(fMs - sMs)
  }
  return ''
}

onMounted(() => {
  tickTimer = setInterval(() => {
    nowTick.value = Date.now()
  }, 1000)
})

onBeforeUnmount(() => {
  if (tickTimer) {
    clearInterval(tickTimer)
    tickTimer = null
  }
})

function openCreateModal(): void {
  tenderFile.value = null
  bidFile.value = null
  form.title = ''
  form.openDate = ''
  form.buyerName = ''
  form.autoRun = true
  dialogVisible.value = true
}

function onSelectTenderFile(ev: Event): void {
  const input = ev.target as HTMLInputElement
  const f = input.files?.[0]
  if (!f) return
  const v = validateUploadFile(f)
  if (!v.valid) {
    ElMessage.warning(v.message)
    input.value = ''
    return
  }
  tenderFile.value = f
}

function onSelectBidFile(ev: Event): void {
  const input = ev.target as HTMLInputElement
  const f = input.files?.[0]
  if (!f) return
  const v = validateUploadFile(f)
  if (!v.valid) {
    ElMessage.warning(v.message)
    input.value = ''
    return
  }
  bidFile.value = f
  if (!form.title.trim()) {
    form.title = f.name.replace(/\.[^.]+$/, '')
  }
}

async function handleCreateSubmit(): Promise<void> {
  if (!tenderFile.value) {
    ElMessage.warning('请先选择招标文件（.docx / .pdf / .txt）')
    return
  }
  if (!bidFile.value) {
    ElMessage.warning('请先选择我方投标文件（.docx / .pdf / .txt）')
    return
  }
  creating.value = true
  try {
    const created = await checksApi.create({
      tenderFile: tenderFile.value,
      bidFile: bidFile.value,
      title: form.title,
      openDate: form.openDate,
      buyerName: form.buyerName,
      autoRun: false,
    })
    dialogVisible.value = false
    ElMessage.success(`防废标体检任务 #${created.id} 创建成功`)
    emit('created', created, form.autoRun)
  } finally {
    creating.value = false
  }
}

defineExpose({
  openCreateModal,
})
</script>

<template>
  <aside class="task-rail bg-panel">
    <div class="rail-hd">
      <div>
        <div class="rail-title">封标体检任务台账</div>
        <div class="rail-sub">共 {{ props.total }} 个项目</div>
      </div>
      <div class="rail-actions">
        <el-button size="small" @click="emit('refresh')">刷新</el-button>
        <el-button type="primary" size="small" @click="openCreateModal">+ 新建体检</el-button>
      </div>
    </div>

    <div v-loading="props.loading" class="rail-scroll">
      <div
        v-for="item in props.tasks"
        :key="item.id"
        class="rail-item"
        :class="{ active: item.id === props.selectedTaskId }"
        @click="emit('select', item)"
      >
        <div class="ri-row1">
          <span class="ri-id mono-num">#{{ item.id }}</span>
          <div class="ri-row1-right">
            <span
              v-if="getTerminalDurationLabel(item)"
              class="ri-dur-pill mono-num"
              title="本次体检任务总耗时"
            >
              ⏱ {{ getTerminalDurationLabel(item) }}
            </span>
            <span class="sev-pill" :class="statusMeta(item.status).cls">
              {{ statusMeta(item.status).label }}
            </span>
            <el-button
              type="danger"
              link
              size="small"
              class="ri-del-btn"
              :disabled="['extracting', 'checking', 'grading'].includes(item.status)"
              title="删除该体检任务"
              @click.stop="emit('delete', item)"
            >
              删除
            </el-button>
          </div>
        </div>

        <div class="ri-title" :title="item.title">{{ item.title }}</div>

        <div class="ri-files">
          <div>招：{{ item.tender_filename || '—' }}</div>
          <div>投：{{ item.bid_filename || '—' }}</div>
        </div>

        <div class="ri-prog-row">
          <el-progress
            :percentage="item.progress || 0"
            :stroke-width="5"
            :status="item.status === 'failed' ? 'exception' : item.status === 'delivered' ? 'success' : undefined"
          />
        </div>

        <div v-if="item.stats_json" class="ri-stats-bar">
          <span class="stat-chip err">否决 {{ item.stats_json.error_count ?? 0 }}</span>
          <span class="stat-chip warn">预警 {{ item.stats_json.warning_count ?? 0 }}</span>
          <span v-if="item.stats_json.price_status === 'NEED_MANUAL'" class="stat-chip manual">
            待回填报价
          </span>
        </div>
      </div>

      <el-empty
        v-if="!props.loading && props.tasks.length === 0"
        description="暂无体检任务，点击上方「+ 新建体检」上传双书"
        :image-size="64"
      />
    </div>

    <div v-if="props.total > props.pageSize" class="rail-pager">
      <el-pagination
        small
        background
        layout="prev, pager, next"
        :total="props.total"
        :page-size="props.pageSize"
        :current-page="props.page"
        @current-change="(p: number) => emit('page-change', p)"
      />
    </div>

    <!-- 新建防废标体检上传弹窗 -->
    <el-dialog
      v-model="dialogVisible"
      title="新建封标前防废标体检任务"
      width="560px"
      :close-on-click-modal="false"
      destroy-on-close
    >
      <div class="create-form">
        <div class="upload-box">
          <label class="ub-label">1. 上传招标文件（必传 · 支持 .docx / .pdf / .txt，≤100MB）</label>
          <input
            type="file"
            accept=".docx,.pdf,.txt"
            class="file-native-input"
            @change="onSelectTenderFile"
          />
          <div v-if="tenderFile" class="file-picked-tag">
            已选招标文件：<b>{{ tenderFile.name }}</b>
            <span class="mono-num">({{ (tenderFile.size / 1024).toFixed(1) }} KB)</span>
          </div>
        </div>

        <div class="upload-box">
          <label class="ub-label">2. 上传我方投标文件正本（必传 · 支持 .docx / .pdf / .txt，≤100MB）</label>
          <input
            type="file"
            accept=".docx,.pdf,.txt"
            class="file-native-input"
            @change="onSelectBidFile"
          />
          <div v-if="bidFile" class="file-picked-tag">
            已选投标文件：<b>{{ bidFile.name }}</b>
            <span class="mono-num">({{ (bidFile.size / 1024).toFixed(1) }} KB)</span>
          </div>
        </div>

        <el-form label-position="top">
          <el-form-item label="投标项目名称（可选，留空自动取投标文件名）">
            <el-input v-model.trim="form.title" placeholder="例如：某省政务云安全态势感知项目" />
          </el-form-item>

          <div class="two-cols">
            <el-form-item label="计划开标日期（用于 F3 证照有效期核验，留空则体检时自动从招标书识别）">
              <el-input v-model.trim="form.openDate" placeholder="留空自动识别，或填 YYYY-MM-DD" />
            </el-form-item>

            <el-form-item label="采购人/招标单位全称（防串名自伤）">
              <el-input v-model.trim="form.buyerName" placeholder="例如：某省大数据管理中心" />
            </el-form-item>
          </div>

          <el-form-item>
            <el-checkbox v-model="form.autoRun">
              上传完成后立即启动 F1~F4 防废标核验流水线并连接 SSE 实时进度（不勾选则仅创建任务，稍后可手动启动）
            </el-checkbox>
          </el-form-item>
        </el-form>
      </div>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="handleCreateSubmit">
          创建任务{{ form.autoRun ? '并开始体检' : '' }}
        </el-button>
      </template>
    </el-dialog>
  </aside>
</template>

<style scoped>
.task-rail {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 110px);
  min-height: 620px;
  padding: 16px;
}

.rail-hd {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-line);
  margin-bottom: 12px;
}

.rail-title {
  font-size: 14.5px;
  font-weight: 900;
  color: var(--txt-strong);
}

.rail-sub {
  font-size: 11.5px;
  color: var(--txt-muted);
}

.rail-actions {
  display: flex;
  gap: 6px;
}

.rail-scroll {
  flex: 1;
  overflow-y: auto;
  padding-right: 4px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.rail-item {
  padding: 12px 13px;
  border-radius: 8px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
  cursor: pointer;
  transition: all 0.15s;
}

.rail-item:hover {
  border-color: var(--brand);
  background: #ffffff;
}

.rail-item.active {
  border-color: var(--brand);
  background: var(--brand-soft);
  box-shadow: 0 0 0 2px rgba(29, 78, 216, 0.12);
}

.ri-row1 {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.ri-row1-right {
  display: flex;
  align-items: center;
  gap: 6px;
}

.ri-dur-pill {
  font-size: 11px;
  font-weight: 700;
  color: var(--txt-sec);
  background: #f1f5f9;
  border: 1px solid var(--border-line);
  padding: 1px 6px;
  border-radius: 4px;
}

.ri-del-btn {
  font-size: 11.5px;
  padding: 0 2px;
}

.ri-id {
  font-size: 12px;
  font-weight: 800;
  color: var(--txt-muted);
}

.ri-title {
  font-size: 13.5px;
  font-weight: 800;
  color: var(--txt-strong);
  margin-bottom: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ri-files {
  font-size: 11.5px;
  color: var(--txt-sec);
  line-height: 1.45;
  margin-bottom: 8px;
}

.ri-files div {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ri-prog-row {
  margin-bottom: 8px;
}

.ri-stats-bar {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.stat-chip {
  font-size: 11px;
  font-weight: 700;
  padding: 1px 7px;
  border-radius: 4px;
}

.stat-chip.err {
  background: var(--sev-error-bg);
  color: var(--sev-error);
}

.stat-chip.warn {
  background: var(--sev-warning-bg);
  color: var(--sev-warning);
}

.stat-chip.manual {
  background: #fef3c7;
  color: #b45309;
}

.rail-pager {
  padding-top: 10px;
  border-top: 1px solid var(--border-line);
  display: flex;
  justify-content: center;
}

.create-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.upload-box {
  padding: 12px 14px;
  background: var(--bg-subtle);
  border: 1px dashed var(--border-strong);
  border-radius: 8px;
}

.ub-label {
  display: block;
  font-size: 12.5px;
  font-weight: 800;
  color: var(--txt-strong);
  margin-bottom: 8px;
}

.file-native-input {
  font-size: 12.5px;
  width: 100%;
}

.file-picked-tag {
  margin-top: 6px;
  font-size: 12px;
  color: var(--sev-ok);
}

.two-cols {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
</style>