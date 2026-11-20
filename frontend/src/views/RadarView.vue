<script setup lang="ts">
import { computed, nextTick, onActivated, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { checksApi, radarApi, validateUploadFile } from '@/api'
import { formatPageOrOffset, type BidTaskOut, type RadarScanOut } from '@/api/types'
import { RADAR_CACHE_KEY, useAuthStore } from '@/stores/auth'
import { useNotificationStore } from '@/stores/notifications'

defineOptions({ name: 'RadarView' })

const route = useRoute()
const authStore = useAuthStore()
const notificationStore = useNotificationStore()
const radarFocusPulse = ref(false)
let radarFocusTimer: number | null = null
let lastHandledRadarSeq = 0

function scrollRadarDetailFocus(): void {
  if (radarFocusTimer !== null) {
    window.clearTimeout(radarFocusTimer)
    radarFocusTimer = null
  }
  radarFocusPulse.value = false
  void nextTick(() => {
    document.getElementById('bg-radar-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    radarFocusPulse.value = true
    radarFocusTimer = window.setTimeout(() => {
      radarFocusPulse.value = false
      radarFocusTimer = null
    }, 1700)
  })
}

const loading = ref(false)
const detailLoading = ref(false)
const listLoading = ref(false)
const rescanningId = ref<number | null>(null)
const cancellingRadar = ref(false)
const historyTasks = ref<BidTaskOut[]>([])
const scanResult = ref<RadarScanOut | null>(null)

let radarAbortController: AbortController | null = null
let radarStopRequested = false

// E20 子项 2：点击/拖拽一体化多文件排队扫描状态（移除易误导的旧任务名文本框）
const fileInputRef = ref<HTMLInputElement | null>(null)
const isDragOver = ref(false)
const pendingFiles = ref<File[]>([])
const queueTotal = ref(0)
const queueDone = ref(0)
const currentScanningName = ref('')

interface CachedRadarPayload {
  username: string
  data: RadarScanOut
}

/**
 * E20 子项 2.3：将后端返回的 naive UTC 时间字符串规范化补 'Z' 后转为浏览器本地时区 YYYY-MM-DD HH:mm
 */
function formatLocalCreatedAt(isoStr?: string | null): string {
  if (!isoStr) return '—'
  const raw = String(isoStr).trim()
  if (!raw) return '—'
  const normalized = raw.endsWith('Z') || raw.includes('+') ? raw : `${raw}Z`
  const dt = new Date(normalized)
  if (Number.isNaN(dt.getTime())) {
    return raw.slice(0, 16).replace('T', ' ')
  }
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())} ${pad(dt.getHours())}:${pad(dt.getMinutes())}`
}

function saveToSessionStorage(data: RadarScanOut): void {
  try {
    const payload: CachedRadarPayload = {
      username: authStore.user?.username || '',
      data,
    }
    sessionStorage.setItem(RADAR_CACHE_KEY, JSON.stringify(payload))
  } catch {
    // ignore storage errors
  }
}

function restoreFromSessionStorage(): void {
  if (scanResult.value) return
  try {
    const raw = sessionStorage.getItem(RADAR_CACHE_KEY)
    if (!raw) return
    const parsed = JSON.parse(raw) as Partial<CachedRadarPayload>
    const curUser = authStore.user?.username || ''
    if (parsed && parsed.data && (!parsed.username || parsed.username === curUser)) {
      scanResult.value = parsed.data
    } else {
      sessionStorage.removeItem(RADAR_CACHE_KEY)
    }
  } catch {
    // ignore
  }
}

async function loadHistoryList(autoSelectFirst = false): Promise<void> {
  listLoading.value = true
  try {
    const res = await radarApi.list()
    historyTasks.value = res.items || []
    if (
      scanResult.value &&
      historyTasks.value.length > 0 &&
      !historyTasks.value.some((t) => t.id === scanResult.value?.task_id)
    ) {
      scanResult.value = null
      sessionStorage.removeItem(RADAR_CACHE_KEY)
    } else if (scanResult.value && historyTasks.value.length === 0) {
      scanResult.value = null
      sessionStorage.removeItem(RADAR_CACHE_KEY)
    }
    if (autoSelectFirst && !scanResult.value && historyTasks.value.length > 0) {
      const first = historyTasks.value[0]
      if (first) {
        await handleSelectHistory(first)
      }
    }
  } finally {
    listLoading.value = false
  }
}

async function handleSelectHistory(task: BidTaskOut): Promise<void> {
  detailLoading.value = true
  try {
    const res = await radarApi.detail(task.id)
    scanResult.value = res
    saveToSessionStorage(res)
  } finally {
    detailLoading.value = false
  }
}

async function handleDeleteHistory(task: BidTaskOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除控标雷达扫描记录「#${task.id} ${task.title}」及其关联发现与上传标书文件吗？删除后不可恢复。`,
      '确认删除扫描记录',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }

  await radarApi.remove(task.id)
  ElMessage.success(`控标雷达记录 #${task.id} 已删除`)

  if (scanResult.value?.task_id === task.id) {
    scanResult.value = null
    sessionStorage.removeItem(RADAR_CACHE_KEY)
    await loadHistoryList(true)
  } else {
    await loadHistoryList(false)
  }
}

