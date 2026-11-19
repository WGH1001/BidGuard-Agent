<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { checksApi } from '@/api'
import { formatPageOrOffset, type BidFindingOut, type FeedbackType } from '@/api/types'

const props = defineProps<{
  taskId: number
  findings: BidFindingOut[]
}>()

const emit = defineEmits<{
  (e: 'updated', finding: BidFindingOut): void
}>()

const severityFilter = ref<'ALL' | 'error' | 'warning' | 'info'>('ALL')
const moduleFilter = ref<string>('ALL')
const busyMap = ref<Record<number, boolean>>({})

const SEVERITY_ORDER: Record<string, number> = {
  error: 0,
  warning: 1,
  info: 2,
}

const sortedAndFiltered = computed<BidFindingOut[]>(() => {
  const list = props.findings.filter((f) => {
    if (severityFilter.value !== 'ALL' && f.severity !== severityFilter.value) {
      return false
    }
    if (moduleFilter.value !== 'ALL' && f.source_module !== moduleFilter.value) {
      return false
    }
    return true
  })
  return [...list].sort((a, b) => {
    const sa = SEVERITY_ORDER[a.severity] ?? 9
    const sb = SEVERITY_ORDER[b.severity] ?? 9
    if (sa !== sb) return sa - sb
    return a.id - b.id
  })
})

const errorCount = computed(() => props.findings.filter((f) => f.severity === 'error').length)
const warningCount = computed(() => props.findings.filter((f) => f.severity === 'warning').length)
const infoCount = computed(() => props.findings.filter((f) => f.severity === 'info').length)

function consequenceMeta(cons?: string | null): { text: string; cls: string } {
  const c = (cons || 'none').toLowerCase()
  if (c === 'veto') return { text: '★一票否决项', cls: 'error' }
  if (c === 'deduction') return { text: '▲重要扣分项', cls: 'warning' }
  if (c === 'bonus') return { text: '▲重要加分项', cls: 'info' }
  return { text: '一般核查项', cls: 'info' }
}

function feedbackMeta(fb: FeedbackType): { text: string; cls: string } {
  if (fb === 'accepted') return { text: '已采纳整改', cls: 'ok' }
  if (fb === 'rejected') return { text: '已驳回误报', cls: 'info' }
  return { text: '待确认', cls: 'warning' }
}

function moduleLabel(mod: string): string {
  const map: Record<string, string> = {
    F1: '红线提取',
    F2: '报价算术',
    F3: '证照与串名',
    F4: '双书勾稽',
    F5: '偏离制表',
    F6: '投前控标',
    BLIND_BID: '暗标合规',
  }
  return map[mod] || mod
}

function codeLabel(code: string): string {
  const map: Record<string, string> = {
    CONTRADICTION: '前后承诺矛盾',
    MISSING_RESPONSE: '★条款漏应答',
    NEGATIVE_DEVIATION: '实质性负偏离',
    NEED_MANUAL: '待人工复核',
    PRICE_MISMATCH: '报价算术异常',
    PRICE_SUM_MISMATCH: '分项合计不符',
    PRICE_UPPER_MISMATCH: '大小写金额不符',
    PRICE_CAP_EXCEEDED: '超最高限价',
    CERT_EXPIRED: '证照已过期',
    CERT_EXPIRING_SOON: '证照临期预警',
    SOCIAL_SECURITY_GAP: '社保证明不足',
    STALE_COMPANY_NAME: '疑似错用历史单位名',
    BLIND_BID_NEED_MANUAL: '暗标图文复核',
    BLIND_BID_IDENTITY_LEAK: '暗标身份泄露',
    BLIND_BID_STYLE_VIOLATION: '暗标排版违规',
    BLIND_BID_TOC_FORBIDDEN: '暗标违规目录',
    RADAR_EXCLUSIVE_PARAM: '疑似排他性参数',
  }
  return map[code] || code
}

function isDocxEstimatedPage(item: BidFindingOut): boolean {
  if (typeof item.page !== 'number' || item.page <= 0) return false
  const dj = item.detail_json
  if (!dj) return false
  return Boolean(
    dj.is_docx_page ||
      dj.bid_is_docx ||
      dj.tender_is_docx ||
      typeof dj.footer_page === 'number' ||
      typeof dj.bid_footer_page === 'number' ||
      typeof dj.tender_footer_page === 'number',
  )
}

function getFooterPage(item: BidFindingOut): number | null {
  const dj = item.detail_json
  if (!dj) return null
  if (typeof dj.footer_page === 'number' && dj.footer_page > 0) return dj.footer_page
  if (typeof dj.bid_footer_page === 'number' && dj.bid_footer_page > 0) return dj.bid_footer_page
  if (typeof dj.tender_footer_page === 'number' && dj.tender_footer_page > 0) return dj.tender_footer_page
  return null
}

