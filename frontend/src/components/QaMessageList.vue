<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import {
  formatPageOrOffset,
  type QaMessageItem,
  type QaQuoteOut,
  type ReasoningEffortLevel,
} from '@/api/types'
import ReActSteps from './ReActSteps.vue'

const props = withDefaults(
  defineProps<{
    messages: QaMessageItem[]
    sending: boolean
    presetQuestions?: string[]
    reasoningEffort?: ReasoningEffortLevel
  }>(),
  {
    reasoningEffort: 'medium',
  },
)

const emit = defineEmits<{
  (e: 'send', question: string): void
  (e: 'update:reasoningEffort', level: ReasoningEffortLevel): void
}>()

const questionInput = ref('')
const scrollContainerRef = ref<HTMLElement | null>(null)
const effortMenuOpen = ref(false)
const presetMenuOpen = ref(false)

const EFFORT_OPTIONS: Array<{
  value: ReasoningEffortLevel
  label: string
  desc: string
}> = [
  { value: 'low', label: '⚡ 极速', desc: '快响应 (low) · 开放 7 轮 6 工具' },
  { value: 'medium', label: '⚖ 均衡', desc: '默认均衡 (high) · 开放 7 轮 6 工具' },
  { value: 'high', label: '🧠 深度', desc: 'Max 深度推理 (max) · 开放 7 轮 6 工具' },
]

const currentEffortLabel = computed(() => {
  const found = EFFORT_OPTIONS.find((o) => o.value === props.reasoningEffort)
  return found ? found.label : '⚖ 均衡'
})

function selectEffort(level: ReasoningEffortLevel): void {
  emit('update:reasoningEffort', level)
  effortMenuOpen.value = false
}

function sourceBadge(src: string): { label: string; cls: string } {
  if (src === 'tender') {
    return { label: '招标文件原文', cls: 'info' }
  }
  if (src === 'bid') {
    return { label: '我方投标书原文', cls: 'ok' }
  }
  return { label: '标书原文', cls: 'info' }
}

function visibleQuotes(quotes?: QaQuoteOut[]): QaQuoteOut[] {
  if (!quotes || !Array.isArray(quotes)) return []
  return quotes.filter((q) => Boolean((q.quote && q.quote.trim()) || q.note))
}

function handleSendClick(): void {
  const clean = questionInput.value.trim()
  if (!clean || props.sending) return
  questionInput.value = ''
  effortMenuOpen.value = false
  presetMenuOpen.value = false
  emit('send', clean)
}

function handlePresetClick(q: string): void {
  if (props.sending) return
  presetMenuOpen.value = false
  emit('send', q)
}

function handleKeydown(ev: Event | KeyboardEvent): void {
  if (!('key' in ev)) return
  if (ev.key === 'Enter' && !ev.shiftKey) {
    ev.preventDefault()
    handleSendClick()
  }
}

function scrollToBottom(): void {
  void nextTick(() => {
    if (scrollContainerRef.value) {
      scrollContainerRef.value.scrollTop = scrollContainerRef.value.scrollHeight
    }
  })
}

watch(
  () => props.messages.length,
  () => {
    scrollToBottom()
  },
)

defineExpose({
  scrollToBottom,
})
</script>