async function handleRescan(taskId: number): Promise<void> {
  if (rescanningId.value !== null) return
  rescanningId.value = taskId
  radarStopRequested = false
  radarAbortController = new AbortController()
  try {
    const res = await radarApi.rescan(taskId, radarAbortController.signal)
    scanResult.value = res
    saveToSessionStorage(res)
    await loadHistoryList(false)
    notificationStore.notifyTaskTerminal({
      kind: 'radar',
      targetId: res.task_id,
      title: res.title,
      summary: `重新扫描结论：${res.conclusion?.recommendation || '已完成'}（疑似参数 ${res.suspicious_params?.length || 0} 处）`,
      tone: res.conclusion?.recommendation === '疑似控标高' ? 'warning' : 'success',
      routePath: '/radar',
      routeQuery: { task_id: String(res.task_id) },
      dedupSuffix: `rescan:${Date.now()}`,
    })
  } catch {
    await loadHistoryList(false)
  } finally {
    radarAbortController = null
    rescanningId.value = null
  }
}

async function handleCancelRadarTask(taskId: number): Promise<void> {
  try {
    await checksApi.cancel(taskId)
    ElMessage.success(`控标雷达记录 #${taskId} 已中断`)
    await loadHistoryList(false)
  } catch {
    await loadHistoryList(false)
  }
}

async function handleStopRadar(): Promise<void> {
  cancellingRadar.value = true
  radarStopRequested = true
  pendingFiles.value = []
  try {
    if (rescanningId.value !== null) {
      await checksApi.cancel(rescanningId.value).catch(() => undefined)
    } else {
      const listRes = await radarApi.list().catch(() => null)
      const runningOnes = (listRes?.items || []).filter((t) =>
        ['extracting', 'checking', 'grading'].includes(t.status),
      )
      for (const rt of runningOnes) {
        await checksApi.cancel(rt.id).catch(() => undefined)
      }
    }
    radarAbortController?.abort()
    ElMessage.info('已发送控标雷达停止指令并清空待扫描队列')
    await loadHistoryList(false)
  } finally {
    cancellingRadar.value = false
    loading.value = false
    rescanningId.value = null
    currentScanningName.value = ''
  }
}

function handleAuthChanged(): void {
  scanResult.value = null
  historyTasks.value = []
  pendingFiles.value = []
}

function triggerFilePicker(): void {
  if (loading.value) return
  fileInputRef.value?.click()
}

function addFilesToQueue(fileList: FileList | File[]): void {
  const incoming = Array.from(fileList || [])
  if (incoming.length === 0) return
  const validOnes: File[] = []
  for (const f of incoming) {
    const check = validateUploadFile(f)
    if (!check.valid) {
      ElMessage.warning(check.message)
      continue
    }
    if (!pendingFiles.value.some((p) => p.name === f.name && p.size === f.size)) {
      validOnes.push(f)
    }
  }
  if (validOnes.length > 0) {
    pendingFiles.value = [...pendingFiles.value, ...validOnes]
  }
}

function onPickFile(ev: Event): void {
  const input = ev.target as HTMLInputElement
  if (input.files && input.files.length > 0) {
    addFilesToQueue(input.files)
  }
  input.value = ''
}

function onDropFiles(ev: DragEvent): void {
  isDragOver.value = false
  if (loading.value) return
  if (ev.dataTransfer?.files && ev.dataTransfer.files.length > 0) {
    addFilesToQueue(ev.dataTransfer.files)
  }
}

