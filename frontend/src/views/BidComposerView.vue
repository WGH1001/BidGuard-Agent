<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { checksApi, composerApi, reportsApi, validateUploadFile } from '@/api'
import type {
  BidFindingOut,
  BidTaskOut,
  CleanBidExportOut,
  ComposeBlueprintData,
  ComposeBlueprintSection,
  ComposeDraftDetailOut,
  ComposeDraftSummaryOut,
  ComposeModeType,
} from '@/api/types'

defineOptions({ name: 'BidComposerView' })

const route = useRoute()

const mainTab = ref<'compose' | 'clean'>('compose')

// ── Tab 1: 正向智能编制初稿状态 ───────────────────────────────────────────
const tenderSourceType = ref<'upload' | 'task'>('upload')
const tenderFile = ref<File | null>(null)
const selectedSourceTaskId = ref<number | undefined>(undefined)
const composeTitle = ref('')
const packageNo = ref<number | undefined>(undefined)
const composeMode = ref<ComposeModeType>('skeleton_v1')
const knowledgeFiles = ref<File[]>([])
const generating = ref(false)

const loadingDrafts = ref(false)
const draftList = ref<ComposeDraftSummaryOut[]>([])
const activeDraft = ref<ComposeDraftDetailOut | null>(null)
const expandedSections = ref<string[]>([
  'sec_cover',
  'sec_qual',
  'sec_price',
  'sec_deviation',
  'sec_scoring',
])
const highlightedSectionId = ref<string>('')

// ── Tab 2: 采纳整改导定稿状态 ─────────────────────────────────────────────
const loadingTasks = ref(false)
const taskList = ref<BidTaskOut[]>([])
const cleanTaskId = ref<number | undefined>(undefined)
const loadingAccepted = ref(false)
const acceptedFindings = ref<BidFindingOut[]>([])
const replacementDrafts = reactive<Record<number, string>>({})
const exportingClean = ref(false)
const cleanResult = ref<CleanBidExportOut | null>(null)

const activeBlueprint = computed<ComposeBlueprintData | null>(() => {
  if (!activeDraft.value || !activeDraft.value.blueprint) return null
  return activeDraft.value.blueprint as ComposeBlueprintData
})

const cleanSelectedTask = computed<BidTaskOut | null>(() => {
  if (cleanTaskId.value === undefined) return null
  return taskList.value.find((t) => t.id === cleanTaskId.value) || null
})

const cleanIsPdfTask = computed<boolean>(() => {
  const fn = (cleanSelectedTask.value?.bid_filename || '').toLowerCase()
  return fn.endsWith('.pdf')
})

function handleTenderFileChange(ev: Event): void {
  const input = ev.target as HTMLInputElement
  const f = input.files?.[0]
  input.value = ''
  if (!f) return
  if (!validateUploadFile(f)) return
  tenderFile.value = f
}

function handleKnowledgeFilesChange(ev: Event): void {
  const input = ev.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  for (const f of files) {
    if (!validateUploadFile(f)) continue
    if (knowledgeFiles.value.some((k) => k.name === f.name && k.size === f.size)) continue
    knowledgeFiles.value.push(f)
  }
}

function removeKnowledgeFile(idx: number): void {
  knowledgeFiles.value.splice(idx, 1)
}

async function loadDrafts(): Promise<void> {
  loadingDrafts.value = true
  try {
    const res = await composerApi.listDrafts()
    draftList.value = res.items
    if (!activeDraft.value && res.items.length > 0) {
      await openDraftDetail(res.items[0].id)
    }
  } finally {
    loadingDrafts.value = false
  }
}

async function openDraftDetail(draftId: number): Promise<void> {
  activeDraft.value = await composerApi.getDraft(draftId)
  highlightedSectionId.value = ''
}