<template>
  <div class="qa-msg-pane" @click="effortMenuOpen = false; presetMenuOpen = false">
    <!-- 消息流滚动区 -->
    <div ref="scrollContainerRef" class="msg-scroll-area">
      <div
        v-for="(msg, index) in props.messages"
        :key="index"
        class="msg-item-row"
        :class="msg.role === 'user' ? 'is-user' : 'is-assistant'"
      >
        <!-- User 右侧气泡 -->
        <div v-if="msg.role === 'user'" class="bubble user-bubble">
          <div class="u-role-tag">我方提问</div>
          <div class="u-text">{{ msg.content }}</div>
        </div>

        <!-- Assistant 左侧气泡 -->
        <div v-else class="bubble assistant-bubble">
          <div class="a-head">
            <span class="a-name">BidGuard 标书核验专家</span>
            <span
              v-if="msg.mode === 'keyword_fallback'"
              class="sev-pill warning"
            >
              离线检索模式 (0-Token)
            </span>
            <span v-else-if="msg.mode === 'react'" class="sev-pill info">
              ReAct 多步检索模式
            </span>
          </div>

          <!-- E21 §2.3：思考与调工具阶段（尚未收到 answer_delta 前）动态展示步骤数并默认展开 ReActSteps -->
          <div v-if="msg.loading && !msg.content" class="a-thinking-stage">
            <div class="a-loading-box">
              <span class="spin-dot"></span>
              <span>
                {{
                  msg.steps && msg.steps.length > 0
                    ? `AI 专家已完成 ${msg.steps.length} 步工具检索，正在继续推演与核对原文溯源…`
                    : 'AI 专家正在分析问题并调起双书检索工具…'
                }}
              </span>
            </div>

            <ReActSteps
              v-if="msg.steps && msg.steps.length > 0"
              :steps="msg.steps"
              :default-open="true"
            />
          </div>

          <!-- 收到 answer_delta 流式吐字或最终完成态 -->
          <template v-else>
            <div class="a-answer-text">
              <span>{{ msg.content }}</span>
              <span v-if="msg.streaming || msg.loading" class="stream-cursor">▋</span>
            </div>

            <!-- Quotes 原文溯源卡片列表（严格执行附件一 RiskCard 三分支溯源徽标语义） -->
            <div v-if="visibleQuotes(msg.quotes).length > 0" class="quotes-grid">
              <div class="qg-title">原文出处溯源核验卡（{{ visibleQuotes(msg.quotes).length }} 处）</div>
              <div
                v-for="(q, qIdx) in visibleQuotes(msg.quotes)"
                :key="qIdx"
                class="quote-card"
              >
                <div class="qc-top">
                  <span class="sev-pill" :class="sourceBadge(q.source).cls">
                    {{ sourceBadge(q.source).label }}
                  </span>
                  <span class="qc-loc mono-num">
                    定位：{{ formatPageOrOffset(q.page, q.offset) }}
                  </span>
                  <span v-if="q.quote_verified" class="verified-tick">
                    ✓ 已通过原文溯源
                  </span>
                  <span v-else class="unverified-tick">
                    {{ q.note || '未经溯源校验（未通过原文匹配已清空）' }}
                  </span>
                </div>
                <div v-if="q.quote" class="qc-text">“{{ q.quote }}”</div>
              </div>
            </div>

            <!-- ReAct 轨迹面板（流式期保持展开，历史已完成消息可自由折叠/展开） -->
            <ReActSteps
              v-if="msg.steps && msg.steps.length > 0"
              :steps="msg.steps"
              :default-open="Boolean(msg.streaming)"
            />
          </template>
        </div>
      </div>

      <el-empty
        v-if="props.messages.length === 0"
        description="点击下方快捷问题或输入您想核实的评分项、质保期、★条款响应页码"
        :image-size="68"
      />
    </div>

    <!-- 底部输入舱（E23：空会话时展开快捷追问芯片，有消息后自动收起为弹出按钮；发送按钮旁集成三挡思考深度菜单） -->
    <div class="msg-input-dock">
      <div
        v-if="props.messages.length === 0 && props.presetQuestions && props.presetQuestions.length > 0"
        class="preset-chips"
      >
        <span class="chip-lbl">快捷追问：</span>
        <button
          v-for="pq in props.presetQuestions"
          :key="pq"
          type="button"
          class="preset-chip-btn"
          :disabled="props.sending"
          @click="handlePresetClick(pq)"
        >
          {{ pq }}
        </button>
      </div>

      <div class="input-row">
        <el-input
          v-model="questionInput"
          type="textarea"
          :autosize="{ minRows: 1, maxRows: 4 }"
          :disabled="props.sending"
          placeholder="输入标书追问（支持多轮上下文追问，如：刚刚提到的条款在投标书哪一页？）按 Enter 发送…"
          @keydown="handleKeydown"
        />

        <div class="dock-right-controls">
          <!-- 对话开始后折叠的快捷追问弹出菜单 -->
          <div
            v-if="props.messages.length > 0 && props.presetQuestions && props.presetQuestions.length > 0"
            class="pop-wrap"
            @click.stop
          >
            <button
              type="button"
              class="dock-tool-btn"
              :disabled="props.sending"
              title="查看常用快捷追问"
              @click="presetMenuOpen = !presetMenuOpen; effortMenuOpen = false"
            >
              💡 快捷追问 ▾
            </button>
            <div v-if="presetMenuOpen" class="pop-menu preset-pop-menu">
              <div class="pm-hd">点击直接发起常见追问：</div>
              <button
                v-for="pq in props.presetQuestions"
                :key="pq"
                type="button"
                class="pm-item"
                @click="handlePresetClick(pq)"
              >
                {{ pq }}
              </button>
            </div>
          </div>

          <!-- 发送按钮左侧三挡推理深度弹出选择器 -->
          <div class="pop-wrap" @click.stop>
            <button
              type="button"
              class="dock-tool-btn effort-trigger-btn"
              title="切换大模型 ReAct 推理思考深度（均开放 7 轮 6 工具）"
              @click="effortMenuOpen = !effortMenuOpen; presetMenuOpen = false"
            >
              {{ currentEffortLabel }} ▾
            </button>
            <div v-if="effortMenuOpen" class="pop-menu effort-pop-menu">
              <div class="pm-hd">选择 ReAct 推理思考深度：</div>
              <button
                v-for="opt in EFFORT_OPTIONS"
                :key="opt.value"
                type="button"
                class="pm-item effort-opt-item"
                :class="{ active: props.reasoningEffort === opt.value }"
                @click="selectEffort(opt.value)"
              >
                <span class="eoi-lbl">{{ opt.label }}</span>
                <span class="eoi-desc">{{ opt.desc }}</span>
              </button>
            </div>
          </div>

          <el-button
            type="primary"
            class="send-action-btn"
            :loading="props.sending"
            :disabled="props.sending"
            @click="handleSendClick"
          >
            {{ props.sending ? '核验中…' : '发送追问' }}
          </el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.qa-msg-pane {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 480px;
}

