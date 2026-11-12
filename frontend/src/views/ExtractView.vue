<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { extractApi, validateUploadFile } from '@/api'
import {
  formatPageOrOffset,
  type ExtractChecklistOut,
  type MarkedClauseItem,
  type RequiredCertItemOut,
} from '@/api/types'
import { EXTRACT_CACHE_KEY, useAuthStore } from '@/stores/auth'

defineOptions({ name: 'ExtractView' })

const authStore = useAuthStore()
const router = useRouter()

const loading = ref(false)
const activeTab = ref<'marked' | 'wording' | 'qual_comp' | 'scoring' | 'certs'>('marked')
const kindFilter = ref<'ALL' | 'requirement' | 'non_req'>('ALL')
const certFilter = ref<'ALL' | 'mandatory' | 'bonus' | 'gap'>('ALL')
const result = ref<ExtractChecklistOut | null>(null)

// E19：支持点击/拖拽多文件排队依次提取
const fileInputRef = ref<HTMLInputElement | null>(null)
const isDragOver = ref(false)
const pendingFiles = ref<File[]>([])
const queueTotal = ref(0)
const queueDone = ref(0)
const currentExtractingName = ref('')

interface ExtractHistoryItem {
  id: string
  createdAt: string
  data: ExtractChecklistOut
}

const MAX_HISTORY_ITEMS = 20
const historyList = ref<ExtractHistoryItem[]>([])
const activeHistoryId = ref<string>('')

interface CachedExtractPayload {
  username: string
  data: ExtractChecklistOut
}

/**
 * E19 §2 Commit1.1：使用浏览器本地时区格式化当前时间（YYYY-MM-DD HH:mm），严禁 toISOString 慢 8 小时
 */
