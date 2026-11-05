<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { subscribeCheckStream } from '@/api/sse'
import type { BidTaskStats, SSEProgressFrame } from '@/api/types'

const props = defineProps<{
  taskId: number
  status: string
  progress: number
  stageDetail?: string | null
  errorMsg?: string | null
}>()

const emit = defineEmits<{
  (e: 'progress', frame: SSEProgressFrame): void
  (e: 'done', stats?: BidTaskStats): void
  (e: 'error', msg: string): void
}>()

interface PipelineStageNode {
  key: string
  stepNo: string
  title: string
  desc: string
  thresholdPct: number
}

const PIPELINE_STAGES: PipelineStageNode[] = [
  { key: 'created', stepNo: '01', title: '双书入库', desc: '招标书与投标书解析就绪', thresholdPct: 5 },
  { key: 'extracting', stepNo: '02', title: 'F1 红线提取', desc: '资格/符合/★▲强制条款抽取', thresholdPct: 20 },
  { key: 'checking_det', stepNo: '03', title: 'F2/F3 确定性沙盒', desc: '报价算术与证照/串名 0-Token 扫描', thresholdPct: 45 },
  { key: 'checking', stepNo: '04', title: 'F4 双书逐条勾稽', desc: '★条款漏答与书内前后矛盾核验', thresholdPct: 65 },
  { key: 'grading', stepNo: '05', title: '废标风险定级', desc: '一票否决项与扣分项归类锁账', thresholdPct: 85 },
  { key: 'delivered', stepNo: '06', title: '体检交付就绪', desc: '生成风险清单与偏离表底座', thresholdPct: 100 },
]

const streaming = ref(false)
const localStatus = ref<string>(props.status)
const localProgress = ref<number>(props.progress || 0)
const localDetail = ref<string>(props.stageDetail || '')
const localError = ref<string>(props.errorMsg || '')

let stopStreamFn: (() => void) | null = null

function closeActiveStream(): void {
  if (stopStreamFn) {
    stopStreamFn()
    stopStreamFn = null
  }
  streaming.value = false
}

function getStageState(node: PipelineStageNode, index: number): 'done' | 'active' | 'failed' | 'wait' {
  const st = localStatus.value
  const pct = localProgress.value

  if (st === 'delivered') return 'done'
  if (st === 'failed') {
    if (pct >= node.thresholdPct) return 'done'
    if (index === 0 || pct >= (PIPELINE_STAGES[index - 1]?.thresholdPct || 0)) return 'failed'
    return 'wait'
  }
  if (st === 'created' && pct === 0) {
    return index === 0 ? 'done' : 'wait'
  }
  if (pct >= node.thresholdPct + 15) return 'done'
  if (st === node.key || (pct >= node.thresholdPct - 10 && pct < node.thresholdPct + 15)) {
    return 'active'
  }
  return pct > node.thresholdPct ? 'done' : 'wait'
}

const isFailed = computed(() => localStatus.value === 'failed')
const isDelivered = computed(() => localStatus.value === 'delivered')

function startStream(): void {
  closeActiveStream()
  streaming.value = true
  stopStreamFn = subscribeCheckStream(props.taskId, {
    onProgress(frame) {
      localStatus.value = frame.status
      if (typeof frame.progress === 'number') {
        localProgress.value = Math.max(localProgress.value, frame.progress)
      }
      if (frame.stage_detail) {
        localDetail.value = frame.stage_detail
      }
      emit('progress', frame)
    },
    onDone(frame) {
      streaming.value = false
      localStatus.value = 'delivered'
      localProgress.value = 100
      localDetail.value = frame.stage_detail || '防废标核验全部完成'
      emit('done', frame.stats)
    },
    onError(errOrFrame) {
      streaming.value = false
      localStatus.value = 'failed'
      const msg =
        errOrFrame instanceof Error
          ? errOrFrame.message
          : errOrFrame.error_msg || '核验流水线异常中断'
      localError.value = msg
      emit('error', msg)
    },
  })
}