.msg-scroll-area {
  flex: 1;
  overflow-y: auto;
  padding: 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  background: var(--bg-subtle);
}

.msg-item-row {
  display: flex;
  width: 100%;
}

.msg-item-row.is-user {
  justify-content: flex-end;
}

.msg-item-row.is-assistant {
  justify-content: flex-start;
}

.bubble {
  max-width: 92%;
  padding: 12px 15px;
  border-radius: 10px;
  box-shadow: var(--shadow-card);
}

.user-bubble {
  background: var(--brand);
  color: #ffffff;
  border-bottom-right-radius: 2px;
}

.u-role-tag {
  font-size: 10.5px;
  font-weight: 800;
  opacity: 0.85;
  margin-bottom: 3px;
}

.u-text {
  font-size: 13.5px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}

.assistant-bubble {
  width: 92%;
  background: #ffffff;
  border: 1px solid var(--border-line);
  border-bottom-left-radius: 2px;
}

.a-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding-bottom: 7px;
  border-bottom: 1px solid var(--border-line);
  margin-bottom: 9px;
}

.a-name {
  font-size: 13px;
  font-weight: 900;
  color: var(--txt-strong);
}

.a-thinking-stage {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.a-loading-box {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 11px 14px;
  background: var(--brand-soft);
  border: 1px solid var(--brand-border);
  border-radius: 6px;
  color: var(--brand);
  font-weight: 700;
  font-size: 12.5px;
}

.spin-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--brand);
  animation: pulseDot 1s infinite ease-in-out;
  flex-shrink: 0;
}

@keyframes pulseDot {
  0%,
  100% {
    transform: scale(0.85);
    opacity: 0.5;
  }
  50% {
    transform: scale(1.25);
    opacity: 1;
  }
}