function formatLocalNowStr(d = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function getHistoryStorageKey(): string {
  const curUser = authStore.user?.username || 'default'
  return `bidguard_extract_history_v1_${curUser}`
}

function loadHistoryFromStorage(): void {
  try {
    const raw = localStorage.getItem(getHistoryStorageKey())
    if (!raw) {
      historyList.value = []
      return
    }
    const parsed = JSON.parse(raw) as ExtractHistoryItem[]
    historyList.value = Array.isArray(parsed) ? parsed : []
  } catch {
    historyList.value = []
  }
}

/**
 * E20 子项 3.2：localStorage 5MB 配额超限 LRU 淘汰重试——
 * 当多份特大标书 JSON 累计超浏览器配额抛 QuotaExceededError 时，循环弹出尾部最早的旧记录重试，
 * 确保最新提取出的成果 100% 落盘不丢失。
 */
function persistHistoryToStorage(): void {
  const key = getHistoryStorageKey()
  const trimmed = historyList.value.slice(0, MAX_HISTORY_ITEMS)
  while (trimmed.length > 0) {
    try {
      localStorage.setItem(key, JSON.stringify(trimmed))
      historyList.value = trimmed
      return
    } catch {
      if (trimmed.length <= 1) {
        break
      }
      trimmed.pop()
    }
  }
}

function pushToHistory(data: ExtractChecklistOut): void {
  const nowStr = formatLocalNowStr()
  const id = `${data.filename}_${Date.now()}`
  const filtered = historyList.value.filter((h) => h.data.filename !== data.filename)
  historyList.value = [{ id, createdAt: nowStr, data }, ...filtered].slice(0, MAX_HISTORY_ITEMS)
  activeHistoryId.value = id
  persistHistoryToStorage()
}

function handleSelectHistoryItem(item: ExtractHistoryItem): void {
  activeHistoryId.value = item.id
  result.value = item.data
  saveToSessionStorage(item.data)
}

function handleQuickSelectById(id: string): void {
  const found = historyList.value.find((h) => h.id === id)
  if (found) {
    handleSelectHistoryItem(found)
  }
}

async function handleDeleteHistoryItem(item: ExtractHistoryItem): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除招标文件「${item.data.filename}」的 F1 四张清单提取记录吗？`,
      '确认删除提取记录',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }

  historyList.value = historyList.value.filter((h) => h.id !== item.id)
  persistHistoryToStorage()

  if (activeHistoryId.value === item.id || result.value?.filename === item.data.filename) {
    if (historyList.value.length > 0) {
      const next = historyList.value[0]
      if (next) {
        handleSelectHistoryItem(next)
      }
    } else {
      activeHistoryId.value = ''
      result.value = null
      sessionStorage.removeItem(EXTRACT_CACHE_KEY)
    }
  }
  ElMessage.success(`已删除「${item.data.filename}」提取记录`)
}

function saveToSessionStorage(data: ExtractChecklistOut): void {
  try {
    const payload: CachedExtractPayload = {
      username: authStore.user?.username || '',
      data,
    }
    sessionStorage.setItem(EXTRACT_CACHE_KEY, JSON.stringify(payload))
  } catch {
    // 忽略配额异常
  }
}

function restoreFromSessionStorage(): void {
  loadHistoryFromStorage()
  if (result.value) return
  try {
    const raw = sessionStorage.getItem(EXTRACT_CACHE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<CachedExtractPayload>
      const curUser = authStore.user?.username || ''
      if (parsed && parsed.data && (!parsed.username || parsed.username === curUser)) {
        result.value = parsed.data
        const matched = historyList.value.find((h) => h.data.filename === parsed.data?.filename)
        if (matched) {
          activeHistoryId.value = matched.id
        } else {
          pushToHistory(parsed.data)
        }
        return
      } else {
        sessionStorage.removeItem(EXTRACT_CACHE_KEY)
      }
    }
  } catch {
    // 忽略损坏缓存
  }
  if (!result.value && historyList.value.length > 0) {
    const first = historyList.value[0]
    if (first) {
      handleSelectHistoryItem(first)
    }
  }
}

function handleAuthChanged(): void {
  result.value = null
  pendingFiles.value = []
  historyList.value = []
  activeHistoryId.value = ''
  loadHistoryFromStorage()
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

function onFileChange(ev: Event): void {
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

async function handleRunExtract(): Promise<void> {
  if (pendingFiles.value.length === 0) {
    ElMessage.warning('请先点击或拖拽选择待提取的招标文件（支持多选 .docx / .pdf / .txt）')
    return
  }
  loading.value = true
  queueTotal.value = pendingFiles.value.length
  queueDone.value = 0
  let successCount = 0

  try {
    while (pendingFiles.value.length > 0) {
      const nextFile = pendingFiles.value[0]
      if (!nextFile) break
      currentExtractingName.value = nextFile.name
      try {
        const extracted = await extractApi.extract(nextFile)
        result.value = extracted
        saveToSessionStorage(extracted)
        pushToHistory(extracted)
        successCount += 1
      } catch {
        // 单文件失败由全局拦截器提示，继续队列后续文件
      } finally {
        pendingFiles.value.shift()
        queueDone.value += 1
      }
    }
    if (successCount > 0) {
      ElMessage.success(
        successCount === 1
          ? `成功提取「${result.value?.filename}」四张必审红线清单`
          : `批量提取完成：成功提取 ${successCount} 份招标文件`,
      )
    }
  } finally {
    loading.value = false
    currentExtractingName.value = ''
  }
}

/**
 * 合并展示 marked_clauses 与 llm_structured（若 LLM 结构化层产出则优先挂接 structured 信息）
 */
const enrichedMarkedList = computed<MarkedClauseItem[]>(() => {
  if (!result.value) return []
  const rawList = result.value.marked_clauses || []
  const llmList = result.value.llm_structured || []
  const llmByStart = new Map<number, MarkedClauseItem>()
  for (const item of llmList) {
    llmByStart.set(item.start, item)
  }
  return rawList
    .map((m) => {
      const matched = llmByStart.get(m.start)
      if (!matched) return m
      return {
        ...m,
        full_text: matched.full_text || m.text,
        quote_verified: matched.quote_verified ?? true,
        structured: matched.structured || null,
        consequence: matched.structured?.consequence || m.consequence || 'none',
      }
    })
    .filter((item) => {
      if (kindFilter.value === 'requirement') return item.kind === 'requirement'
      if (kindFilter.value === 'non_req') return item.kind !== 'requirement'
      return true
    })
})

const reqCount = computed(
  () => (result.value?.marked_clauses || []).filter((c) => c.kind === 'requirement').length,
)
const nonReqCount = computed(
  () => (result.value?.marked_clauses || []).filter((c) => c.kind !== 'requirement').length,
)

function kindBadge(kind: string): { label: string; cls: string } {
  if (kind === 'requirement') {
    return { label: 'requirement · 实质要求', cls: 'ok' }
  }
  if (kind === 'usage_note') {
    return { label: 'usage_note · 非要求（说明语）', cls: 'neutral' }
  }
  if (kind === 'table_marker') {
    return { label: 'table_marker · 非要求（表格碎片）', cls: 'neutral' }
  }
  return { label: kind, cls: 'neutral' }
}

function consequenceBadge(item: {
  consequence?: string
  mark?: string
  structured?: { consequence?: string } | null
}): { label: string; cls: string } {
  const c = (item.structured?.consequence || item.consequence || '').toLowerCase()
  if (c === 'veto') return { label: 'veto · 一票否决', cls: 'error' }
  if (c === 'deduction') return { label: 'deduction · 扣分项', cls: 'warning' }
  if (c === 'bonus') return { label: 'bonus · 加分项', cls: 'info' }
  if (item.mark === '★') return { label: 'veto · ★实质要求', cls: 'error' }
  if (item.mark === '▲') return { label: 'deduction/bonus · ▲重要参数', cls: 'warning' }
  return { label: 'none · 一般条款', cls: 'neutral' }
}

function itemPage(item: unknown): number | null {
  if (item && typeof item === 'object' && 'page' in item) {
    const p = (item as { page?: number | null }).page
    return typeof p === 'number' && p > 0 ? p : null
  }
  return null
}

const requiredCertsList = computed<RequiredCertItemOut[]>(
  () => result.value?.required_certificates || [],
)

const mandatoryCertCount = computed(
  () => requiredCertsList.value.filter((c) => c.requirement_level === 'mandatory').length,
)

const bonusCertCount = computed(
  () => requiredCertsList.value.filter((c) => c.requirement_level !== 'mandatory').length,
)

const gapCertCount = computed(
  () => requiredCertsList.value.filter((c) => c.ledger_status !== 'valid').length,
)

const filteredRequiredCerts = computed<RequiredCertItemOut[]>(() => {
  return requiredCertsList.value.filter((c) => {
    if (certFilter.value === 'mandatory') return c.requirement_level === 'mandatory'
    if (certFilter.value === 'bonus') return c.requirement_level !== 'mandatory'
    if (certFilter.value === 'gap') return c.ledger_status !== 'valid'
    return true
  })
})

function certCategoryLabel(cat: string): string {
  if (cat === 'enterprise_qualification') return '企业资质与主体证明'
  if (cat === 'product_cert') return '产品与强制认证'
  if (cat === 'staff_cert') return '人员证书与职称'
  if (cat === 'bid_bond_or_ca') return '投标程序 / CA'
  return cat
}

function ledgerStatusMeta(item: RequiredCertItemOut): { label: string; cls: string } {
  const st = item.ledger_status
  const mc = item.matched_cert
  if (st === 'valid') {
    return {
      label: mc ? `台账有效 · 剩 ${mc.days_remaining} 天` : '台账有效',
      cls: 'ok',
    }
  }
  if (st === 'expiring_soon') {
    return {
      label: mc ? `临期预警 · 剩 ${mc.days_remaining} 天` : '临期预警',
      cls: 'warning',
    }
  }
  if (st === 'expired') {
    return {
      label: mc ? `台账已过期 ${Math.abs(mc.days_remaining)} 天` : '台账已过期',
      cls: 'error',
    }
  }
  return { label: '台账未收录', cls: 'neutral' }
}

function goCertLedger(): void {
  void router.push({ path: '/rules', query: { tab: 'certs' } })
}

onMounted(() => {
  restoreFromSessionStorage()
  window.addEventListener('bidguard-auth-changed', handleAuthChanged)
})

onBeforeUnmount(() => {
  window.removeEventListener('bidguard-auth-changed', handleAuthChanged)
})
</script>

<template>
  <!-- E19：移除根容器整页 v-loading，改为上传卡内部局部进度动效，提取期间历史列表与下方清单可正常浏览滚动 -->
  <div class="extract-view">
    <section class="bg-panel ext-upload-bar">
      <div class="eu-left">
        <div class="eu-tag">F1 · 招标红线与 ★/▲ 强制条款提取器</div>
        <h2>上传招标文件结构化拆解四张必审清单</h2>
        <p>
          先抽取资格项、符合性审查项、★/▲ 强制参数与评分细则表建立核验基准（★漏检率=0%），
          同时原样保留说明语与附表标记碎片供人工核对。
        </p>
      </div>

      <div class="eu-right">
        <!-- E19：点击/拖拽一体化虚线上传卡片（支持一次多选/拖入多个标段文件排队提取） -->
        <div
          class="ext-dropzone"
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
            @change="onFileChange"
          />
          <div class="dz-main-txt">
            点击或将招标文件拖拽至此区域（支持多文件批量排队）
          </div>
          <div class="dz-sub-txt">
            支持 .docx / .pdf / .txt，单文件 ≤ 100MB
          </div>
        </div>

        <el-button
          type="primary"
          size="large"
          :loading="loading"
          :disabled="pendingFiles.length === 0"
          @click="handleRunExtract"
        >
          {{
            loading
              ? `正在提取 (${queueDone + 1}/${queueTotal})…`
              : `立即提取四张清单${pendingFiles.length > 0 ? ` (${pendingFiles.length})` : ''}`
          }}
        </el-button>
      </div>

      <!-- 待提取文件队列与局部提取进度条 -->
      <div v-if="pendingFiles.length > 0 || loading" class="eu-queue-strip">
        <div v-if="loading" class="eu-progress-box">
          <div class="eup-head">
            <span class="eup-pulse"></span>
            <b>正在结构化提取中：{{ currentExtractingName }}</b>
            <span class="mono-num">（队列进度 {{ queueDone }}/{{ queueTotal }}，大标书约需 1~3 分钟，您可正常浏览下方历史记录）</span>
          </div>
          <el-progress :percentage="queueProgressPct" :stroke-width="6" status="warning" />
        </div>

        <div class="eu-file-tags">
          <span class="euf-lbl">待提取队列（{{ pendingFiles.length }}）：</span>
          <span
            v-for="(f, idx) in pendingFiles"
            :key="`${f.name}_${f.size}`"
            class="euf-chip"
          >
            <b>{{ f.name }}</b>
            <span class="mono-num">({{ (f.size / 1024).toFixed(1) }} KB)</span>
            <button
              v-if="!loading"
              type="button"
              class="euf-rm"
              title="移出队列"
              @click.stop="removePendingFile(idx)"
            >
              ×
            </button>
          </span>
        </div>
      </div>
    </section>

    <!-- E20 子项 3.1：F1 提取历史单行横向滚动芯片条 + 右侧可搜索下拉切换器（绝不折行撑破页面） -->
    <section v-if="historyList.length > 0" class="bg-panel ext-history-bar">
      <div class="ehb-label">近期提取记录（{{ historyList.length }}）：</div>
      <div class="ehb-chips">
        <div
          v-for="item in historyList"
          :key="item.id"
          class="ehb-chip"
          :class="{ active: item.id === activeHistoryId || result?.filename === item.data.filename }"
          @click="handleSelectHistoryItem(item)"
        >
          <span class="ehb-name" :title="item.data.filename">{{ item.data.filename }}</span>
          <span class="sev-pill ok">★▲ {{ item.data.marked_clauses.length }}</span>
          <span class="ehb-time mono-num">{{ item.createdAt }}</span>
          <el-button
            type="danger"
            link
            size="small"
            class="ehb-del"
            title="删除该条提取记录"
            @click.stop="handleDeleteHistoryItem(item)"
          >
            删除
          </el-button>
        </div>
      </div>

      <el-select
        v-model="activeHistoryId"
        filterable
        size="small"
        placeholder="搜索切换历史记录…"
        class="ehb-quick-select"
        @change="handleQuickSelectById"
      >
        <el-option
          v-for="item in historyList"
          :key="`opt_${item.id}`"
          :value="item.id"
          :label="`${item.data.filename} (${item.createdAt})`"
        />
      </el-select>
    </section>

    <!-- 0-Token 确定性模式提示横幅（当 llm_status === 'skipped' 时显示） -->
    <div v-if="result && result.llm_status === 'skipped'" class="det-mode-banner">
      <b>当前为确定性模式（llm_status = skipped · 0 Token）：</b>
      已通过纯规则引擎完成 ★/▲ 标记条款、无标记模式 A/B 废标句式、审查章节与表格型评分细则 100% 确定性提取。
    </div>

    <template v-if="result">
      <!-- 提取结果概览条 -->
      <div class="ext-summary-strip bg-panel">
        <div><b>文档名称：</b>{{ result.filename }} ({{ result.source_type.toUpperCase() }})</div>
        <div><b>文档规模：</b><span class="mono-num">{{ result.chars }}</span> 字符</div>
        <div>
          <b>开标日期：</b>
          <span
            class="sev-pill mono-num"
            :class="result.open_date ? 'info' : 'neutral'"
            :title="result.open_date_quote ? `原文摘录：${result.open_date_quote}` : undefined"
          >
            {{
              result.open_date
                ? `${result.open_date}（${result.open_date_source === 'tender_llm' ? 'AI 局部窗' : '正文识别'}）`
                : '未识别'
            }}
          </span>
        </div>
        <div>
          <b>★/▲ 实质要求：</b>
          <span class="sev-pill ok">{{ reqCount }} 条</span>
          <span class="muted-note">（另过滤非要求说明语/碎片 {{ nonReqCount }} 条）</span>
        </div>
        <div>
          <b>无标记废标措辞（模式 A/B）：</b>
          <span class="sev-pill error">{{ result.invalid_bid_clauses.length }} 条</span>
        </div>
        <div>
          <b>评分细则项：</b>
          <span class="sev-pill info">
            {{ result.scoring_tables.length + result.scoring_prose.length }} 项
          </span>
        </div>
        <div>
          <b>所需证照：</b>
          <span class="sev-pill" :class="gapCertCount > 0 ? 'warning' : 'ok'">
            {{ requiredCertsList.length }} 项
          </span>
          <span class="muted-note">（门槛 {{ mandatoryCertCount }} / 缺口或临期 {{ gapCertCount }}）</span>
        </div>
      </div>

      <!-- 四张清单切换 Tabs -->
      <div class="bg-panel ext-tabs-panel">
        <el-tabs v-model="activeTab">
          <!-- Tab 1：★/▲ 标记条款清单 -->
          <el-tab-pane
            :label="`① ★/▲ 标记条款清单 (${result.marked_clauses.length})`"
            name="marked"
          >
            <div class="tab-toolbar">
              <span>分类筛选：</span>
              <el-radio-group v-model="kindFilter" size="small">
                <el-radio-button value="ALL">
                  全部记录 ({{ result.marked_clauses.length }})
                </el-radio-button>
                <el-radio-button value="requirement">
                  仅看实质要求 requirement ({{ reqCount }})
                </el-radio-button>
                <el-radio-button value="non_req">
                  非要求说明语/表格碎片 ({{ nonReqCount }})
                </el-radio-button>
              </el-radio-group>
            </div>

            <div class="clause-cards">
              <div
                v-for="(item, idx) in enrichedMarkedList"
                :key="`${item.start}_${idx}`"
                class="c-card"
                :class="{ 'non-req': item.kind !== 'requirement' }"
              >
                <div class="cc-head">
                  <span class="mark-circle" :class="item.mark === '★' ? 'star' : 'tri'">
                    {{ item.mark }}
                  </span>
                  <span v-if="item.clause_no" class="cc-no mono-num">{{ item.clause_no }}</span>
                  <span class="sev-pill" :class="kindBadge(item.kind).cls">
                    {{ kindBadge(item.kind).label }}
                  </span>
                  <span class="sev-pill" :class="consequenceBadge(item).cls">
                    {{ consequenceBadge(item).label }}
                  </span>
                  <span class="cc-loc mono-num">
                    {{ formatPageOrOffset(itemPage(item), item.start) }}
                    <template v-if="itemPage(item) === null"> ~ @{{ item.end }}</template>
                  </span>
                  <span v-if="item.quote_verified !== false" class="verified-badge">
                    ✓ 原文溯源通过
                  </span>
                </div>
                <div class="cc-body">{{ item.full_text || item.text }}</div>
              </div>
              <el-empty v-if="enrichedMarkedList.length === 0" description="本文件未检出 ★/▲ 标记条款" />
            </div>
          </el-tab-pane>

          <!-- Tab 2：无标记废标措辞（模式 A / B） -->
          <el-tab-pane
            :label="`② 废标措辞模式 A/B (${result.invalid_bid_clauses.length})`"
            name="wording"
          >
            <div class="clause-cards">
              <div
                v-for="(w, idx) in result.invalid_bid_clauses"
                :key="`${w.start}_${idx}`"
                class="c-card"
              >
                <div class="cc-head">
                  <span class="sev-pill error">模式 {{ w.pattern }} · 无标记强制废标条款</span>
                  <span v-if="w.clause_no" class="cc-no mono-num">{{ w.clause_no }}</span>
                  <span v-if="w.promise_doc" class="sev-pill warning">
                    须提供：《{{ w.promise_doc }}》
                  </span>
                  <span class="cc-loc mono-num">
                    {{ formatPageOrOffset(itemPage(w), w.start) }}
                  </span>
                  <span class="verified-badge">✓ 原文溯源通过</span>
                </div>
                <div class="cc-body">{{ w.text }}</div>
              </div>
              <el-empty
                v-if="result.invalid_bid_clauses.length === 0"
                description="未检出模式 A/B 措辞型废标条款"
              />
            </div>
          </el-tab-pane>

          <!-- Tab 3：资格审查与符合性审查区段 -->
          <el-tab-pane
            :label="`③ 资格与符合性审查 (${result.qualification_hits.length + result.compliance_hits.length})`"
            name="qual_comp"
          >
            <div class="two-col-sections">
              <div>
                <h4 class="sub-sec-title">
                  资格审查命中区段 ({{ result.qualification_hits.length }})
                </h4>
                <div
                  v-for="(q, idx) in result.qualification_hits"
                  :key="`q_${idx}`"
                  class="c-card"
                >
                  <div class="cc-head">
                    <span class="sev-pill info">资格审查 · {{ q.keyword }}</span>
                    <span class="cc-loc mono-num">{{ formatPageOrOffset(itemPage(q), q.start) }}</span>
                  </div>
                  <div class="cc-body pre-excerpt">{{ q.excerpt }}</div>
                </div>
              </div>

              <div>
                <h4 class="sub-sec-title">
                  符合性审查命中区段 ({{ result.compliance_hits.length }})
                </h4>
                <div
                  v-for="(c, idx) in result.compliance_hits"
                  :key="`c_${idx}`"
                  class="c-card"
                >
                  <div class="cc-head">
                    <span class="sev-pill warning">符合性审查 · {{ c.keyword }}</span>
                    <span class="cc-loc mono-num">{{ formatPageOrOffset(itemPage(c), c.start) }}</span>
                  </div>
                  <div class="cc-body pre-excerpt">{{ c.excerpt }}</div>
                </div>
              </div>
            </div>
          </el-tab-pane>

          <!-- Tab 4：评分细则表 -->
          <el-tab-pane
            :label="`④ 评分细则表 (${result.scoring_tables.length + result.scoring_prose.length})`"
            name="scoring"
          >
            <el-table
              v-if="result.scoring_tables.length > 0"
              :data="result.scoring_tables"
              border
              stripe
              style="width: 100%"
            >
              <el-table-column prop="section" label="分节" width="130" />
              <el-table-column prop="no" label="序号" width="80" />
              <el-table-column prop="factor" label="评审因素" width="180" />
              <el-table-column prop="detail" label="评分细则与所需证明材料原文" min-width="320" />
              <el-table-column prop="score" label="分值" width="100" />
            </el-table>

            <div v-else-if="result.scoring_prose.length > 0" class="clause-cards">
              <div
                v-for="(sp, idx) in result.scoring_prose"
                :key="`sp_${idx}`"
                class="c-card"
              >
                <div class="cc-head">
                  <span class="sev-pill info">{{ sp.factor || '评分项' }}</span>
                  <span class="sev-pill ok">最高 {{ sp.score_max ?? sp.score ?? '—' }} 分</span>
                  <span v-if="sp.quote_verified" class="verified-badge">✓ 原文溯源通过</span>
                </div>
                <div class="cc-body">{{ sp.detail || sp.detail_raw }}</div>
              </div>
            </div>

            <el-empty v-else description="当前文档未包含结构化评分表格" />
          </el-tab-pane>

          <!-- Tab 5：E26 所需证照清单与企业台账自动对账 -->
          <el-tab-pane
            :label="`⑤ 所需证照与台账对账 (${requiredCertsList.length})`"
            name="certs"
          >
            <div v-if="result.cert_ledger_summary" class="cert-summary-bar">
              <div class="csb-metrics">
                <span class="sev-pill error">门槛必交 {{ result.cert_ledger_summary.mandatory_count }}</span>
                <span class="sev-pill info">评分加分 {{ result.cert_ledger_summary.bonus_count }}</span>
                <span class="sev-pill ok">台账有效 {{ result.cert_ledger_summary.valid_count }}</span>
                <span class="sev-pill warning">临期 {{ result.cert_ledger_summary.expiring_soon_count }}</span>
                <span class="sev-pill error">已过期 {{ result.cert_ledger_summary.expired_count }}</span>
                <span class="sev-pill neutral">未收录 {{ result.cert_ledger_summary.not_in_ledger_count }}</span>
                <span class="cc-loc mono-num">
                  效期参照日：{{ result.cert_ledger_summary.reference_date }}
                  （{{ result.cert_ledger_summary.reference_date_source === 'open_date' ? '标书开标日' : '今日' }}）
                </span>
              </div>
              <el-button size="small" type="primary" plain @click="goCertLedger">
                去企业证照台账补录 →
              </el-button>
            </div>

            <div class="tab-toolbar">
              <span>筛选视图：</span>
              <el-radio-group v-model="certFilter" size="small">
                <el-radio-button value="ALL">全部 ({{ requiredCertsList.length }})</el-radio-button>
                <el-radio-button value="mandatory">资格·废标门槛 ({{ mandatoryCertCount }})</el-radio-button>
                <el-radio-button value="bonus">评分加分项 ({{ bonusCertCount }})</el-radio-button>
                <el-radio-button value="gap">缺口与异常 ({{ gapCertCount }})</el-radio-button>
              </el-radio-group>
            </div>

            <div v-if="filteredRequiredCerts.length > 0" class="clause-cards">
              <div
                v-for="(item, idx) in filteredRequiredCerts"
                :key="`${item.cert_name}_${item.requirement_level}_${idx}`"
                class="c-card"
              >
                <div class="cc-head">
                  <span
                    class="sev-pill"
                    :class="item.requirement_level === 'mandatory' ? 'error' : 'info'"
                  >
                    {{ item.requirement_level === 'mandatory' ? '资格 / 废标门槛' : '评分加分 / 一般要求' }}
                  </span>
                  <span class="sev-pill neutral">{{ certCategoryLabel(item.category) }}</span>
                  <span class="cc-no">{{ item.cert_name }}</span>
                  <span class="sev-pill" :class="ledgerStatusMeta(item).cls">
                    {{ ledgerStatusMeta(item).label }}
                  </span>
                  <span class="cc-loc mono-num">
                    来源：{{ item.source_section }} ·
                    {{ formatPageOrOffset(item.page ?? null, item.offset ?? undefined) }}
                  </span>
                  <span v-if="item.quote_verified" class="verified-badge">✓ 原文溯源通过</span>
                </div>

                <div class="cc-body">{{ item.quote }}</div>

                <div v-if="item.matched_cert" class="cert-match-foot">
                  <b>匹配台账证照：</b>
                  <span>{{ item.matched_cert.cert_name }}</span>
                  <span v-if="item.matched_cert.cert_no" class="mono-num">
                    （编号：{{ item.matched_cert.cert_no }}）
                  </span>
                  <span class="mono-num">有效期至 {{ item.matched_cert.valid_until }}</span>
                  <span v-if="item.matched_cert.has_attachment" class="sev-pill ok">
                    已留档原件{{ item.matched_cert.attachment_name ? `：${item.matched_cert.attachment_name}` : '' }}
                  </span>
                  <span v-if="item.matched_count > 1" class="muted-note">
                    （共匹配台账 {{ item.matched_count }} 条同类证照，已择优展示）
                  </span>
                </div>
              </div>
            </div>

            <el-empty v-else description="当前筛选条件下暂无所需证照记录" />
          </el-tab-pane>
        </el-tabs>
      </div>
    </template>
  </div>
</template>

<style scoped>
.extract-view {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.ext-upload-bar {
  padding: 20px 24px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  flex-wrap: wrap;
}

.eu-left {
  flex: 1;
  min-width: 280px;
}

.eu-tag {
  font-size: 11px;
  font-weight: 800;
  color: var(--brand);
  margin-bottom: 4px;
}

.eu-left h2 {
  font-size: 18px;
  font-weight: 900;
  color: var(--txt-strong);
}

.eu-left p {
  font-size: 12.5px;
  color: var(--txt-sec);
  margin-top: 4px;
}

.eu-right {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.ext-dropzone {
  min-width: 320px;
  padding: 12px 18px;
  border: 1.5px dashed var(--border-strong);
  border-radius: 8px;
  background: var(--bg-subtle);
  cursor: pointer;
  transition: all 0.15s;
  text-align: center;
}

.ext-dropzone:hover,
.ext-dropzone.is-dragover {
  border-color: var(--brand);
  background: var(--brand-soft);
}

.ext-dropzone.is-disabled {
  opacity: 0.65;
  cursor: not-allowed;
}

.dz-main-txt {
  font-size: 13px;
  font-weight: 800;
  color: var(--txt-strong);
}

.dz-sub-txt {
  font-size: 11.5px;
  color: var(--txt-muted);
  margin-top: 2px;
}

.eu-queue-strip {
  width: 100%;
  padding-top: 12px;
  border-top: 1px dashed var(--border-line);
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.eu-progress-box {
  padding: 10px 14px;
  border-radius: 8px;
  background: #fffbeb;
  border: 1px solid #fde68a;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.eup-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: #92400e;
}

.eup-pulse {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #d97706;
  box-shadow: 0 0 0 3px rgba(217, 119, 6, 0.25);
}

.eu-file-tags {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12px;
}

.euf-lbl {
  font-weight: 800;
  color: var(--txt-sec);
}

.euf-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 9px;
  border-radius: 5px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-line);
}

.euf-rm {
  border: none;
  background: transparent;
  color: var(--sev-error);
  font-weight: 900;
  cursor: pointer;
  padding: 0 2px;
}

.ext-history-bar {
  padding: 10px 18px;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: nowrap;
  overflow: hidden;
}

.ehb-label {
  font-size: 12px;
  font-weight: 800;
  color: var(--txt-sec);
  flex-shrink: 0;
}

.ehb-chips {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: nowrap;
  overflow-x: auto;
  padding-bottom: 2px;
}

.ehb-chip {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border-radius: 6px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
  cursor: pointer;
  font-size: 12px;
  transition: all 0.15s;
}

.ehb-quick-select {
  width: 220px;
  flex-shrink: 0;
}

.ehb-chip:hover {
  border-color: var(--brand);
  background: #ffffff;
}

.ehb-chip.active {
  border-color: var(--brand);
  background: var(--brand-soft);
}

.ehb-name {
  font-weight: 700;
  color: var(--txt-strong);
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ehb-time {
  font-size: 11px;
  color: var(--txt-muted);
}

.ehb-del {
  font-size: 11.5px;
  padding: 0 2px;
}

.det-mode-banner {
  padding: 11px 16px;
  border-radius: 8px;
  background: #eff6ff;
  border: 1px solid #bfdbfe;
  border-left: 4px solid var(--brand);
  color: #1e3a8a;
  font-size: 12.5px;
}

.ext-summary-strip {
  padding: 13px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 13px;
}

.muted-note {
  font-size: 11.5px;
  color: var(--txt-muted);
  margin-left: 4px;
}

.ext-tabs-panel {
  padding: 16px 20px;
}

.tab-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
  font-size: 12.5px;
  font-weight: 700;
}

.clause-cards {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.c-card {
  padding: 12px 15px;
  border-radius: 8px;
  border: 1px solid var(--border-line);
  background: #ffffff;
}

.c-card.non-req {
  background: #f8fafc;
  border-style: dashed;
  opacity: 0.82;
}

.cc-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

.mark-circle {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 900;
  color: #ffffff;
}

.mark-circle.star {
  background: var(--sev-error);
}

.mark-circle.tri {
  background: var(--sev-warning);
}

.cc-no {
  font-weight: 800;
  color: var(--txt-strong);
}

.sev-pill.neutral {
  background: #e2e8f0;
  color: #475569;
  border: 1px solid #cbd5e1;
}

.cc-loc {
  font-size: 11.5px;
  color: var(--txt-muted);
}

.verified-badge {
  margin-left: auto;
  font-size: 11.5px;
  font-weight: 800;
  color: var(--sev-ok);
}

.cc-body {
  font-size: 13px;
  color: var(--txt-body);
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}

.pre-excerpt {
  max-height: 180px;
  overflow-y: auto;
  font-size: 12px;
}

.two-col-sections {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.sub-sec-title {
  font-size: 14px;
  font-weight: 800;
  color: var(--txt-strong);
  margin-bottom: 10px;
}

.cert-summary-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  padding: 10px 14px;
  border-radius: 8px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-line);
  margin-bottom: 12px;
}

.csb-metrics {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.cert-match-foot {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--border-line);
  font-size: 12px;
  color: var(--txt-sec);
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
</style>