async function handleGenerateDraft(): Promise<void> {
  if (generating.value) return
  if (tenderSourceType.value === 'upload' && !tenderFile.value) {
    ElMessage.warning('请先选择上传一份招标文件（.docx / .pdf / .txt）')
    return
  }
  if (tenderSourceType.value === 'task' && !selectedSourceTaskId.value) {
    ElMessage.warning('请先选择一个已有体检任务作为招标依据')
    return
  }
  generating.value = true
  try {
    const detail = await composerApi.generate({
      tenderFile: tenderSourceType.value === 'upload' ? tenderFile.value : null,
      taskId: tenderSourceType.value === 'task' ? selectedSourceTaskId.value : null,
      knowledgeFiles: composeMode.value === 'knowledge_draft_v2' ? knowledgeFiles.value : [],
      composeMode: composeMode.value,
      title: composeTitle.value.trim() || undefined,
      packageNo: packageNo.value || undefined,
    })
    activeDraft.value = detail
    ElMessage.success('标书初稿骨架蓝图与 Word 初稿生成完成')
    await loadDrafts()
  } finally {
    generating.value = false
  }
}

async function handleDownloadDraft(item: { id: number; title: string }): Promise<void> {
  await composerApi.downloadDraft(item.id, `${item.title}_投标文件初稿骨架.docx`)
  ElMessage.success('已下载《投标文件初稿骨架.docx》')
}