watch(
  () => [props.taskId, props.status, props.progress, props.stageDetail, props.errorMsg],
  (newVals, oldVals) => {
    const [newId, newSt, newProg, newDet, newErr] = newVals ?? []
    const [oldId] = oldVals ?? []
    if (newId !== oldId) {
      closeActiveStream()
    }
    localStatus.value = String(newSt || 'created')
    localProgress.value = Number(newProg || 0)
    localDetail.value = String(newDet || '')
    localError.value = String(newErr || '')

    if (localStatus.value in { extracting: 1, checking: 1, grading: 1 } && !streaming.value) {
      startStream()
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  closeActiveStream()
})

defineExpose({
  startStream,
  closeActiveStream,
})
</script>

<template>
  <div class="sse-stepper bg-panel" :class="{ 'is-failed': isFailed }">
    <div class="st-top-bar">
      <div class="st-status-line">
        <span
          class="pulse-indicator"
          :class="{
            running: streaming,
            done: isDelivered,
            err: isFailed,
          }"
        ></span>
        <span class="st-main-txt">
          <template v-if="isDelivered">
            六步防废标体检流水线已交付完成
          </template>
          <template v-else-if="isFailed">
            核验流水线异常中断：{{ localError || localDetail || '请检查文档后重试' }}
          </template>
          <template v-else>
            {{ localDetail || '等待启动封标前防废标核验流水线' }}
          </template>
        </span>
      </div>

      <span class="st-pct mono-num" :class="{ err: isFailed, ok: isDelivered }">
        {{ localProgress }}%
      </span>
    </div>

    <div class="st-track">
      <div
        class="st-track-fill"
        :class="{ err: isFailed, ok: isDelivered }"
        :style="{ width: `${localProgress}%` }"
      ></div>
    </div>

    <div class="st-nodes-grid">
      <div
        v-for="(node, idx) in PIPELINE_STAGES"
        :key="node.key"
        class="st-node"
        :class="getStageState(node, idx)"
      >
        <div class="nd-head">
          <span class="nd-num mono-num">{{ node.stepNo }}</span>
          <span class="nd-title">{{ node.title }}</span>
        </div>
        <div class="nd-desc">{{ node.desc }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sse-stepper {
  padding: 16px 20px;
}

.sse-stepper.is-failed {
  border-color: var(--sev-error-border);
  background: #fffafa;
}

.st-top-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.st-status-line {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 700;
  color: var(--txt-strong);
}

.pulse-indicator {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--txt-muted);
}

.pulse-indicator.running {
  background: var(--brand);
  box-shadow: 0 0 0 4px rgba(29, 78, 216, 0.2);
}

.pulse-indicator.done {
  background: var(--sev-ok);
}

.pulse-indicator.err {
  background: var(--sev-error);
}

.st-pct {
  font-size: 14px;
  font-weight: 900;
  color: var(--brand);
}

.st-pct.ok {
  color: var(--sev-ok);
}

.st-pct.err {
  color: var(--sev-error);
}

.st-track {
  height: 6px;
  background: #e2e8f0;
  border-radius: 99px;
  overflow: hidden;
  margin-bottom: 14px;
}

.st-track-fill {
  height: 100%;
  background: var(--brand);
  transition: width 0.3s ease;
}

.st-track-fill.ok {
  background: var(--sev-ok);
}

.st-track-fill.err {
  background: var(--sev-error);
}

.st-nodes-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 10px;
}

.st-node {
  padding: 10px 11px;
  border-radius: 7px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
  transition: all 0.2s;
}

.st-node.done {
  background: #f0fdf4;
  border-color: #a7f3d0;
}

.st-node.active {
  background: var(--brand-soft);
  border-color: var(--brand);
  box-shadow: 0 0 0 2px rgba(29, 78, 216, 0.15);
}

.st-node.failed {
  background: var(--sev-error-bg);
  border-color: var(--sev-error);
}

.nd-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 3px;
}

.nd-num {
  font-size: 11px;
  font-weight: 900;
  padding: 1px 5px;
  border-radius: 4px;
  background: rgba(15, 23, 42, 0.08);
  color: var(--txt-strong);
}

.st-node.done .nd-num {
  background: var(--sev-ok);
  color: #ffffff;
}

.st-node.active .nd-num {
  background: var(--brand);
  color: #ffffff;
}

.st-node.failed .nd-num {
  background: var(--sev-error);
  color: #ffffff;
}

.nd-title {
  font-size: 12.5px;
  font-weight: 800;
  color: var(--txt-strong);
}

.nd-desc {
  font-size: 11px;
  color: var(--txt-muted);
  line-height: 1.4;
}

@media (max-width: 1280px) {
  .st-nodes-grid {
    grid-template-columns: repeat(3, 1fr);
  }
}
</style>