function removePendingFile(idx: number): void {
  if (loading.value) return
  pendingFiles.value.splice(idx, 1)
}

const queueProgressPct = computed<number>(() => {
  if (!loading.value || queueTotal.value <= 0) return 0
  return Math.min(95, Math.max(12, Math.round(((queueDone.value + 0.5) / queueTotal.value) * 100)))
})

async function handleRunRadar(): Promise<void> {
  if (pendingFiles.value.length === 0) {
    ElMessage.warning('请先点击或拖拽选择待扫描的招标文件（支持多选 .docx / .pdf / .txt）')
    return
  }
  loading.value = true
  queueTotal.value = pendingFiles.value.length
  queueDone.value = 0
  radarStopRequested = false
  let successCount = 0

  try {
    while (pendingFiles.value.length > 0 && !radarStopRequested) {
      const nextFile = pendingFiles.value[0]
      if (!nextFile) break
      currentScanningName.value = nextFile.name
      const autoTitle = `${nextFile.name.replace(/\.[^.]+$/, '')}-投前控标评估`
      radarAbortController = new AbortController()
      try {
        const res = await radarApi.scan(nextFile, autoTitle, radarAbortController.signal)
        scanResult.value = res
        saveToSessionStorage(res)
        successCount += 1
        await loadHistoryList(false)
      } catch {
        if (radarStopRequested) {
          break
        }
        // 单文件异常由全局拦截器提示，继续扫描队列后续文件
      } finally {
        radarAbortController = null
        pendingFiles.value.shift()
        queueDone.value += 1
      }
    }
    if (successCount > 0 && !radarStopRequested) {
      const latest = scanResult.value
      notificationStore.notifyTaskTerminal({
        kind: 'radar',
        targetId: latest?.task_id || Date.now(),
        title:
          successCount === 1
            ? latest?.title || '投前控标评估'
            : `批量控标评估（共 ${successCount} 份）`,
        summary:
          successCount === 1
            ? `结论：${latest?.conclusion?.recommendation || '已完成'}（疑似控标参数 ${latest?.suspicious_params?.length || 0} 处）`
            : `成功完成 ${successCount} 份招标文件的投前控标雷达评估`,
        tone: latest?.conclusion?.recommendation === '疑似控标高' ? 'warning' : 'success',
        routePath: '/radar',
        routeQuery: latest?.task_id ? { task_id: String(latest.task_id) } : undefined,
        dedupSuffix: `scan:${Date.now()}`,
      })
    }
  } finally {
    radarAbortController = null
    loading.value = false
    currentScanningName.value = ''
  }
}

function recommendationBannerCls(rec?: string): string {
  if (rec === '值得投') return 'rec-ok'
  if (rec === '疑似控标高') return 'rec-high'
  return 'rec-caution'
}

function recommendationPillCls(rec?: string): string {
  if (rec === '值得投') return 'ok'
  if (rec === '疑似控标高') return 'error'
  return 'warning'
}

function exclusiveTypeCls(exType: string): string {
  if (exType === '品牌绑定') return 'error'
  if (exType === '资质定向') return 'warning'
  if (exType === '业绩定向') return 'info'
  return 'warning'
}

async function focusRadarTaskTarget(rawTaskId: unknown): Promise<void> {
  if (route.path !== '/radar') return
  const taskId = Number(rawTaskId || 0)
  if (Number.isFinite(taskId) && taskId > 0) {
    try {
      const res = await radarApi.detail(taskId)
      scanResult.value = res
      saveToSessionStorage(res)
    } catch {
      // ignore missing/deleted record
    }
  }
  scrollRadarDetailFocus()
}

watch(
  () => route.query.task_id,
  (rawTaskId) => {
    if (Number(rawTaskId || 0) > 0) {
      void focusRadarTaskTarget(rawTaskId)
    }
  },
)

watch(
  () => notificationStore.focusRequest.seq,
  (seq) => {
    if (!seq || seq <= lastHandledRadarSeq || notificationStore.focusRequest.path !== '/radar') return
    lastHandledRadarSeq = seq
    const qId = notificationStore.focusRequest.query?.task_id
    void loadHistoryList(false).then(() => focusRadarTaskTarget(qId))
  },
)

onMounted(async () => {
  restoreFromSessionStorage()
  await loadHistoryList(true)
  if (Number(route.query.task_id || 0) > 0) {
    await focusRadarTaskTarget(route.query.task_id)
  }
  window.addEventListener('bidguard-auth-changed', handleAuthChanged)
})