async function handleDeleteDraft(item: ComposeDraftSummaryOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除草稿「${item.title}」及服务器生成的初稿文档吗？`,
      '确认删除编制草稿',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await composerApi.deleteDraft(item.id)
  if (activeDraft.value?.id === item.id) {
    activeDraft.value = null
  }
  ElMessage.success('草稿已删除并清理磁盘文件')
  await loadDrafts()
}

function jumpToSection(sectionId: string): void {
  if (!expandedSections.value.includes(sectionId)) {
    expandedSections.value.push(sectionId)
  }
  highlightedSectionId.value = sectionId
  const el = document.getElementById(`composer-sec-${sectionId}`)
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
}

function sectionBlocks(sec: ComposeBlueprintSection, kind?: string): Array<Record<string, unknown>> {
  const list = sec.content_blocks || []
  return kind ? list.filter((b) => b.kind === kind) : list
}

function findOldTargetForFinding(f: Record<string, unknown> | BidFindingOut): string {
  const detail = ((f as BidFindingOut).detail_json || {}) as Record<string, unknown>
  return String(
    detail.wrong_text || detail.stale_name || (f as BidFindingOut).bid_quote || (f as BidFindingOut).quote || '',
  ).trim()
}

async function loadTasks(): Promise<void> {
  loadingTasks.value = true
  try {
    const res = await checksApi.list({ page: 1, page_size: 50, task_type: 'pre_check' })
    taskList.value = res.items || []
    if (!cleanTaskId.value && taskList.value.length > 0) {
      const preferred = taskList.value.find((t) => !(t.bid_filename || '').toLowerCase().endsWith('.pdf'))
      cleanTaskId.value = (preferred || taskList.value[0]).id
    }
    if (!selectedSourceTaskId.value && taskList.value.length > 0) {
      selectedSourceTaskId.value = taskList.value[0].id
    }
  } finally {
    loadingTasks.value = false
  }
}

async function loadAcceptedForClean(tid: number | undefined): Promise<void> {
  cleanResult.value = null
  acceptedFindings.value = []
  Object.keys(replacementDrafts).forEach((k) => {
    delete replacementDrafts[Number(k)]
  })
  if (!tid) return
  loadingAccepted.value = true
  try {
    const res = await checksApi.findings(tid)
    acceptedFindings.value = (res.items || []).filter((f) => f.feedback === 'accepted')
    for (const f of acceptedFindings.value) {
      const d = (f.detail_json || {}) as Record<string, unknown>
      const preset = d.target_project_name || d.expected_name || d.replacement
      if (typeof preset === 'string' && preset.trim()) {
        replacementDrafts[f.id] = preset.trim()
      }
    }
  } finally {
    loadingAccepted.value = false
  }
}

async function handleExportCleanBid(): Promise<void> {
  if (!cleanTaskId.value || exportingClean.value) return
  exportingClean.value = true
  try {
    const reps: Record<number, string> = {}
    for (const f of acceptedFindings.value) {
      const v = (replacementDrafts[f.id] || '').trim()
      if (v) {
        reps[f.id] = v
      }
    }
    const res = await reportsApi.exportCleanBid(cleanTaskId.value, { replacements: reps })
    cleanResult.value = res
    await reportsApi.downloadCleanBid(cleanTaskId.value, res.output_filename)
    ElMessage.success(
      `修订定稿导出成功：原位替换 ${res.applied_count} 处，待人工补写 ${res.manual_pending_count} 项`,
    )
  } finally {
    exportingClean.value = false
  }
}

watch(cleanTaskId, (nv) => {
  void loadAcceptedForClean(nv)
})

onMounted(async () => {
  if (route.query.tab === 'clean') {
    mainTab.value = 'clean'
  }
  const qTaskId = Number(route.query.taskId)
  if (Number.isFinite(qTaskId) && qTaskId > 0) {
    cleanTaskId.value = qTaskId
    selectedSourceTaskId.value = qTaskId
  }
  await Promise.all([loadDrafts(), loadTasks()])
  if (cleanTaskId.value) {
    await loadAcceptedForClean(cleanTaskId.value)
  }
})
</script>

<template>
  <div class="composer-page">
    <div class="page-intro">
      <div>
        <h2>标书编制输出与无损修订中心（F8）</h2>
        <p class="sub-desc">
          正向按招标文件与企业证照台账装配《投标文件初稿骨架.docx》（报价栏强制人工填报占位，V2 可接参考方案库）；后期支持将体检已采纳整改项原位无损导出 Clean 干净定稿版。
        </p>
      </div>
    </div>

    <el-tabs v-model="mainTab" class="composer-tabs">
      <!-- ── Tab 1: 正向智能编制初稿 ───────────────────────────────────── -->
      <el-tab-pane label="智能编制初稿（V1 骨架+填表 / V2 方案库）" name="compose">
        <el-card shadow="never" class="setup-card">
          <div class="setup-grid">
            <div class="setup-col">
              <div class="field-label">1. 招标依据来源</div>
              <el-radio-group v-model="tenderSourceType" size="small" class="mb-8">
                <el-radio value="upload">直接上传招标文件</el-radio>
                <el-radio value="task">复用已有体检任务</el-radio>
              </el-radio-group>

              <div v-if="tenderSourceType === 'upload'" class="upload-box">
                <input
                  type="file"
                  accept=".docx,.pdf,.txt"
                  @change="handleTenderFileChange"
                />
                <div v-if="tenderFile" class="file-picked">已选择：{{ tenderFile.name }}</div>
                <div v-else class="file-hint">支持 .docx / .pdf / .txt，自动提取限价/开标日/★▲/资格证照/评分细则</div>
              </div>
              <div v-else>
                <el-select
                  v-model="selectedSourceTaskId"
                  placeholder="请选择一个已有体检任务"
                  style="width: 100%"
                  filterable
                >
                  <el-option
                    v-for="t in taskList"
                    :key="t.id"
                    :label="`#${t.id} ${t.title}（${t.tender_filename}）`"
                    :value="t.id"
                  />
                </el-select>
              </div>

              <div class="row-inputs">
                <el-input
                  v-model="composeTitle"
                  placeholder="初稿标题（可选，默认自动带出招标项目名）"
                  clearable
                />
                <el-input-number
                  v-model="packageNo"
                  :min="1"
                  :max="99"
                  placeholder="分包号(可选)"
                  controls-position="right"
                />
              </div>
            </div>

            <div class="setup-col">
              <div class="field-label">2. 编制模式与方案库参考（V1 / V2）</div>
              <el-radio-group v-model="composeMode" class="mode-radios">
                <el-radio value="skeleton_v1">
                  V1 正向骨架初稿（0 幻觉确定性装配，缺项标黄占位，报价严禁 AI 填报）
                </el-radio>
                <el-radio value="knowledge_draft_v2">
                  V2 附企业参考方案库片段（按评分细则局部检索历史方案，无参考文件自动回退大纲）
                </el-radio>
              </el-radio-group>

              <div v-if="composeMode === 'knowledge_draft_v2'" class="knowledge-box">
                <div class="kb-head">
                  <span>上传历史优秀标书 / 企业方案库切片（可选 .docx/.pdf/.txt）</span>
                  <input
                    type="file"
                    multiple
                    accept=".docx,.pdf,.txt"
                    @change="handleKnowledgeFilesChange"
                  />
                </div>
                <div v-if="knowledgeFiles.length > 0" class="kb-list">
                  <el-tag
                    v-for="(kf, idx) in knowledgeFiles"
                    :key="`${kf.name}-${idx}`"
                    closable
                    @close="removeKnowledgeFile(idx)"
                  >
                    {{ kf.name }}
                  </el-tag>
                </div>
                <div v-else class="file-hint">未上传参考文件时将安全回退至 V1 评分大纲，绝不编造参数</div>
              </div>

              <div class="action-bar">
                <el-button
                  type="primary"
                  :loading="generating"
                  @click="handleGenerateDraft"
                >
                  生成《投标文件初稿骨架.docx》
                </el-button>
              </div>
            </div>
          </div>
        </el-card>

        <div class="workspace-split">
          <!-- 左侧草稿历史列表 -->
          <el-card shadow="never" class="history-pane" v-loading="loadingDrafts">
            <template #header>
              <div class="pane-head">
                <span>历史编制草稿箱（{{ draftList.length }}）</span>
                <el-button size="small" text @click="loadDrafts">刷新</el-button>
              </div>
            </template>
            <el-empty v-if="draftList.length === 0" description="暂无编制草稿，请在上方发起生成" />
            <div v-else class="draft-list">
              <div
                v-for="item in draftList"
                :key="item.id"
                class="draft-item"
                :class="{ active: activeDraft?.id === item.id }"
                @click="openDraftDetail(item.id)"
              >
                <div class="draft-title">{{ item.title }}</div>
                <div class="draft-meta">
                  <el-tag size="small" :type="item.scoring_fill_mode === 'rag_knowledge' ? 'success' : 'info'">
                    {{ item.scoring_fill_mode === 'rag_knowledge' ? 'V2 方案参考' : 'V1 骨架' }}
                  </el-tag>
                  <span>待填 {{ item.manual_item_count }} 项</span>
                </div>
                <div class="draft-actions" @click.stop>
                  <el-button size="small" type="primary" link @click="handleDownloadDraft(item)">下载 Word</el-button>
                  <el-button size="small" type="danger" link @click="handleDeleteDraft(item)">删除</el-button>
                </div>
              </div>
            </div>
          </el-card>

          <!-- 右侧蓝图详情与待人工填报高亮区 -->
          <el-card shadow="never" class="detail-pane">
            <el-empty v-if="!activeBlueprint" description="请选择左侧草稿或生成新的标书初稿骨架" />
            <template v-else>
              <div class="bp-header">
                <div>
                  <h3>{{ activeBlueprint.project_name }}</h3>
                  <div class="bp-sub">
                    编号：{{ activeBlueprint.project_code }} ｜ 开标日：{{ activeBlueprint.open_date || '待人工核对' }} ｜ 参考限价：{{ activeBlueprint.price_cap ? `${activeBlueprint.price_cap} 元` : '未识别/待核对' }}
                  </div>
                </div>
                <el-button
                  v-if="activeDraft"
                  type="success"
                  @click="handleDownloadDraft(activeDraft)"
                >
                  下载初稿骨架 (.docx)
                </el-button>
              </div>

              <!-- 人工待填报清单高亮摘要 -->
              <el-alert
                type="warning"
                :closable="false"
                show-icon
                class="manual-alert"
                :title="`共 ${activeBlueprint.manual_checklist.length} 项待人工填报与终核确认（点击定位对应章节）`"
              >
                <div class="manual-tags">
                  <el-tag
                    v-for="(mi, idx) in activeBlueprint.manual_checklist"
                    :key="idx"
                    type="danger"
                    effect="plain"
                    class="manual-tag"
                    @click="jumpToSection(mi.section_id)"
                  >
                    {{ mi.field }}
                  </el-tag>
                </div>
              </el-alert>

              <el-collapse v-model="expandedSections" class="bp-sections">
                <el-collapse-item
                  v-for="sec in activeBlueprint.sections"
                  :key="sec.section_id"
                  :name="sec.section_id"
                  :id="`composer-sec-${sec.section_id}`"
                  :class="{ 'sec-highlighted': highlightedSectionId === sec.section_id }"
                >
                  <template #title>
                    <div class="sec-title-row">
                      <span>{{ sec.title }}</span>
                      <el-tag size="small" effect="plain">
                        {{ sec.fill_mode === 'rag_knowledge' ? 'V2 方案片段已吸附' : sec.fill_mode }}
                      </el-tag>
                    </div>
                  </template>

                  <!-- Cover & Price KV -->
                  <template v-if="sec.slot_type === 'cover_and_letter' || sec.slot_type === 'price_placeholder'">
                    <el-table
                      :data="sec.content_blocks.filter((b) => b.kind !== 'paragraph')"
                      size="small"
                      border
                    >
                      <el-table-column prop="label" label="字段" width="210" />
                      <el-table-column label="装配值 / 占位提示">
                        <template #default="{ row }">
                          <span :class="{ 'highlight-manual-text': Boolean(row.highlight) }">
                            {{ row.value }}
                          </span>
                        </template>
                      </el-table-column>
                    </el-table>
                    <p
                      v-for="(pBlock, pi) in sectionBlocks(sec, 'paragraph')"
                      :key="pi"
                      class="bp-para"
                      :class="{ 'highlight-manual-text': Boolean(pBlock.highlight) }"
                    >
                      {{ pBlock.text }}
                    </p>
                  </template>

                  <!-- Qualification Checklist -->
                  <template v-else-if="sec.slot_type === 'qualification_checklist'">
                    <el-table :data="sectionBlocks(sec, 'cert_row')" size="small" border>
                      <el-table-column prop="cert_name" label="招标所需证照" min-width="180" />
                      <el-table-column prop="requirement_level" label="级别" width="100" />
                      <el-table-column prop="valid_until" label="台账有效期" width="120" />
                      <el-table-column label="对账结果" min-width="240">
                        <template #default="{ row }">
                          <span :class="{ 'highlight-manual-text': row.ledger_status !== 'valid' }">
                            {{ row.display_status }}
                          </span>
                        </template>
                      </el-table-column>
                    </el-table>
                  </template>

                  <!-- Deviation Table -->
                  <template v-else-if="sec.slot_type === 'deviation_table'">
                    <el-table :data="sectionBlocks(sec, 'deviation_row')" size="small" border>
                      <el-table-column prop="clause_no" label="条款号" width="110" />
                      <el-table-column prop="tender_requirement" label="招标实质要求" min-width="220" />
                      <el-table-column prop="bid_response" label="我方响应占位 / 核验参考" min-width="220" />
                      <el-table-column prop="deviation" label="状态" width="130" />
                    </el-table>
                  </template>

                  <!-- Scoring Outline & V2 RAG -->
                  <template v-else-if="sec.slot_type === 'scoring_response_outline'">
                    <div
                      v-for="(tp, tidx) in sectionBlocks(sec, 'scoring_topic')"
                      :key="tidx"
                      class="topic-card"
                    >
                      <div class="topic-head">
                        <strong>{{ tp.title }}</strong>
                        <el-tag size="small" type="warning">{{ tp.score_points }}</el-tag>
                      </div>
                      <div v-if="tp.tender_quote" class="topic-quote">招标依据：{{ tp.tender_quote }}</div>
                      <ul class="topic-outline">
                        <li v-for="(line, li) in (tp.sub_outline as string[])" :key="li">{{ line }}</li>
                      </ul>
                      <div
                        v-for="(sn, sidx) in ((tp.reference_snippets as Array<Record<string, unknown>>) || [])"
                        :key="sidx"
                        class="rag-snippet-box"
                      >
                        <div class="rag-notice">{{ sn.notice }}</div>
                        <div class="rag-body">{{ sn.snippet }}</div>
                      </div>
                    </div>
                  </template>

                  <!-- Appendix -->
                  <template v-else>
                    <el-table :data="sectionBlocks(sec, 'ledger_appendix_row')" size="small" border>
                      <el-table-column prop="cert_name" label="台账证照名称" min-width="180" />
                      <el-table-column prop="cert_no" label="证号" width="150" />
                      <el-table-column prop="valid_until" label="效期" width="120" />
                      <el-table-column prop="attachment_name" label="原件附件" min-width="160" />
                    </el-table>
                  </template>
                </el-collapse-item>
              </el-collapse>
            </template>
          </el-card>
        </div>
      </el-tab-pane>

      <!-- ── Tab 2: 采纳整改导定稿（Clean 无损修订导出）───────────────── -->
      <el-tab-pane label="采纳整改导定稿（无损修订版）" name="clean">
        <el-card shadow="never" class="setup-card" v-loading="loadingTasks">
          <div class="clean-toolbar">
            <div class="clean-picker">
              <span class="field-label">选择关联的体检任务：</span>
              <el-select
                v-model="cleanTaskId"
                placeholder="请选择已体检任务"
                filterable
                style="width: 380px"
              >
                <el-option
                  v-for="t in taskList"
                  :key="t.id"
                  :label="`#${t.id} ${t.title}（${t.bid_filename}）`"
                  :value="t.id"
                />
              </el-select>
            </div>
            <el-button
              type="primary"
              :disabled="!cleanTaskId || cleanIsPdfTask"
              :loading="exportingClean"
              @click="handleExportCleanBid"
            >
              生成并下载 Clean 修订定稿 (.docx)
            </el-button>
          </div>

          <el-alert
            v-if="cleanIsPdfTask"
            type="error"
            :closable="false"
            show-icon
            title="当前任务投标文件为 PDF 格式（PDF 原件受合规与签章完整性保护，禁止自动改写；请在 Word .docx 原件中修改后重新上传体检）"
            class="mt-12"
          />
          <el-alert
            v-else
            type="info"
            :closable="false"
            show-icon
            title="仅对在体检详情点击过「采纳整改」的发现执行安全原位替换，自动清除全文 w:comment 批注标记；大段漏应答若未填替换正文将诚实列入待人工补写清单，绝不编造篡改原排版。"
            class="mt-12"
          />
        </el-card>

        <el-card shadow="never" class="mt-12" v-loading="loadingAccepted">
          <template #header>
            <div class="pane-head">
              <span>已采纳整改项（{{ acceptedFindings.length }} 条）</span>
              <el-button v-if="cleanTaskId" size="small" text @click="loadAcceptedForClean(cleanTaskId)">
                刷新列表
              </el-button>
            </div>
          </template>
          <el-empty
            v-if="acceptedFindings.length === 0"
            description="该任务暂无标为「已采纳整改」的发现（请先在 F2-F4 体检详情页采纳对应问题）"
          />
          <el-table v-else :data="acceptedFindings" border size="small">
            <el-table-column prop="id" label="ID" width="70" />
            <el-table-column prop="code" label="规则代码" width="150" />
            <el-table-column prop="clause_no" label="条款" width="110" />
            <el-table-column label="待替换原文靶标" min-width="220">
              <template #default="{ row }">
                <div class="quote-preview">{{ findOldTargetForFinding(row) || '（无明确原文定位，需人工补页）' }}</div>
              </template>
            </el-table-column>
            <el-table-column label="安全替换目标文本（留空则安全计入待人工清单）" min-width="260">
              <template #default="{ row }">
                <el-input
                  v-model="replacementDrafts[row.id]"
                  :placeholder="
                    row.code === 'MISSING_RESPONSE'
                      ? '大段漏应答建议线下补写（可留空安全跳过）'
                      : '输入替换后的准确文本（例如本项目正式名称）'
                  "
                  clearable
                  size="small"
                />
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card v-if="cleanResult" shadow="never" class="mt-12">
          <template #header>
            <div class="pane-head">
              <span>
                本次无损修订导出回执：已原位替换 {{ cleanResult.applied_count }} 处 ｜ 尚待人工补写 {{ cleanResult.manual_pending_count }} 项
              </span>
              <el-button
                size="small"
                type="success"
                @click="cleanTaskId && reportsApi.downloadCleanBid(cleanTaskId, cleanResult.output_filename)"
              >
                再次下载定稿 Word
              </el-button>
            </div>
          </template>
          <el-table
            v-if="cleanResult.applied_items.length > 0"
            :data="cleanResult.applied_items"
            border
            size="small"
            class="mb-12"
          >
            <el-table-column prop="code" label="已替换规则" width="160" />
            <el-table-column prop="old_text" label="原片段" min-width="200" />
            <el-table-column prop="new_text" label="替换为" min-width="200" />
            <el-table-column prop="occurrences" label="命中次数" width="90" />
          </el-table>

          <div v-if="cleanResult.manual_pending_items.length > 0">
            <div class="field-label">尚待人工补写 / 核对事项清单：</div>
            <el-table :data="cleanResult.manual_pending_items" border size="small">
              <el-table-column prop="code" label="规则代码" width="160" />
              <el-table-column prop="clause_no" label="条款" width="110" />
              <el-table-column prop="reason" label="跳过原因（宁缺毋滥保护）" min-width="220" />
              <el-table-column prop="suggestion" label="整改建议" min-width="220" />
            </el-table>
          </div>
        </el-card>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.composer-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.page-intro h2 {
  margin: 0 0 6px;
  font-size: 20px;
  color: #0f172a;
}

.sub-desc {
  margin: 0;
  font-size: 13px;
  color: #475569;
  line-height: 1.5;
}

.setup-card {
  border: 1px solid #e2e8f0;
}

.setup-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: 20px;
}

