<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { formatPageOrOffset, type QaMessageItem, type QaQuoteOut } from '@/api/types'
import ReActSteps from './ReActSteps.vue'

const props = defineProps<{
  messages: QaMessageItem[]
  sending: boolean
  presetQuestions?: string[]
}>()

const emit = defineEmits<{
  (e: 'send', question: string): void
}>()

const questionInput = ref('')
const scrollContainerRef = ref<HTMLElement | null>(null)

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
  emit('send', clean)
}

function handlePresetClick(q: string): void {
  if (props.sending) return
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
  <div class="qa-msg-pane">
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

    <!-- 底部预设快捷提问与输入框 -->
    <div class="msg-input-dock">
      <div v-if="props.presetQuestions && props.presetQuestions.length > 0" class="preset-chips">
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
          :rows="2"
          :disabled="props.sending"
          placeholder="输入标书追问（支持多轮上下文追问，如：刚刚提到的条款在投标书哪一页？）按 Enter 发送…"
          @keydown="handleKeydown"
        />
        <el-button
          type="primary"
          class="send-action-btn"
          :loading="props.sending"
          :disabled="props.sending"
          @click="handleSendClick"
        >
          {{ props.sending ? '检索核验中…' : '发送追问' }}
        </el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.qa-msg-pane {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 540px;
}

.msg-scroll-area {
  flex: 1;
  overflow-y: auto;
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
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
  max-width: 85%;
  padding: 14px 16px;
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
  margin-bottom: 4px;
}

.u-text {
  font-size: 13.5px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}

.assistant-bubble {
  width: 86%;
  background: #ffffff;
  border: 1px solid var(--border-line);
  border-bottom-left-radius: 2px;
}

.a-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border-line);
  margin-bottom: 10px;
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
  padding: 12px 14px;
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
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed var(--border-line);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.qg-title {
  font-size: 11.5px;
  font-weight: 800;
  color: var(--txt-sec);
}

.quote-card {
  padding: 9px 12px;
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
  margin-bottom: 5px;
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
  padding: 12px 18px;
  background: #ffffff;
  border-top: 1px solid var(--border-line);
}

.preset-chips {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.chip-lbl {
  font-size: 11.5px;
  font-weight: 800;
  color: var(--txt-muted);
}

.preset-chip-btn {
  padding: 4px 10px;
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
  gap: 10px;
  align-items: stretch;
}

.send-action-btn {
  height: auto;
  min-width: 120px;
  font-weight: 800;
}
</style>