onActivated(() => {
  const hasNewFocus =
    notificationStore.focusRequest.path === '/radar' &&
    notificationStore.focusRequest.seq > lastHandledRadarSeq
  if (hasNewFocus) {
    lastHandledRadarSeq = notificationStore.focusRequest.seq
  }
  const qTask = Number(
    route.query.task_id || (hasNewFocus ? notificationStore.focusRequest.query?.task_id : 0) || 0,
  )
  if (qTask > 0 && (hasNewFocus || scanResult.value?.task_id !== qTask)) {
    void loadHistoryList(false).then(() => focusRadarTaskTarget(qTask))
  }
})

onBeforeUnmount(() => {
  if (radarFocusTimer !== null) {
    window.clearTimeout(radarFocusTimer)
    radarFocusTimer = null
  }
  window.removeEventListener('bidguard-auth-changed', handleAuthChanged)
})
</script>

<template>
  <div class="radar-workbench">
    <!-- 左栏：F6 控标雷达扫描历史列表 -->
    <aside class="radar-rail bg-panel">
      <div class="rr-hd">
        <div>
          <div class="rr-title">控标雷达扫描历史</div>
          <div class="rr-sub">共 {{ historyTasks.length }} 条记录</div>
        </div>
        <el-button size="small" @click="loadHistoryList(false)">刷新</el-button>
      </div>

      <div v-loading="listLoading" class="rr-scroll">
        <div
          v-for="item in historyTasks"
          :key="item.id"
          class="rr-item"
          :class="{ active: scanResult?.task_id === item.id }"
          @click="handleSelectHistory(item)"
        >
          <div class="rri-row1">
            <span class="rri-id mono-num">#{{ item.id }}</span>
            <div class="rri-row1-right">
              <span
                class="sev-pill"
                :class="
                  item.status === 'checking'
                    ? 'info'
                    : item.status === 'failed'
                      ? 'error'
                      : recommendationPillCls(item.stats_json?.recommendation)
                "
              >
                {{
                  item.status === 'checking'
                    ? '扫描中'
                    : item.status === 'failed'
                      ? '已中断/失败'
                      : item.stats_json?.recommendation || '已扫描'
                }}
              </span>
              <el-button
                v-if="item.status === 'checking'"
                type="warning"
                link
                size="small"
                class="rri-del-btn"
                title="中断卡住的控标扫描任务"
                @click.stop="handleCancelRadarTask(item.id)"
              >
                中断
              </el-button>
              <el-button
                v-else
                type="primary"
                link
                size="small"
                class="rri-del-btn"
                :loading="rescanningId === item.id"
                :disabled="rescanningId !== null && rescanningId !== item.id"
                title="重新执行控标雷达扫描与开标日期提取"
                @click.stop="handleRescan(item.id)"
              >
                重扫
              </el-button>
              <el-button
                type="danger"
                link
                size="small"
                class="rri-del-btn"
                :disabled="item.status === 'checking'"
                title="删除该扫描记录"
                @click.stop="handleDeleteHistory(item)"
              >
                删除
              </el-button>
            </div>
          </div>

          <div class="rri-title" :title="item.title">{{ item.title }}</div>
          <div class="rri-file" :title="item.tender_filename || ''">
            招：{{ item.tender_filename || '—' }}
          </div>
          <div class="rri-meta">
            <span>疑似项 {{ item.stats_json?.suspicious_count ?? 0 }} 处</span>
            <span class="mono-num">{{ formatLocalCreatedAt(item.created_at) }}</span>
          </div>
        </div>

        <el-empty
          v-if="!listLoading && historyTasks.length === 0"
          description="暂无扫描历史，请在右侧上传招标书启动扫描"
          :image-size="60"
        />
      </div>
    </aside>

    <!-- 右栏：上传面板 + 扫描详情（E20：移除右栏整块 v-loading 锁死，改为上传卡内局部进度条） -->
    <div v-loading="detailLoading" class="radar-view">
      <!-- 顶部上传与扫描触发面板 -->
      <section class="bg-panel radar-upload-card">
        <div class="ru-left">
          <div class="ru-tag">投前控标雷达（立项决策辅助流）</div>
          <h2>刚拿到招标书？30 秒扫描疑似排他性控标参数</h2>
          <p>
            识别指定或参照特定品牌型号、原厂唯一授权函、特定软著全称绑定及毫米级非标尺寸；
            合法国产化/信创政策导向自动免检不误报。
          </p>
        </div>

        <div class="ru-controls">
          <div
            class="radar-dropzone"
            :class="{ 'is-dragover': isDragOver, 'is-disabled': loading }"
            @click="triggerFilePicker"
            @dragover.prevent="isDragOver = true"
            @dragleave.prevent="isDragOver = false"
            @drop.prevent="onDropFiles"
          >
            <input
              ref="fileInputRef"
              type="file"
              multiple
              accept=".docx,.pdf,.txt"
              style="display: none"
              @change="onPickFile"
            />
            <div class="rdz-main-txt">
              点击或将招标文件拖拽至此区域（支持多文件批量排队）
            </div>
            <div class="rdz-sub-txt">
              按文件名自动生成评估标题 · 支持 .docx / .pdf / .txt（≤100MB）
            </div>
          </div>

          <el-button
            type="primary"
            size="large"
            :loading="loading"
            :disabled="pendingFiles.length === 0"
            @click="handleRunRadar"
          >
            {{
              loading
                ? `正在扫描 (${queueDone + 1}/${queueTotal})…`
                : `启动控标雷达扫描${pendingFiles.length > 0 ? ` (${pendingFiles.length})` : ''}`
            }}
          </el-button>
          <el-button
            v-if="loading || rescanningId !== null"
            type="danger"
            size="large"
            plain
            :loading="cancellingRadar"
            @click="handleStopRadar"
          >
            ⏹ 停止扫描
          </el-button>
        </div>

        <!-- 待扫描队列与局部扫描进度动效 -->
        <div v-if="pendingFiles.length > 0 || loading" class="ru-queue-strip">
          <div v-if="loading" class="ru-progress-box">
            <div class="rup-head">
              <span class="rup-pulse"></span>
              <b>正在执行控标雷达扫描：{{ currentScanningName }}</b>
              <span class="mono-num">
                （队列进度 {{ queueDone }}/{{ queueTotal }}，扫描期间您可随时点击左侧历史记录查阅已扫结果）
              </span>
            </div>
            <el-progress :percentage="queueProgressPct" :stroke-width="6" status="warning" />
          </div>

          <div class="ru-file-tags">
            <span class="ruf-lbl">待扫描队列（{{ pendingFiles.length }}）：</span>
            <span
              v-for="(f, idx) in pendingFiles"
              :key="`${f.name}_${f.size}`"
              class="ruf-chip"
            >
              <b>{{ f.name }}</b>
              <span class="mono-num">({{ (f.size / 1024).toFixed(1) }} KB)</span>
              <button
                v-if="!loading"
                type="button"
                class="ruf-rm"
                title="移出队列"
                @click.stop="removePendingFile(idx)"
              >
                ×
              </button>
            </span>
          </div>
        </div>
      </section>

      <template v-if="scanResult">
        <!-- 结论横幅（强制带"疑似"标注，明确声明不替用户拍板） -->
        <section
          id="bg-radar-detail"
          class="conclusion-banner bg-panel"
          :class="[recommendationBannerCls(scanResult.conclusion.recommendation), { 'bg-focus-flash': radarFocusPulse }]"
        >
          <div class="cb-top">
            <div class="cb-badge-row">
              <span class="cb-rec-pill">
                #{{ scanResult.task_id }} {{ scanResult.title }} · 投前评估参考意见：{{
                  scanResult.conclusion.recommendation
                }}（疑似筛查）
              </span>
              <span class="sev-pill info">
                检出疑似排他参数 {{ scanResult.suspicious_params.length }} 处
              </span>
              <span
                class="sev-pill mono-num"
                :class="scanResult.open_date ? 'info' : 'neutral'"
                :title="scanResult.open_date_quote ? `原文摘录：${scanResult.open_date_quote}` : undefined"
              >
                {{
                  scanResult.open_date
                    ? `开标日期：${scanResult.open_date}（${scanResult.open_date_source === 'tender_llm' ? 'AI 局部窗' : '正文识别'}）`
                    : '开标日期：未识别'
                }}
              </span>
              <span class="sev-pill" :class="scanResult.llm_status === 'done' ? 'ok' : 'info'">
                {{ scanResult.llm_status === 'done' ? '确定性 + 大模型双层复核' : '确定性规则模式' }}
              </span>
              <el-button
                size="small"
                type="primary"
                plain
                :loading="rescanningId === scanResult.task_id"
                :disabled="rescanningId !== null && rescanningId !== scanResult.task_id"
                style="margin-left: auto"
                @click="handleRescan(scanResult.task_id)"
              >
                重新扫描
              </el-button>
            </div>
          </div>
          <div class="cb-summary">{{ scanResult.conclusion.summary }}</div>
          <div class="cb-disclaimer">
            合规边界说明：以上结论及下方参数均标注为“疑似”，仅作为投标立项与发起标前澄清提疑的参考线索，最终是否参标由业务负责人决策拍板。
          </div>
        </section>

        <!-- 可疑排他参数卡片流 -->
        <section class="bg-panel radar-list-panel">
          <div class="rl-hd">
            <h3>检出疑似排他性 / 倾向性控标参数明细（{{ scanResult.suspicious_params.length }} 处）</h3>
          </div>

          <div v-if="scanResult.suspicious_params.length > 0" class="param-cards">
            <div
              v-for="(item, idx) in scanResult.suspicious_params"
              :key="`${item.offset}_${idx}`"
              class="p-card"
            >
              <div class="pc-head">
                <span class="pc-seq mono-num">#{{ idx + 1 }}</span>
                <span class="sev-pill" :class="exclusiveTypeCls(item.exclusive_type)">
                  疑似{{ item.exclusive_type }}
                </span>
                <span class="pc-loc mono-num">
                  招标书 {{ formatPageOrOffset(item.page ?? null, item.offset) }}
                </span>
                <span v-if="item.quote_verified" class="pc-verified">✓ 原文逐字溯源</span>

                <div class="pc-conf-wrap">
                  <span class="conf-lbl">疑似置信度</span>
                  <div class="conf-bar-Track">
                    <el-progress
                      :percentage="Math.round((item.confidence || 0.8) * 100)"
                      :stroke-width="8"
                      color="#ea580c"
                    />
                  </div>
                </div>
              </div>

              <div class="pc-reason">
                <b>疑似排他研判理由：</b>{{ item.reason }}
              </div>

              <div class="pc-quote">
                <div class="pq-label">招标文件原文逐字摘录：</div>
                <div class="pq-body">“{{ item.quote }}”</div>
              </div>
            </div>
          </div>

          <el-empty
            v-else
            description="本次扫描未检出明显疑似排他性或品牌定向参数，招标参数整体开放合规"
            :image-size="80"
          />
        </section>
      </template>

      <div v-else class="bg-panel radar-empty">
        <el-empty description="请在上方点击或拖拽招标书启动控标雷达扫描，或在左侧点击历史记录回放" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.radar-workbench {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: 18px;
  align-items: start;
}