function renderItemLocation(item: BidFindingOut): string {
  return formatPageOrOffset(item.page, item.quote_offset, {
    isDocx: isDocxEstimatedPage(item),
    footerPage: getFooterPage(item),
  })
}

async function submitFeedback(item: BidFindingOut, fb: FeedbackType): Promise<void> {
  if (busyMap.value[item.id]) return
  busyMap.value[item.id] = true
  try {
    const res = await checksApi.feedback(props.taskId, item.id, {
      feedback: fb,
      feedback_note: fb === 'accepted' ? '投标专员已确认采纳并整改' : fb === 'rejected' ? '人工复核判定不构成偏离' : '',
    })
    emit('updated', res)
    ElMessage.success(
      fb === 'accepted' ? '已标记为【采纳整改】' : fb === 'rejected' ? '已标记为【驳回误报】' : '已重置反馈状态',
    )
  } finally {
    busyMap.value[item.id] = false
  }
}
</script>

<template>
  <div class="risk-board bg-panel">
    <div class="rb-header">
      <div class="rb-title-group">
        <h3>封标前检出废标与偏离风险清单</h3>
        <span class="rb-sub">按严重级排序（一票否决红线置顶）</span>
      </div>

      <div class="rb-filters">
        <div class="pill-tabs">
          <button
            type="button"
            class="ptab"
            :class="{ active: severityFilter === 'ALL' }"
            @click="severityFilter = 'ALL'"
          >
            全部 ({{ props.findings.length }})
          </button>
          <button
            type="button"
            class="ptab err"
            :class="{ active: severityFilter === 'error' }"
            @click="severityFilter = 'error'"
          >
            一票否决 ({{ errorCount }})
          </button>
          <button
            type="button"
            class="ptab warn"
            :class="{ active: severityFilter === 'warning' }"
            @click="severityFilter = 'warning'"
          >
            扣分/临期预警 ({{ warningCount }})
          </button>
          <button
            type="button"
            class="ptab info"
            :class="{ active: severityFilter === 'info' }"
            @click="severityFilter = 'info'"
          >
            提示 ({{ infoCount }})
          </button>
        </div>

        <el-select v-model="moduleFilter" size="small" style="width: 148px">
          <el-option label="全部核验模块" value="ALL" />
          <el-option label="报价算术沙盒" value="F2" />
          <el-option label="证照与串名扫描" value="F3" />
          <el-option label="★响应与前后矛盾" value="F4" />
          <el-option label="暗标合规核查" value="BLIND_BID" />
          <el-option label="投前控标雷达" value="F6" />
        </el-select>
      </div>
    </div>

    <div class="rb-card-list">
      <div
        v-for="item in sortedAndFiltered"
        :key="item.id"
        class="f-card"
        :class="[item.severity, { 'is-rejected': item.feedback === 'rejected' }]"
      >
        <div class="fc-top">
          <span class="sev-pill" :class="item.severity">
            {{ item.severity === 'error' ? '废标红线' : item.severity === 'warning' ? '扣分预警' : '合规提示' }}
          </span>
          <span class="mod-badge">{{ moduleLabel(item.source_module) }}</span>
          <span class="code-badge">{{ codeLabel(item.code) }}</span>
          <span v-if="item.clause_no" class="clause-chip">条款：{{ item.clause_no }}</span>
          <span class="sev-pill" :class="consequenceMeta(item.consequence).cls">
            {{ consequenceMeta(item.consequence).text }}
          </span>
          <span class="loc-tag mono-num">
            {{ renderItemLocation(item) }}
          </span>
          <span v-if="isDocxEstimatedPage(item)" class="docx-est-hint">
            Word 页码为系统估算，可能与实际排版有出入，以字符偏移为准
          </span>

          <span class="fb-status sev-pill" :class="feedbackMeta(item.feedback).cls">
            {{ feedbackMeta(item.feedback).text }}
          </span>
        </div>

        <div class="fc-message">{{ item.message }}</div>

        <div v-if="item.quote && item.bid_quote" class="dual-quote-grid">
          <div class="dq-box tender-side">
            <div class="dq-label">招标文件要求 / 前文基准摘录</div>
            <div class="dq-text">“{{ item.quote }}”</div>
          </div>
          <div class="dq-box bid-side">
            <div class="dq-label">我方投标书响应 / 后文冲突摘录</div>
            <div class="dq-text">“{{ item.bid_quote }}”</div>
          </div>
        </div>
        <div v-else-if="item.quote || item.bid_quote" class="single-quote-box">
          <span class="sq-label">原文逐字溯源摘录：</span>
          <span class="sq-text">“{{ item.quote || item.bid_quote }}”</span>
          <span v-if="item.quote_verified" class="verified-tick">✓ 已通过原文溯源</span>
          <span v-else class="unverified-tick">未经溯源校验（规则引擎计算类风险）</span>
        </div>

        <div v-if="item.suggestion" class="fc-suggestion">
          <b>整改建议：</b>{{ item.suggestion }}
        </div>

        <div class="fc-foot">
          <div class="fc-btns">
            <el-button
              size="small"
              type="success"
              :plain="item.feedback !== 'accepted'"
              :loading="Boolean(busyMap[item.id])"
              @click="submitFeedback(item, 'accepted')"
            >
              采纳整改
            </el-button>
            <el-button
              size="small"
              type="info"
              :plain="item.feedback !== 'rejected'"
              :loading="Boolean(busyMap[item.id])"
              @click="submitFeedback(item, 'rejected')"
            >
              驳回误报
            </el-button>
            <el-button
              v-if="item.feedback !== 'pending'"
              size="small"
              link
              :loading="Boolean(busyMap[item.id])"
              @click="submitFeedback(item, 'pending')"
            >
              重置状态
            </el-button>
          </div>
        </div>
      </div>

      <el-empty
        v-if="sortedAndFiltered.length === 0"
        description="当前筛选条件下未检出废标或偏离风险项"
        :image-size="72"
      />
    </div>
  </div>