.a-answer-text {
  font-size: 13.5px;
  color: var(--txt-body);
  line-height: 1.68;
  white-space: pre-wrap;
  word-break: break-word;
}

.stream-cursor {
  display: inline-block;
  margin-left: 2px;
  color: var(--brand);
  font-weight: 900;
  animation: blinkCursor 0.8s infinite;
}

@keyframes blinkCursor {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0;
  }
}

.quotes-grid {
  margin-top: 10px;
  padding-top: 9px;
  border-top: 1px dashed var(--border-line);
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.qg-title {
  font-size: 11.5px;
  font-weight: 800;
  color: var(--txt-sec);
}

.quote-card {
  padding: 8px 11px;
  background: #f8fafc;
  border: 1px solid var(--border-line);
  border-left: 3px solid var(--brand);
  border-radius: 6px;
}

.qc-top {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}

.qc-loc {
  font-size: 11.5px;
  color: var(--txt-muted);
}

.verified-tick {
  margin-left: auto;
  font-size: 11px;
  font-weight: 800;
  color: var(--sev-ok);
}

.unverified-tick {
  margin-left: auto;
  font-size: 11px;
  color: #888780;
}

.qc-text {
  font-size: 12.5px;
  color: var(--txt-strong);
  line-height: 1.55;
  word-break: break-word;
}

.msg-input-dock {
  padding: 10px 16px;
  background: #ffffff;
  border-top: 1px solid var(--border-line);
}

.preset-chips {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.chip-lbl {
  font-size: 11.5px;
  font-weight: 800;
  color: var(--txt-muted);
}

.preset-chip-btn {
  padding: 3px 9px;
  border-radius: 5px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
  color: var(--txt-sec);
  font-size: 11.5px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
}

.preset-chip-btn:hover:not(:disabled) {
  border-color: var(--brand);
  color: var(--brand);
  background: var(--brand-soft);
}

.preset-chip-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.input-row {
  display: flex;
  gap: 8px;
  align-items: flex-end;
}

.dock-right-controls {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.pop-wrap {
  position: relative;
}

.dock-tool-btn {
  height: 34px;
  padding: 0 10px;
  border-radius: 6px;
  border: 1px solid var(--border-strong);
  background: var(--bg-subtle);
  color: var(--txt-strong);
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
  white-space: nowrap;
  transition: all 0.15s;
}

.dock-tool-btn:hover:not(:disabled) {
  border-color: var(--brand);
  color: var(--brand);
  background: var(--brand-soft);
}

.effort-trigger-btn {
  background: #eff6ff;
  border-color: #bfdbfe;
  color: var(--brand);
}

.pop-menu {
  position: absolute;
  bottom: calc(100% + 8px);
  right: 0;
  background: #ffffff;
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  box-shadow: 0 10px 25px rgba(15, 23, 42, 0.14);
  padding: 8px;
  z-index: 50;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.preset-pop-menu {
  width: 340px;
}

.effort-pop-menu {
  width: 250px;
}

.pm-hd {
  font-size: 11px;
  font-weight: 800;
  color: var(--txt-muted);
  padding: 2px 6px 4px;
  border-bottom: 1px solid var(--border-line);
  margin-bottom: 2px;
}

.pm-item {
  text-align: left;
  padding: 6px 8px;
  border-radius: 5px;
  border: none;
  background: transparent;
  color: var(--txt-strong);
  font-size: 12px;
  cursor: pointer;
  transition: background 0.12s;
}

.pm-item:hover {
  background: var(--bg-subtle);
}

.effort-opt-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.effort-opt-item.active {
  background: var(--brand-soft);
  color: var(--brand);
}

.eoi-lbl {
  font-weight: 800;
  font-size: 12.5px;
}

.eoi-desc {
  font-size: 11px;
  color: var(--txt-muted);
}

.send-action-btn {
  height: 34px;
  min-width: 96px;
  font-weight: 800;
}
</style>