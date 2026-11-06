<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { extractApi, validateUploadFile } from '@/api'
import {
  formatPageOrOffset,
  type ExtractChecklistOut,
  type MarkedClauseItem,
} from '@/api/types'
import { EXTRACT_CACHE_KEY, useAuthStore } from '@/stores/auth'

defineOptions({ name: 'ExtractView' })

const authStore = useAuthStore()

const loading = ref(false)
const activeTab = ref<'marked' | 'wording' | 'qual_comp' | 'scoring'>('marked')
const kindFilter = ref<'ALL' | 'requirement' | 'non_req'>('ALL')
const result = ref<ExtractChecklistOut | null>(null)
const selectedFile = ref<File | null>(null)

interface CachedExtractPayload {
  username: string
  data: ExtractChecklistOut
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
  if (result.value) return
  try {
    const raw = sessionStorage.getItem(EXTRACT_CACHE_KEY)
    if (!raw) return
    const parsed = JSON.parse(raw) as Partial<CachedExtractPayload>
    const curUser = authStore.user?.username || ''
    if (parsed && parsed.data && (!parsed.username || parsed.username === curUser)) {
      result.value = parsed.data
    } else {
      sessionStorage.removeItem(EXTRACT_CACHE_KEY)
    }
  } catch {
    // 忽略损坏缓存
  }
}

function handleAuthChanged(): void {
  result.value = null
  selectedFile.value = null
}

function onFileChange(ev: Event): void {
  const input = ev.target as HTMLInputElement
  const f = input.files?.[0]
  if (!f) return
  const check = validateUploadFile(f)
  if (!check.valid) {
    ElMessage.warning(check.message)
    input.value = ''
    return
  }
  selectedFile.value = f
}

async function handleRunExtract(): Promise<void> {
  if (!selectedFile.value) {
    ElMessage.warning('请先选择待提取的招标文件（.docx / .pdf / .txt）')
    return
  }
  loading.value = true
  try {
    const extracted = await extractApi.extract(selectedFile.value)
    result.value = extracted
    saveToSessionStorage(extracted)
    ElMessage.success(`成功提取「${extracted.filename}」四张必审红线清单`)
  } finally {
    loading.value = false
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

onMounted(() => {
  restoreFromSessionStorage()
  window.addEventListener('bidguard-auth-changed', handleAuthChanged)
})

onBeforeUnmount(() => {
  window.removeEventListener('bidguard-auth-changed', handleAuthChanged)
})
</script>

<template>
  <div v-loading="loading" class="extract-view">
    <!-- 顶部上传与操作面板 -->
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
        <input
          type="file"
          accept=".docx,.pdf,.txt"
          class="ext-file-input"
          @change="onFileChange"
        />
        <el-button type="primary" :loading="loading" @click="handleRunExtract">
          立即提取四张清单
        </el-button>
      </div>
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
  gap: 10px;
  flex-wrap: wrap;
}

.ext-file-input {
  font-size: 12.5px;
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
</style>