</template>

<style scoped>
.risk-board {
  padding: 18px 20px;
}

.rb-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border-line);
  margin-bottom: 14px;
}

.rb-title-group h3 {
  font-size: 15.5px;
  font-weight: 900;
  color: var(--txt-strong);
}

.rb-sub {
  font-size: 12px;
  color: var(--txt-muted);
}

.rb-filters {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.pill-tabs {
  display: flex;
  gap: 6px;
}

.ptab {
  padding: 4px 11px;
  border-radius: 6px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
  color: var(--txt-sec);
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}

.ptab.active {
  background: var(--txt-strong);
  color: #ffffff;
  border-color: var(--txt-strong);
}

.rb-card-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.f-card {
  padding: 14px 16px;
  border-radius: 9px;
  border: 1px solid var(--border-line);
  border-left: 4px solid var(--brand);
  background: #ffffff;
  transition: all 0.15s;
}

.f-card.error {
  border-left-color: var(--sev-error);
}

.f-card.warning {
  border-left-color: var(--sev-warning);
}

.f-card.is-rejected {
  opacity: 0.6;
  background: var(--bg-subtle);
}

.fc-top {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.mod-badge {
  background: #0f172a;
  color: #ffffff;
  font-size: 11px;
  font-weight: 800;
  padding: 2px 7px;
  border-radius: 4px;
}

.code-badge {
  background: var(--bg-subtle);
  border: 1px solid var(--border-line);
  color: var(--txt-sec);
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
}

.clause-chip {
  font-size: 12px;
  font-weight: 800;
  color: var(--brand);
  background: var(--brand-soft);
  padding: 2px 8px;
  border-radius: 4px;
}

.loc-tag {
  font-size: 11.5px;
  color: var(--txt-muted);
}

.docx-est-hint {
  font-size: 11px;
  color: #888780;
}

.fb-status {
  margin-left: auto;
}

.fc-message {
  font-size: 14px;
  font-weight: 800;
  color: var(--txt-strong);
  margin-bottom: 10px;
  line-height: 1.5;
}

.dual-quote-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin-bottom: 10px;
}

.dq-box {
  padding: 10px 12px;
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.55;
}

.dq-box.tender-side {
  background: #f8fafc;
  border: 1px solid #cbd5e1;
}

.dq-box.bid-side {
  background: #fef2f2;
  border: 1px solid #fecaca;
}

.dq-label {
  font-size: 11px;
  font-weight: 800;
  color: var(--txt-sec);
  margin-bottom: 4px;
}

.dq-text {
  color: var(--txt-strong);
  word-break: break-word;
}

.single-quote-box {
  padding: 9px 12px;
  background: var(--bg-subtle);
  border-left: 3px solid var(--border-strong);
  border-radius: 0 6px 6px 0;
  font-size: 12px;
  color: var(--txt-sec);
  margin-bottom: 10px;
}

.sq-label {
  font-weight: 800;
  color: var(--txt-strong);
}

.unverified-tick {
  color: var(--color-text-tertiary, #888780);
  font-size: 12px;
}
.verified-tick {
  margin-left: 8px;
  font-size: 11px;
  font-weight: 800;
  color: var(--sev-ok);
}

.fc-suggestion {
  padding: 8px 12px;
  background: var(--sev-ok-bg);
  border: 1px solid var(--sev-ok-border);
  border-radius: 6px;
  font-size: 12.5px;
  color: #065f46;
  margin-bottom: 10px;
}

.fc-foot {
  display: flex;
  justify-content: flex-end;
  padding-top: 8px;
  border-top: 1px dashed var(--border-line);
}

.fc-btns {
  display: flex;
  gap: 8px;
}
</style>