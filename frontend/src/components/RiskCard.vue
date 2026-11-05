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
        <span class="rb-sub">按严重级排序（error 一票否决红线置顶）</span>
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

        <el-select v-model="moduleFilter" size="small" style="width: 140px">
          <el-option label="全部防线模块" value="ALL" />
          <el-option label="F2 报价算术沙盒" value="F2" />
          <el-option label="F3 证照与串名" value="F3" />
          <el-option label="F4 ★响应与矛盾" value="F4" />
          <el-option label="F6 控标雷达" value="F6" />
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
            {{ item.severity === 'error' ? 'ERROR · 废标红线' : item.severity === 'warning' ? 'WARN · 扣分预警' : 'INFO · 提示' }}
          </span>
          <span class="mod-badge mono-num">{{ item.source_module }}</span>
          <span class="code-badge mono-num">{{ item.code }}</span>
          <span v-if="item.clause_no" class="clause-chip">条款：{{ item.clause_no }}</span>
          <span class="sev-pill" :class="consequenceMeta(item.consequence).cls">
            {{ consequenceMeta(item.consequence).text }}
          </span>
          <span class="loc-tag mono-num">
            {{ formatPageOrOffset(item.page, item.quote_offset) }}
          </span>

          <span class="fb-status sev-pill" :class="feedbackMeta(item.feedback).cls">
            {{ feedbackMeta(item.feedback).text }}
          </span>
        </div>

        <div class="fc-message">{{ item.message }}</div>

        <!-- 双书对照展示（当 bid_quote 存在时并排展示招标要求 vs 我方响应，否则单栏展示原文摘录） -->
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