.setup-col {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.field-label {
  font-size: 13px;
  font-weight: 600;
  color: #1e293b;
}

.upload-box,
.knowledge-box {
  padding: 10px 12px;
  border: 1px dashed #cbd5e1;
  border-radius: 6px;
  background: #f8fafc;
}

.file-picked {
  margin-top: 6px;
  font-size: 12px;
  color: #0f766e;
  font-weight: 600;
}

.file-hint {
  margin-top: 6px;
  font-size: 12px;
  color: #64748b;
}

.row-inputs {
  display: flex;
  gap: 10px;
  align-items: center;
}

.mode-radios {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
}

.kb-head {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  color: #334155;
}

.kb-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

.action-bar {
  display: flex;
  justify-content: flex-end;
  margin-top: auto;
}

.workspace-split {
  display: grid;
  grid-template-columns: 290px minmax(0, 1fr);
  gap: 14px;
  align-items: start;
}

.history-pane {
  max-height: 74vh;
  overflow-y: auto;
}

.pane-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: 600;
}

.draft-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.draft-item {
  padding: 10px;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  cursor: pointer;
  transition: border-color 0.15s ease, background-color 0.15s ease;
}

.draft-item:hover {
  border-color: #94a3b8;
}

.draft-item.active {
  border-color: #2563eb;
  background: #eff6ff;
}

