<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { ReActStepTraceItem } from '@/api/types'

const props = withDefaults(
  defineProps<{
    steps: ReActStepTraceItem[]
    defaultOpen?: boolean
  }>(),
  {
    defaultOpen: false,
  },
)

const isOpen = ref<boolean>(props.defaultOpen)
const expandedObs = ref<Record<number, boolean>>({})

const roundCount = computed(() => {
  if (!props.steps || props.steps.length === 0) return 0
  const distinct = new Set(
    props.steps.map((s, idx) => s.round ?? s.step_no ?? idx + 1),
  )
  return distinct.size
})

// E21 §2.3：当 defaultOpen 为 true 且流式新步骤陆续到达时，确保自动展开并保持展开
watch(
  () => [props.defaultOpen, props.steps?.length],
  ([defOpen]) => {
    if (defOpen) {
      isOpen.value = true
    }
  },
  { immediate: true },
)

const TOOL_LABELS: Record<string, string> = {
  search_tender: '检索招标文件 (search_tender)',
  search_bid: '检索我方投标书 (search_bid)',
  locate_chapter: '定位标书章节 (locate_chapter)',
  recalc_price: 'F2 报价算术沙盒复算 (recalc_price)',
  check_cert_expiry: 'F3 证照效期与串名核验 (check_cert_expiry)',
  cite_regulation: '检索招投标法规库原文 (cite_regulation)',
  search_attachment: '检索会话补充附件 (search_attachment)',
  keyword_search: '0-Token 离线双书关键词检索 (keyword_search)',
  final: '汇总原文证据并输出结论 (final)',
}

function getActionLabel(action?: string): string {
  const key = (action || '').trim()
  return TOOL_LABELS[key] || key || '工具调用'
}

function stringifyPayload(val: unknown): string {
  if (val === null || val === undefined) return ''
  if (typeof val === 'string') return val
  try {
    return JSON.stringify(val, null, 2)
  } catch {
    return String(val)
  }
}

function getObsPreview(idx: number, obs: unknown): { text: string; isLong: boolean } {
  const full = stringifyPayload(obs)
  const isLong = full.length > 260
  if (!isLong || expandedObs.value[idx]) {
    return { text: full, isLong }
  }
  return { text: `${full.slice(0, 260)}…`, isLong }
}

function toggleObs(idx: number): void {
  expandedObs.value[idx] = !expandedObs.value[idx]
}
</script>

<template>
  <div v-if="props.steps && props.steps.length > 0" class="react-panel">
    <div class="rp-header" @click="isOpen = !isOpen">
      <div class="rp-title">
        <span class="rp-badge mono-num">检索轨迹</span>
        <span>
          跨双书工具检索与推理步骤（共 {{ roundCount }} 个思考回合 · {{ props.steps.length }} 步执行）
        </span>
      </div>
      <span class="rp-toggle">{{ isOpen ? '收起轨迹 ▲' : '展开轨迹 ▼' }}</span>
    </div>

    <div v-show="isOpen" class="rp-body">
      <div
        v-for="(step, idx) in props.steps"
        :key="idx"
        class="rp-step-item"
      >
        <div class="rs-head">
          <span class="rs-round mono-num">Round #{{ step.round ?? step.step_no ?? idx + 1 }}</span>
          <span
            v-if="step.batch_size && step.batch_size > 1"
            class="rs-batch-pill mono-num"
          >
            并行 {{ step.batch_index ?? 1 }}/{{ step.batch_size }}
          </span>
          <span class="rs-action mono-num">{{ getActionLabel(step.action) }}</span>
        </div>

        <div v-if="step.thought" class="rs-thought">
          <b>思考 (Thought)：</b>{{ step.thought }}
        </div>

        <div v-if="step.action_input !== undefined && step.action !== 'final'" class="rs-input">
          <b>调用参数 (Input)：</b>
          <code class="mono-num">{{ stringifyPayload(step.action_input) }}</code>
        </div>

        <div v-if="step.observation !== undefined && step.observation !== null" class="rs-obs">
          <div class="obs-label">工具返回结果 (Observation)：</div>
          <pre class="obs-pre mono-num">{{ getObsPreview(idx, step.observation).text }}</pre>
          <button
            v-if="getObsPreview(idx, step.observation).isLong"
            type="button"
            class="obs-more-btn"
            @click.stop="toggleObs(idx)"
          >
            {{ expandedObs[idx] ? '收起完整输出 ▲' : '展开完整输出 ▼' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.react-panel {
  margin-top: 12px;
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  background: var(--bg-subtle);
  overflow: hidden;
  font-size: 12px;
}

.rp-header {
  padding: 8px 12px;
  background: #e2e8f0;
  color: var(--txt-strong);
  display: flex;
  align-items: center;
  justify-content: space-between;
  cursor: pointer;
  user-select: none;
  font-weight: 700;
}

.rp-title {
  display: flex;
  align-items: center;
  gap: 8px;
}

.rp-badge {
  background: var(--brand);
  color: #ffffff;
  font-size: 10.5px;
  font-weight: 800;
  padding: 1px 7px;
  border-radius: 4px;
}

.rp-toggle {
  font-size: 11px;
  color: var(--brand);
  font-weight: 800;
}

.rp-body {
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.rp-step-item {
  padding: 10px 12px;
  background: #ffffff;
  border: 1px solid var(--border-line);
  border-left: 3px solid var(--brand);
  border-radius: 6px;
}

.rs-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.rs-round {
  font-weight: 900;
  color: var(--brand);
  font-size: 11.5px;
}

.rs-batch-pill {
  background: #fef3c7;
  color: #92400e;
  border: 1px solid #fde68a;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 10.5px;
  font-weight: 700;
}

.rs-action {
  background: #eff6ff;
  color: #1e40af;
  border: 1px solid #bfdbfe;
  padding: 1px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 700;
}

.rs-thought {
  color: var(--txt-body);
  line-height: 1.55;
  margin-bottom: 5px;
}

.rs-input {
  color: var(--txt-sec);
  margin-bottom: 6px;
  word-break: break-all;
}

.rs-obs {
  background: #f8fafc;
  border: 1px solid var(--border-line);
  border-radius: 5px;
  padding: 7px 10px;
}

.obs-label {
  font-size: 11px;
  font-weight: 800;
  color: var(--txt-sec);
  margin-bottom: 4px;
}

.obs-pre {
  font-size: 11px;
  color: var(--txt-body);
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.45;
}

.obs-more-btn {
  margin-top: 4px;
  border: none;
  background: transparent;
  color: var(--brand);
  font-size: 11px;
  font-weight: 800;
  cursor: pointer;
}
</style>
