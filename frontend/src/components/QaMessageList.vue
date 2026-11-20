<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { validateQaAttachmentFile } from '@/api'
import {
  formatPageOrOffset,
  type QaAttachmentOut,
  type QaMessageItem,
  type QaQuoteOut,
  type ReasoningEffortLevel,
} from '@/api/types'
import ReActSteps from './ReActSteps.vue'

const props = withDefaults(
  defineProps<{
    messages: QaMessageItem[]
    sending: boolean
    uploadingAttachment?: boolean
    attachments?: QaAttachmentOut[]
    selectedAttachmentIds?: string[]
    presetQuestions?: string[]
    reasoningEffort?: ReasoningEffortLevel
  }>(),
  {
    uploadingAttachment: false,
    attachments: () => [],
    selectedAttachmentIds: () => [],
    reasoningEffort: 'medium',
  },
)

const emit = defineEmits<{
  (e: 'send', question: string, attachmentIds: string[]): void
  (e: 'upload-attachment', file: File): void
  (e: 'delete-attachment', att: QaAttachmentOut): void
  (e: 'update:selectedAttachmentIds', ids: string[]): void
  (e: 'update:reasoningEffort', level: ReasoningEffortLevel): void
  (e: 'regenerate'): void
  (e: 'withdraw-last'): void
  (e: 'stop-stream'): void
}>()

const questionInput = ref('')
const scrollContainerRef = ref<HTMLElement | null>(null)
const attachInputRef = ref<HTMLInputElement | null>(null)
const effortMenuOpen = ref(false)
const presetMenuOpen = ref(false)

const EFFORT_OPTIONS: Array<{
  value: ReasoningEffortLevel
  label: string
  desc: string
}> = [
  { value: 'low', label: '⚡ 极速', desc: '快响应 (low) · 最高 7 个思考回合（支持单回合多工具并行）' },
  { value: 'medium', label: '⚖ 均衡', desc: '默认均衡 (high) · 最高 8 个思考回合（支持单回合多工具并行）' },
  { value: 'high', label: '🧠 深度', desc: 'Max 深度推理 (max) · 最高 12 个思考回合（证据齐备即早停）' },
]

const currentEffortLabel = computed(() => {
  const found = EFFORT_OPTIONS.find((o) => o.value === props.reasoningEffort)
  return found ? found.label : '⚖ 均衡'
})

function selectEffort(level: ReasoningEffortLevel): void {
  emit('update:reasoningEffort', level)
  effortMenuOpen.value = false
}

function sourceBadge(q: QaQuoteOut): { label: string; cls: string } {
  if (q.source === 'tender') {
    return { label: '招标文件原文', cls: 'info' }
  }
  if (q.source === 'bid') {
    return { label: '我方投标书原文', cls: 'ok' }
  }
  if (q.source === 'attachment') {
    const name = q.attachment_name || q.attachment_id || '补充附件'
    return { label: `附件：${name}`, cls: 'warning' }
  }
  return { label: '标书原文', cls: 'info' }
}

function visibleQuotes(quotes?: QaQuoteOut[]): QaQuoteOut[] {
  if (!quotes || !Array.isArray(quotes)) return []
  return quotes.filter((q) => Boolean((q.quote && q.quote.trim()) || q.note))
}

function isSelectedAtt(attId: string): boolean {
  return (props.selectedAttachmentIds || []).includes(attId)
}

function toggleAttachmentSelection(att: QaAttachmentOut): void {
  if (props.sending) return
  const cur = [...(props.selectedAttachmentIds || [])]
  const idx = cur.indexOf(att.attachment_id)
  if (idx >= 0) {
    cur.splice(idx, 1)
    emit('update:selectedAttachmentIds', cur)
    return
  }
  if (att.is_image) {
    const selectedImgs = (props.attachments || []).filter(
      (a) => a.is_image && cur.includes(a.attachment_id),
    )
    if (selectedImgs.length >= 4) {
      ElMessage.warning('单轮追问最多可同时勾选 4 张图片附件')
      return
    }
  }
  cur.push(att.attachment_id)
  emit('update:selectedAttachmentIds', cur)
}

function triggerAttachFilePicker(): void {
  if (props.sending || props.uploadingAttachment) return
  attachInputRef.value?.click()
}