.draft-title {
  font-size: 13px;
  font-weight: 600;
  color: #0f172a;
  margin-bottom: 6px;
  word-break: break-all;
}

.draft-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
  color: #64748b;
  margin-bottom: 4px;
}

.draft-actions {
  display: flex;
  gap: 10px;
}

.bp-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 12px;
}

.bp-header h3 {
  margin: 0 0 4px;
  font-size: 17px;
  color: #0f172a;
}

.bp-sub {
  font-size: 12px;
  color: #475569;
}

.manual-alert {
  margin-bottom: 12px;
}

.manual-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

.manual-tag {
  cursor: pointer;
}

.sec-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}

.sec-highlighted {
  outline: 2px solid #f59e0b;
  border-radius: 4px;
}

.highlight-manual-text {
  background-color: #fef3c7;
  color: #b91c1c;
  padding: 1px 4px;
  border-radius: 3px;
  font-weight: 600;
}

.bp-para {
  margin: 10px 0 0;
  font-size: 13px;
  line-height: 1.6;
}

.topic-card {
  padding: 10px 12px;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  margin-bottom: 10px;
}

.topic-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.topic-quote {
  font-size: 12px;
  color: #475569;
  margin-bottom: 6px;
}

.topic-outline {
  margin: 0 0 8px;
  padding-left: 18px;
  font-size: 13px;
  color: #1e293b;
}

.rag-snippet-box {
  background: #f8fafc;
  border-left: 3px solid #f59e0b;
  padding: 8px 10px;
  margin-top: 6px;
  font-size: 12px;
}

.rag-notice {
  color: #b45309;
  font-weight: 600;
  margin-bottom: 4px;
}

.rag-body {
  color: #334155;
  white-space: pre-wrap;
}

.clean-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}

.clean-picker {
  display: flex;
  align-items: center;
  gap: 8px;
}

.quote-preview {
  font-size: 12px;
  color: #334155;
  white-space: pre-wrap;
}

.mb-8 {
  margin-bottom: 8px;
}

.mb-12 {
  margin-bottom: 12px;
}

.mt-12 {
  margin-top: 12px;
}
</style>