.radar-rail {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 110px);
  min-height: 580px;
  padding: 16px;
}

.rr-hd {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-line);
  margin-bottom: 12px;
}

.rr-title {
  font-size: 14.5px;
  font-weight: 900;
  color: var(--txt-strong);
}

.rr-sub {
  font-size: 11.5px;
  color: var(--txt-muted);
}

.rr-scroll {
  flex: 1;
  overflow-y: auto;
  padding-right: 4px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.rr-item {
  padding: 12px;
  border-radius: 8px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
  cursor: pointer;
  transition: all 0.15s;
}

.rr-item:hover {
  border-color: var(--brand);
  background: #ffffff;
}

.rr-item.active {
  border-color: var(--brand);
  background: var(--brand-soft);
  box-shadow: 0 0 0 2px rgba(29, 78, 216, 0.12);
}

.rri-row1 {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 5px;
}

.rri-id {
  font-size: 12px;
  font-weight: 800;
  color: var(--txt-muted);
}

.rri-row1-right {
  display: flex;
  align-items: center;
  gap: 6px;
}

.rri-del-btn {
  font-size: 11.5px;
  padding: 0 2px;
}

.rri-title {
  font-size: 13.5px;
  font-weight: 800;
  color: var(--txt-strong);
  margin-bottom: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rri-file {
  font-size: 11.5px;
  color: var(--txt-sec);
  margin-bottom: 6px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rri-meta {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--txt-muted);
}

.radar-view {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.radar-empty {
  padding: 60px 24px;
}

@media (max-width: 1100px) {
  .radar-workbench {
    grid-template-columns: 1fr;
  }
}

.radar-upload-card {
  padding: 20px 24px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 18px;
  flex-wrap: wrap;
}

.ru-left {
  flex: 1;
  min-width: 260px;
}

.ru-tag {
  font-size: 11px;
  font-weight: 800;
  color: var(--brand);
  margin-bottom: 4px;
}

.ru-left h2 {
  font-size: 18px;
  font-weight: 900;
  color: var(--txt-strong);
}

.ru-left p {
  font-size: 12.5px;
  color: var(--txt-sec);
  margin-top: 4px;
}

.ru-controls {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.radar-dropzone {
  min-width: 310px;
  padding: 11px 16px;
  border: 1.5px dashed var(--border-strong);
  border-radius: 8px;
  background: var(--bg-subtle);
  cursor: pointer;
  transition: all 0.15s;
  text-align: center;
}

.radar-dropzone:hover,
.radar-dropzone.is-dragover {
  border-color: var(--brand);
  background: var(--brand-soft);
}

.radar-dropzone.is-disabled {
  opacity: 0.65;
  cursor: not-allowed;
}

.rdz-main-txt {
  font-size: 13px;
  font-weight: 800;
  color: var(--txt-strong);
}

.rdz-sub-txt {
  font-size: 11px;
  color: var(--txt-muted);
  margin-top: 2px;
}

.ru-queue-strip {
  width: 100%;
  padding-top: 12px;
  border-top: 1px dashed var(--border-line);
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.ru-progress-box {
  padding: 10px 14px;
  border-radius: 8px;
  background: #fffbeb;
  border: 1px solid #fde68a;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.rup-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: #92400e;
}

.rup-pulse {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #d97706;
  box-shadow: 0 0 0 3px rgba(217, 119, 6, 0.25);
}

.ru-file-tags {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12px;
}

.ruf-lbl {
  font-weight: 800;
  color: var(--txt-sec);
}

.ruf-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 9px;
  border-radius: 5px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-line);
}

.ruf-rm {
  border: none;
  background: transparent;
  color: var(--sev-error);
  font-weight: 900;
  cursor: pointer;
  padding: 0 2px;
}

.conclusion-banner {
  padding: 20px 24px;
  border-left: 5px solid var(--sev-warning);
}

.conclusion-banner.rec-ok {
  border-left-color: var(--sev-ok);
  background: #f0fdf4;
}

.conclusion-banner.rec-caution {
  border-left-color: var(--sev-warning);
  background: #fffbeb;
}

.conclusion-banner.rec-high {
  border-left-color: var(--sev-error);
  background: #fef2f2;
}

.cb-badge-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.cb-rec-pill {
  font-size: 15px;
  font-weight: 900;
  color: var(--txt-strong);
}

.cb-summary {
  font-size: 14px;
  font-weight: 700;
  color: var(--txt-body);
  line-height: 1.6;
  margin-bottom: 8px;
}

.cb-disclaimer {
  font-size: 12px;
  color: var(--txt-muted);
}

.radar-list-panel {
  padding: 18px 22px;
}

.rl-hd {
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-line);
  margin-bottom: 14px;
}

.rl-hd h3 {
  font-size: 15px;
  font-weight: 900;
  color: var(--txt-strong);
}

.param-cards {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.p-card {
  padding: 14px 16px;
  border-radius: 9px;
  border: 1px solid var(--border-line);
  border-left: 4px solid var(--sev-warning);
  background: #ffffff;
}

.pc-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.pc-seq {
  font-weight: 900;
  color: var(--txt-strong);
}

.pc-loc {
  font-size: 12px;
  color: var(--txt-muted);
}

.pc-verified {
  font-size: 11.5px;
  font-weight: 800;
  color: var(--sev-ok);
}

.pc-conf-wrap {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}

.conf-lbl {
  font-size: 11.5px;
  font-weight: 700;
  color: var(--txt-sec);
}

.conf-bar-Track {
  width: 140px;
}

.pc-reason {
  font-size: 13.5px;
  color: var(--txt-strong);
  margin-bottom: 10px;
}

.pc-quote {
  padding: 10px 12px;
  background: var(--bg-subtle);
  border-radius: 6px;
  border: 1px solid var(--border-line);
}

.pq-label {
  font-size: 11px;
  font-weight: 800;
  color: var(--txt-muted);
  margin-bottom: 4px;
}

.pq-body {
  font-size: 12.5px;
  color: var(--txt-body);
  line-height: 1.6;
}
</style>