function onAttachFilePicked(ev: Event): void {
  const input = ev.target as HTMLInputElement
  const f = input.files?.[0]
  input.value = ''
  if (!f) return
  const check = validateQaAttachmentFile(f)
  if (!check.valid) {
    ElMessage.warning(check.message)
    return
  }
  emit('upload-attachment', f)
}

function validateTurnImagesBeforeSend(): boolean {
  const curIds = props.selectedAttachmentIds || []
  const imgCnt = (props.attachments || []).filter(
    (a) => a.is_image && curIds.includes(a.attachment_id),
  ).length
  if (imgCnt > 4) {
    ElMessage.warning(`单轮追问最多允许勾选 4 张图片附件（当前已勾选 ${imgCnt} 张）`)
    return false
  }
  return true
}

function handleSendClick(): void {
  const clean = questionInput.value.trim()
  if (!clean || props.sending) return
  if (!validateTurnImagesBeforeSend()) return
  questionInput.value = ''
  effortMenuOpen.value = false
  presetMenuOpen.value = false
  emit('send', clean, [...(props.selectedAttachmentIds || [])])
}

function handlePresetClick(q: string): void {
  if (props.sending) return
  if (!validateTurnImagesBeforeSend()) return
  presetMenuOpen.value = false
  emit('send', q, [...(props.selectedAttachmentIds || [])])
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

function setQuestionInput(val: string): void {
  questionInput.value = val
}

defineExpose({
  scrollToBottom,
  setQuestionInput,
})

const QUOTES_COLLAPSE_LIMIT = 2
const expandedQuotesMap = ref<Record<string, boolean>>({})

function quoteMsgKey(msg: { id?: string | number; created_at?: string; content?: string }, idx: number): string {
  return String(msg.id ?? msg.created_at ?? `${idx}_${(msg.content || '').slice(0, 16)}`)
}

function isQuotesExpanded(msg: { id?: string | number; created_at?: string; content?: string }, idx: number): boolean {
  return Boolean(expandedQuotesMap.value[quoteMsgKey(msg, idx)])
}

function toggleQuotesExpanded(msg: { id?: string | number; created_at?: string; content?: string }, idx: number): void {
  const k = quoteMsgKey(msg, idx)
  expandedQuotesMap.value[k] = !expandedQuotesMap.value[k]
}

function displayedQuotes(
  quotes: QaQuoteOut[] | undefined,
  msg: { id?: string | number; created_at?: string; content?: string },
  idx: number,
): QaQuoteOut[] {
  const all = visibleQuotes(quotes)
  if (all.length <= QUOTES_COLLAPSE_LIMIT || isQuotesExpanded(msg, idx)) {
    return all
  }
  return all.slice(0, QUOTES_COLLAPSE_LIMIT)
}
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
        <!-- User 右侧气泡（回显本轮随附的附件胶囊） -->
        <div v-if="msg.role === 'user'" class="bubble user-bubble">
          <div class="u-text">{{ msg.content }}</div>
          <div v-if="msg.attachments && msg.attachments.length > 0" class="u-att-pills">
            <span
              v-for="att in msg.attachments"
              :key="att.attachment_id"
              class="u-att-chip"
            >
              📎 {{ att.filename }}
            </span>
          </div>
        </div>

        <!-- Assistant 左侧气泡 -->
        <div v-else class="bubble assistant-bubble">
          <div class="a-head">
            <span class="a-name">智标 · 标书合规专家</span>
            <div class="a-head-badges">
              <span
                v-if="msg.grounded === false"
                class="sev-pill warning"
                title="该回答未执行招投标文件检索，仅供参考"
              >
                未检索标书原文 · 仅供参考
              </span>
              <span
                v-if="msg.mode === 'keyword_fallback'"
                class="sev-pill warning"
              >
                离线检索模式
              </span>
              <span
                v-else-if="msg.mode === 'react' && msg.grounded !== false"
                class="sev-pill info"
              >
                多步原文检索核验
              </span>
            </div>
          </div>

          <div v-if="msg.loading && !msg.content" class="a-thinking-stage">
            <div class="a-loading-box">
              <span class="spin-dot"></span>
              <span>
                {{
                  msg.steps && msg.steps.length > 0
                    ? `AI 专家已完成 ${msg.steps.length} 步工具检索，正在继续推演与核对原文溯源…`
                    : 'AI 专家正在分析问题并调起双书与附件检索工具…'
                }}
              </span>
            </div>

            <ReActSteps
              v-if="msg.steps && msg.steps.length > 0"
              :steps="msg.steps"
              :default-open="true"
            />
          </div>

          <template v-else>
            <div class="a-answer-text">
              <span>{{ msg.content }}</span>
              <span v-if="msg.streaming || msg.loading" class="stream-cursor">▋</span>
            </div>

            <!-- Quotes 原文溯源卡片列表（E24 支持 attachment 来源与视觉结论非逐字核验标记） -->
            <div v-if="visibleQuotes(msg.quotes).length > 0" class="quotes-grid">
              <div class="qg-head">
                <div class="qg-title">原文出处溯源核验卡（{{ visibleQuotes(msg.quotes).length }} 处）</div>
                <button
                  v-if="visibleQuotes(msg.quotes).length > QUOTES_COLLAPSE_LIMIT"
                  type="button"
                  class="qg-toggle-btn"
                  @click="toggleQuotesExpanded(msg, index)"
                >
                  {{ isQuotesExpanded(msg, index) ? '收起溯源 ▲' : `展开其余 ${visibleQuotes(msg.quotes).length - QUOTES_COLLAPSE_LIMIT} 处溯源 ▼` }}
                </button>
              </div>
              <div
                v-for="(q, qIdx) in displayedQuotes(msg.quotes, msg, index)"
                :key="qIdx"
                class="quote-card"
              >
                <div class="qc-top">
                  <span class="sev-pill" :class="sourceBadge(q).cls">
                    {{ sourceBadge(q).label }}
                  </span>
                  <span class="qc-loc mono-num">
                    定位：{{ formatPageOrOffset(q.page, q.offset) }}
                  </span>
                  <span v-if="q.quote_verified" class="verified-tick">
                    ✓ 已通过原文溯源
                  </span>
                  <span v-else class="unverified-tick">
                    {{ q.note || '视觉参考 · 非原文逐字核验' }}
                  </span>
                </div>
                <div v-if="q.quote" class="qc-text">“{{ q.quote }}”</div>
              </div>
            </div>

            <ReActSteps
              v-if="msg.steps && msg.steps.length > 0"
              :steps="msg.steps"
              :default-open="Boolean(msg.streaming)"
            />
          </template>

          <!-- E27 §2.2.4：最后一轮回答操作栏（生成中支持停止生成，完成后支持重新生成与撤回本轮） -->
          <div
            v-if="index === props.messages.length - 1"
            class="a-turn-actions"
          >
            <button
              v-if="msg.loading || msg.streaming || props.sending"
              type="button"
              class="turn-act-btn stop"
              title="立即中断当前流式生成并保留已检索步骤"
              @click="emit('stop-stream')"
            >
              ⏹ 停止生成
            </button>
            <template v-else>
              <button
                type="button"
                class="turn-act-btn retry"
                title="移除本轮回答并使用同一问题与附件重新生成"
                @click="emit('regenerate')"
              >
                ↻ 重新生成回答
              </button>
              <button
                type="button"
                class="turn-act-btn withdraw"
                title="撤回本轮问答并将原问题回填至下方输入框"
                @click="emit('withdraw-last')"
              >
                ↶ 撤回本轮回答
              </button>
            </template>
          </div>
        </div>
      </div>

      <el-empty
        v-if="props.messages.length === 0"
        description="点击下方快捷问题、上传补遗通知/证照图片附件，或直接输入您想核实的标书问题"
        :image-size="68"
      />
    </div>

    <!-- 底部输入舱 -->
    <div class="msg-input-dock">
      <!-- E24：已上传会话附件胶囊条（支持勾选随附本轮提问、显示识别字数/纯图片状态、点击删除） -->
      <div v-if="props.attachments && props.attachments.length > 0" class="att-tray">
        <span class="att-tray-lbl">会话附件（勾选随本轮提问）：</span>
        <div
          v-for="att in props.attachments"
          :key="att.attachment_id"
          class="att-pill"
          :class="{ selected: isSelectedAtt(att.attachment_id) }"
          @click="toggleAttachmentSelection(att)"
        >
          <input
            type="checkbox"
            :checked="isSelectedAtt(att.attachment_id)"
            class="att-chk"
            @click.stop="toggleAttachmentSelection(att)"
          />
          <span class="att-name" :title="att.filename">{{ att.filename }}</span>
          <span class="att-meta mono-num">
            {{
              att.is_image
                ? att.has_text
                  ? `已识别 ${att.ocr_chars} 字`
                  : '纯图片'
                : `${(att.size_bytes / 1024).toFixed(1)}KB`
            }}
          </span>
          <button
            type="button"
            class="att-del-btn"
            title="删除该附件"
            :disabled="props.sending"
            @click.stop="emit('delete-attachment', att)"
          >
            ×
          </button>
        </div>
      </div>

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
          placeholder="输入标书追问（支持上传补充文档/证照图片联合核验）按 Enter 发送…"
          @keydown="handleKeydown"
        />

        <div class="dock-right-controls">
          <!-- E24：上传补充文档/图片附件按钮 -->
          <input
            ref="attachInputRef"
            type="file"
            accept=".docx,.pdf,.txt,.md,.png,.jpg,.jpeg"
            style="display: none"
            @change="onAttachFilePicked"
          />
          <button
            type="button"
            class="dock-tool-btn"
            :disabled="props.sending || props.uploadingAttachment"
            title="上传补充文档（.docx/.pdf/.txt/.md）或证照/公章图片（.png/.jpg，≤20MB）"
            @click.stop="triggerAttachFilePicker"
          >
            {{ props.uploadingAttachment ? '解析中…' : '+ 上传文档/图片' }}
          </button>

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
              title="切换大模型推理思考深度（单回合支持多工具并行，按挡位最高开放 7/8/12 个思考回合）"
              @click="effortMenuOpen = !effortMenuOpen; presetMenuOpen = false"
            >
              {{ currentEffortLabel }} ▾
            </button>
            <div v-if="effortMenuOpen" class="pop-menu effort-pop-menu">
              <div class="pm-hd">选择推理思考深度：</div>
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
            v-if="props.sending"
            type="danger"
            plain
            class="send-action-btn"
            @click="emit('stop-stream')"
          >
            ⏹ 停止生成
          </el-button>
          <el-button
            v-else
            type="primary"
            class="send-action-btn"
            @click="handleSendClick"
          >
            发送追问
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

.u-att-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

.u-att-chip {
  font-size: 11px;
  font-weight: 700;
  padding: 2px 8px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.2);
  color: #ffffff;
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

.a-head-badges {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
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

.att-tray {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
  padding-bottom: 6px;
  border-bottom: 1px dashed var(--border-line);
}

.att-tray-lbl {
  font-size: 11.5px;
  font-weight: 800;
  color: var(--txt-sec);
}

.att-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 8px;
  border-radius: 5px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
  font-size: 11.5px;
  cursor: pointer;
  user-select: none;
}

.att-pill.selected {
  border-color: var(--brand);
  background: var(--brand-soft);
}

.att-chk {
  margin: 0;
  cursor: pointer;
}

.att-name {
  font-weight: 700;
  color: var(--txt-strong);
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.att-meta {
  font-size: 10.5px;
  color: var(--txt-muted);
}

.att-del-btn {
  border: none;
  background: transparent;
  color: var(--sev-error);
  font-weight: 900;
  cursor: pointer;
  padding: 0 2px;
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

.a-turn-actions {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed var(--border-line);
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.turn-act-btn {
  padding: 3px 10px;
  border-radius: 6px;
  font-size: 11.5px;
  font-weight: 700;
  cursor: pointer;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
  color: var(--txt-body);
  transition: all 0.15s;
}

.turn-act-btn:hover {
  border-color: var(--brand);
  color: var(--brand);
  background: #ffffff;
}

.turn-act-btn.stop {
  border-color: #fca5a5;
  background: #fef2f2;
  color: #dc2626;
}

.turn-act-btn.stop:hover {
  background: #fee2e2;
  border-color: #ef4444;
}

.qg-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.qg-toggle-btn {
  border: none;
  background: transparent;
  color: var(--brand, #1d4ed8);
  font-size: 11.5px;
  font-weight: 600;
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 4px;
}

.qg-toggle-btn:hover {
  background: #eff6ff;
}
</style>
