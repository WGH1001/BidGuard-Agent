<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { checksApi, composerApi, reportsApi, validateUploadFile } from '@/api'
import type {
  BidFindingOut,
  BidTaskOut,
  BizLineIndicatorOverviewOut,
  BizLineType,
  CleanBidExportOut,
  ComposeBlueprintData,
  ComposeBlueprintSection,
  ComposeDraftDetailOut,
  ComposeDraftSummaryOut,
  ComposeManualItem,
  ComposeModeType,
  ComposerSkillOut,
  SkillImportPreviewOut,
  SkillLayoutSpec,
} from '@/api/types'

defineOptions({ name: 'BidComposerView' })

const route = useRoute()
const mainTab = ref<'compose' | 'clean'>('compose')

// ── Skills 状态与 GitHub/Gitee/本地导入弹窗 ───────────────────────────────
const skillList = ref<ComposerSkillOut[]>([])
const selectedSkillId = ref<number | undefined>(undefined)
const skillModalVisible = ref(false)
const skillModalTab = ref<'list' | 'edit' | 'import'>('list')
const editingSkillId = ref<number | null>(null)
const savingSkill = ref(false)

const skillForm = reactive<{
  name: string
  description: string
  biz_line: BizLineType
  writing_instructions: string
  is_default: boolean
  layout_spec: SkillLayoutSpec
}>({
  name: '',
  description: '',
  biz_line: 'general',
  writing_instructions: '',
  is_default: false,
  layout_spec: {
    cover_style: 'gov_clean',
    font_zh: '仿宋',
    heading_font_zh: '黑体',
    font_size_pt: 11.5,
    line_spacing: 1.35,
    accent_color_hex: '0F172A',
    table_header_bg: 'F1F5F9',
    include_toc: true,
    append_checklist_at_end: false,
    target_section_words: 500,
    tone: 'formal_gov',
  },
})

const gitUrlInput = ref('')
const allowGiteeFallback = ref(true)
const previewingUrl = ref(false)
const importingUrl = ref(false)
const urlPreview = ref<SkillImportPreviewOut | null>(null)
const urlImportError = ref('')

// ── Tab 1: 正向智能编制初稿状态 ───────────────────────────────────────────
const tenderSourceType = ref<'upload' | 'task'>('upload')
const tenderFile = ref<File | null>(null)
const selectedSourceTaskId = ref<number | undefined>(undefined)
const composeTitle = ref('')
const packageNo = ref<number | undefined>(undefined)
const composeMode = ref<ComposeModeType>('skeleton_v1')
const useAi = ref(true)
const useIndicatorLibrary = ref(false)
const indicatorOverview = ref<BizLineIndicatorOverviewOut | null>(null)
const loadingIndicators = ref(false)
const showIndicatorPreview = ref(false)
const knowledgeFiles = ref<File[]>([])
const generating = ref(false)
const cancellingDraft = ref(false)

const loadingDrafts = ref(false)
const draftList = ref<ComposeDraftSummaryOut[]>([])
const activeDraft = ref<ComposeDraftDetailOut | null>(null)
const showManualDrawer = ref(false)
const expandedSections = ref<string[]>([
  'sec_cover',
  'sec_qual',
  'sec_price',
  'sec_deviation',
  'sec_scoring',
])
const highlightedSectionId = ref<string>('')

let draftPollTimer: number | null = null

// ── Tab 2: 采纳整改导定稿状态 ─────────────────────────────────────────────
const loadingTasks = ref(false)
const taskList = ref<BidTaskOut[]>([])
const cleanTaskId = ref<number | undefined>(undefined)
const loadingAccepted = ref(false)
const acceptedFindings = ref<BidFindingOut[]>([])
const replacementDrafts = reactive<Record<number, string>>({})
const exportingClean = ref(false)
const cleanResult = ref<CleanBidExportOut | null>(null)

const selectedSkill = computed<ComposerSkillOut | null>(() => {
  if (!selectedSkillId.value) return null
  return skillList.value.find((s) => s.id === selectedSkillId.value) || null
})

function bizLineLabel(line?: string | null): string {
  if (line === 'security') return '网络安全与数据安全'
  if (line === 'maas') return '并行科技 MaaS 平台'
  return '通用政采'
}

function bizLineTagType(line?: string | null): 'info' | 'danger' | 'primary' {
  if (line === 'security') return 'danger'
  if (line === 'maas') return 'primary'
  return 'info'
}

async function loadIndicatorOverview(bizLine: BizLineType = 'general'): Promise<void> {
  loadingIndicators.value = true
  try {
    indicatorOverview.value = await composerApi.listIndicators(bizLine)
  } finally {
    loadingIndicators.value = false
  }
}

const activeBlueprint = computed<ComposeBlueprintData | null>(() => {
  if (!activeDraft.value || !activeDraft.value.blueprint) return null
  const bp = activeDraft.value.blueprint as ComposeBlueprintData
  if (!bp || !Array.isArray(bp.sections)) return null
  return bp
})

const manualGroupStats = computed(() => {
  const items: ComposeManualItem[] = activeBlueprint.value?.manual_checklist || []
  let qual = 0
  let price = 0
  let deviation = 0
  let scoring = 0
  let cover = 0
  for (const m of items) {
    if (m.section_id === 'sec_qual') qual += 1
    else if (m.section_id === 'sec_price') price += 1
    else if (m.section_id === 'sec_deviation') deviation += 1
    else if (m.section_id === 'sec_scoring') scoring += 1
    else cover += 1
  }
  return { total: items.length, cover, qual, price, deviation, scoring }
})

const cleanSelectedTask = computed<BidTaskOut | null>(() => {
  if (cleanTaskId.value === undefined) return null
  return taskList.value.find((t) => t.id === cleanTaskId.value) || null
})

const cleanIsPdfTask = computed<boolean>(() => {
  const fn = (cleanSelectedTask.value?.bid_filename || '').toLowerCase()
  return fn.endsWith('.pdf')
})

function isRunningStatus(st?: string | null): boolean {
  return st === 'queued' || st === 'running'
}

function stopDraftPoll(): void {
  if (draftPollTimer !== null) {
    window.clearTimeout(draftPollTimer)
    draftPollTimer = null
  }
}

function scheduleDraftPoll(draftId: number): void {
  stopDraftPoll()
  draftPollTimer = window.setTimeout(async () => {
    try {
      const detail = await composerApi.getDraft(draftId)
      if (activeDraft.value?.id === draftId || !activeDraft.value) {
        activeDraft.value = detail
      }
      const listRes = await composerApi.listDrafts()
      draftList.value = listRes.items || []
      if (isRunningStatus(detail.status)) {
        scheduleDraftPoll(draftId)
      } else if (detail.status === 'completed') {
        ElMessage.success(`标书初稿生成完成：${detail.title}`)
      }
    } catch {
      stopDraftPoll()
    }
  }, 1500)
}

async function loadSkills(): Promise<void> {
  const res = await composerApi.listSkills()
  skillList.value = res.items || []
  if (
    !selectedSkillId.value ||
    !skillList.value.some((s) => s.id === selectedSkillId.value)
  ) {
    const def = skillList.value.find((s) => s.is_default) || skillList.value[0]
    selectedSkillId.value = def?.id
  }
}

function openSkillModal(tab: 'list' | 'edit' | 'import' = 'list'): void {
  skillModalTab.value = tab
  if (tab === 'edit' && editingSkillId.value === null) {
    resetSkillForm()
  }
  skillModalVisible.value = true
}

function resetSkillForm(): void {
  editingSkillId.value = null
  skillForm.name = ''
  skillForm.description = ''
  skillForm.biz_line = 'general'
  skillForm.writing_instructions = ''
  skillForm.is_default = false
  skillForm.layout_spec = {
    cover_style: 'gov_clean',
    font_zh: '仿宋',
    heading_font_zh: '黑体',
    font_size_pt: 11.5,
    line_spacing: 1.35,
    accent_color_hex: '0F172A',
    table_header_bg: 'F1F5F9',
    include_toc: true,
    append_checklist_at_end: false,
    target_section_words: 500,
    tone: 'formal_gov',
  }
}

function startEditSkill(sk: ComposerSkillOut): void {
  editingSkillId.value = sk.id
  skillForm.name = sk.name
  skillForm.description = sk.description
  skillForm.biz_line = (sk.biz_line || 'general') as BizLineType
  skillForm.writing_instructions = sk.writing_instructions
  skillForm.is_default = sk.is_default
  skillForm.layout_spec = { ...sk.layout_spec }
  skillModalTab.value = 'edit'
}

async function handleSaveSkill(): Promise<void> {
  if (!skillForm.name.trim() || !skillForm.writing_instructions.trim()) {
    ElMessage.warning('请填写 Skill 名称和写作约束指令')
    return
  }
  savingSkill.value = true
  try {
    const payload = {
      name: skillForm.name.trim(),
      description: skillForm.description.trim(),
      biz_line: skillForm.biz_line,
      writing_instructions: skillForm.writing_instructions.trim(),
      layout_spec: { ...skillForm.layout_spec },
      is_default: skillForm.is_default,
    }
    const saved = editingSkillId.value
      ? await composerApi.updateSkill(editingSkillId.value, payload)
      : await composerApi.createSkill(payload)
    ElMessage.success(`Skill「${saved.name}」已保存`)
    await loadSkills()
    selectedSkillId.value = saved.id
    resetSkillForm()
    skillModalTab.value = 'list'
  } finally {
    savingSkill.value = false
  }
}

async function handleDeleteSkill(sk: ComposerSkillOut): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认删除写作 Skill「${sk.name}」吗？`, '删除 Skill', {
      type: 'warning',
      confirmButtonText: '确认删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  await composerApi.deleteSkill(sk.id)
  ElMessage.success('已删除 Skill')
  await loadSkills()
}

async function handlePreviewSkillUrl(): Promise<void> {
  if (!gitUrlInput.value.trim() || previewingUrl.value) return
  previewingUrl.value = true
  urlImportError.value = ''
  urlPreview.value = null
  try {
    urlPreview.value = await composerApi.previewSkillUrl(
      gitUrlInput.value.trim(),
      allowGiteeFallback.value,
    )
    if (urlPreview.value.used_fallback) {
      ElMessage.info('GitHub 未直接命中，已自动降级从 Gitee 拉取预览')
    }
  } catch (err: unknown) {
    const msg =
      (err as { response?: { data?: { detail?: { message?: string } } } })?.response?.data?.detail
        ?.message || '外部托管拉取失败，请改用本地 .md/.json 文件导入或手动新建'
    urlImportError.value = msg
  } finally {
    previewingUrl.value = false
  }
}

async function handleConfirmImportUrl(): Promise<void> {
  if (!gitUrlInput.value.trim() || importingUrl.value) return
  importingUrl.value = true
  try {
    const created = await composerApi.importSkillUrl(
      gitUrlInput.value.trim(),
      allowGiteeFallback.value,
    )
    ElMessage.success(`已导入 Skill：${created.name}`)
    gitUrlInput.value = ''
    urlPreview.value = null
    urlImportError.value = ''
    await loadSkills()
    selectedSkillId.value = created.id
    skillModalTab.value = 'list'
  } finally {
    importingUrl.value = false
  }
}

async function handleLocalSkillFileChange(ev: Event): Promise<void> {
  const input = ev.target as HTMLInputElement
  const f = input.files?.[0]
  input.value = ''
  if (!f) return
  const created = await composerApi.importSkillFile(f)
  ElMessage.success(`已从本地文件导入 Skill：${created.name}`)
  await loadSkills()
  selectedSkillId.value = created.id
  skillModalTab.value = 'list'
}

function handleTenderFileChange(ev: Event): void {
  const input = ev.target as HTMLInputElement
  const f = input.files?.[0]
  input.value = ''
  if (!f) return
  const chk = validateUploadFile(f)
  if (!chk.valid) {
    ElMessage.warning(chk.message)
    return
  }
  tenderFile.value = f
}

function handleKnowledgeFilesChange(ev: Event): void {
  const input = ev.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  for (const f of files) {
    const chk = validateUploadFile(f)
    if (!chk.valid) {
      ElMessage.warning(chk.message)
      continue
    }
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
    draftList.value = res.items || []
    const runningItem = draftList.value.find((d) => isRunningStatus(d.status))
    if (!activeDraft.value && draftList.value.length > 0) {
      await openDraftDetail(draftList.value[0].id)
    } else if (runningItem) {
      scheduleDraftPoll(runningItem.id)
    }
  } finally {
    loadingDrafts.value = false
  }
}

async function openDraftDetail(draftId: number): Promise<void> {
  const d = await composerApi.getDraft(draftId)
  activeDraft.value = d
  highlightedSectionId.value = ''
  if (isRunningStatus(d.status)) {
    scheduleDraftPoll(d.id)
  }
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
      skillId: selectedSkillId.value,
      useAi: useAi.value,
      useIndicatorLibrary: useIndicatorLibrary.value,
      asyncRun: true,
    })
    activeDraft.value = detail
    const listRes = await composerApi.listDrafts()
    draftList.value = listRes.items || []
    if (isRunningStatus(detail.status)) {
      ElMessage.info('标书编制任务已在后台启动，切换其他页面不会中断')
      scheduleDraftPoll(detail.id)
    } else {
      ElMessage.success('标书初稿生成完成')
    }
  } finally {
    generating.value = false
  }
}

async function handleCancelDraft(): Promise<void> {
  if (!activeDraft.value || cancellingDraft.value) return
  cancellingDraft.value = true
  try {
    await composerApi.cancelDraft(activeDraft.value.id)
    stopDraftPoll()
    await openDraftDetail(activeDraft.value.id)
    const listRes = await composerApi.listDrafts()
    draftList.value = listRes.items || []
    ElMessage.warning('已中断标书编制任务')
  } finally {
    cancellingDraft.value = false
  }
}

async function handleDownloadDraft(item: { id: number; title: string }): Promise<void> {
  await composerApi.downloadDraft(item.id, `${item.title}_投标文件初稿.docx`)
  ElMessage.success('已下载《投标文件初稿.docx》')
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

watch(
  selectedSkill,
  (sk) => {
    const bLine = (sk?.biz_line || 'general') as BizLineType
    useIndicatorLibrary.value = bLine !== 'general'
    void loadIndicatorOverview(bLine)
  },
  { immediate: true },
)

onMounted(async () => {
  if (route.query.tab === 'clean') {
    mainTab.value = 'clean'
  }
  const qTaskId = Number(route.query.taskId)
  if (Number.isFinite(qTaskId) && qTaskId > 0) {
    cleanTaskId.value = qTaskId
    selectedSourceTaskId.value = qTaskId
  }
  await Promise.all([loadSkills(), loadDrafts(), loadTasks()])
  if (cleanTaskId.value) {
    await loadAcceptedForClean(cleanTaskId.value)
  }
})

onUnmounted(() => {
  stopDraftPoll()
})
</script>

<template>
  <div class="composer-page">
    <div class="page-intro">
      <div>
        <h2>标书编制输出与无损修订中心（F8）</h2>
        <p class="sub-desc">
          由「写作 Skill 规范 + AI 动态构章撰稿 + 报价/证照/覆盖三锁」驱动生成独立封面、目录与投标初稿（.docx），支持从 GitHub/Gitee 或本地导入 Skills；后期支持将已采纳整改无损导出 Clean 定稿。
        </p>
      </div>
    </div>

    <el-tabs v-model="mainTab" class="composer-tabs">
      <!-- ── Tab 1: 正向智能编制初稿 ───────────────────────────────────── -->
      <el-tab-pane label="AI + Skill 智能编制初稿" name="compose">
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
                  placeholder="初稿标题（可选，默认带出招标项目名）"
                  clearable
                />
                <el-input-number
                  v-model="packageNo"
                  :min="1"
                  :max="99"
                  placeholder="分包"
                  controls-position="right"
                />
              </div>
            </div>

            <div class="setup-col">
              <div class="skill-row-head">
                <span class="field-label">2. 写作风格与版式 Skill + AI 选项</span>
                <el-button size="small" type="primary" link @click="openSkillModal('list')">
                  管理 / GitHub·Gitee·本地导入 Skills
                </el-button>
              </div>

              <div class="skill-picker-bar">
                <el-select v-model="selectedSkillId" placeholder="选择写作 Skill" style="flex: 1">
                  <el-option
                    v-for="sk in skillList"
                    :key="sk.id"
                    :label="`${sk.name}（${bizLineLabel(sk.biz_line)}）${sk.is_default ? '· 默认' : ''}`"
                    :value="sk.id"
                  >
                    <div class="skill-opt-row">
                      <span>{{ sk.name }}{{ sk.is_default ? '（默认）' : '' }}</span>
                      <el-tag size="small" :type="bizLineTagType(sk.biz_line)" effect="plain">
                        {{ bizLineLabel(sk.biz_line) }}
                      </el-tag>
                    </div>
                  </el-option>
                </el-select>
                <el-checkbox v-model="useAi">启用 AI 动态构章撰稿</el-checkbox>
              </div>
              <div v-if="selectedSkill" class="skill-summary-line">
                <el-tag size="small" :type="bizLineTagType(selectedSkill.biz_line)" class="mr-6">
                  {{ bizLineLabel(selectedSkill.biz_line) }}
                </el-tag>
                {{ selectedSkill.description }}（{{ selectedSkill.layout_spec.font_zh }} {{ selectedSkill.layout_spec.font_size_pt }}pt · 行距 {{ selectedSkill.layout_spec.line_spacing }} · 目标 {{ selectedSkill.layout_spec.target_section_words }} 字/节）
              </div>

              <div class="indicator-bar">
                <div class="ind-switch-wrap">
                  <el-switch v-model="useIndicatorLibrary" size="small" />
                  <span>引用公司常见指标与业务线标准应答库</span>
                </div>
                <el-button
                  size="small"
                  link
                  type="primary"
                  :loading="loadingIndicators"
                  @click="showIndicatorPreview = !showIndicatorPreview"
                >
                  {{
                    showIndicatorPreview
                      ? '收起核心指标与评分参考'
                      : `预览核心指标与评分标准参考（${indicatorOverview?.total_indicators || 0} 项）`
                  }}
                </el-button>
              </div>

              <div v-if="showIndicatorPreview && indicatorOverview" class="indicator-preview-box">
                <div class="ind-pv-head">
                  <strong>{{ indicatorOverview.biz_line_label }} · 核心指标与评分参考</strong>
                  <el-tag
                    size="small"
                    :type="indicatorOverview.review_status === 'reviewed' ? 'success' : 'warning'"
                    effect="plain"
                  >
                    {{ indicatorOverview.review_status === 'reviewed' ? '已人工终审' : '待人工终审口径（缺项均留槽位）' }}
                  </el-tag>
                </div>

                <div v-if="indicatorOverview.line_scoring_summary.length > 0" class="ind-score-summary">
                  <div class="ind-sub-title">典型评分标准维度与权重参考</div>
                  <div
                    v-for="(sd, sdi) in indicatorOverview.line_scoring_summary"
                    :key="sdi"
                    class="ind-score-row"
                  >
                    <el-tag size="small" effect="plain">{{ sd.dimension }}</el-tag>
                    <span class="ind-weight">{{ sd.typical_weight }}</span>
                    <span class="ind-focus">{{ sd.key_focus }}</span>
                  </div>
                </div>

                <div class="ind-sub-title mt-8">标准应答指标清单（含评分点与参数槽）</div>
                <el-table :data="indicatorOverview.indicators" size="small" border max-height="230">
                  <el-table-column prop="category" label="分类" width="88" />
                  <el-table-column label="指标名称" min-width="160">
                    <template #default="{ row }">
                      <div>
                        <strong>{{ (row as Record<string, unknown>).indicator_name }}</strong>
                      </div>
                      <el-tag
                        size="small"
                        :type="(row as Record<string, unknown>).line === 'common' ? 'info' : bizLineTagType(String((row as Record<string, unknown>).line))"
                        effect="plain"
                      >
                        {{ (row as Record<string, unknown>).line === 'common' ? '通用底座' : bizLineLabel(String((row as Record<string, unknown>).line)) }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column label="常见评分标准与高分提示" min-width="210">
                    <template #default="{ row }">
                      <div class="ind-cell-text">
                        <div>{{ ((row as Record<string, unknown>).scoring_ref as Record<string, unknown>)?.weight_hint || '—' }}</div>
                        <div class="ind-tip-line">
                          {{ ((row as Record<string, unknown>).scoring_ref as Record<string, unknown>)?.high_score_tips || '' }}
                        </div>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="标准应答口径 / 待确认槽" min-width="230">
                    <template #default="{ row }">
                      <div class="ind-cell-text">
                        {{ String((row as Record<string, unknown>).standard_response || '').slice(0, 110) }}…
                      </div>
                    </template>
                  </el-table-column>
                </el-table>
              </div>

              <el-radio-group v-model="composeMode" class="mode-radios">
                <el-radio value="skeleton_v1">
                  标准依据招标文件编制（按 Skill 与招标评分结构规划正文，报价/缺证严格锁定）
                </el-radio>
                <el-radio value="knowledge_draft_v2">
                  结合企业参考方案库编制（检索参考片段赋能 AI 撰写，无参考库安全回退）
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
              </div>

              <div class="action-bar">
                <el-button
                  type="primary"
                  :loading="generating"
                  @click="handleGenerateDraft"
                >
                  {{ useAi ? '发起 AI + Skill 标书编排与生成 (.docx)' : '生成确定性结构初稿 (.docx)' }}
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
                  <el-tag
                    size="small"
                    :type="isRunningStatus(item.status) ? 'warning' : (item.status === 'failed' ? 'danger' : 'success')"
                  >
                    {{
                      isRunningStatus(item.status)
                        ? `运行中 ${item.progress || 0}%`
                        : item.status === 'failed'
                          ? '已中断/失败'
                          : item.ai_status === 'done'
                            ? 'AI 已撰稿'
                            : '结构初稿'
                    }}
                  </el-tag>
                  <span>{{ item.skill_name || '标准版' }}</span>
                </div>
                <div class="draft-actions" @click.stop>
                  <el-button
                    v-if="!isRunningStatus(item.status) && item.status !== 'failed'"
                    size="small"
                    type="primary"
                    link
                    @click="handleDownloadDraft(item)"
                  >
                    下载 Word
                  </el-button>
                  <el-button
                    v-if="!isRunningStatus(item.status)"
                    size="small"
                    type="danger"
                    link
                    @click="handleDeleteDraft(item)"
                  >
                    删除
                  </el-button>
                </div>
              </div>
            </div>
          </el-card>

          <!-- 右侧蓝图详情 / 异步进度 / 折叠待办核对抽屉 -->
          <el-card shadow="never" class="detail-pane">
            <el-empty v-if="!activeDraft" description="请选择左侧草稿或生成新的标书初稿" />

            <template v-else-if="isRunningStatus(activeDraft.status)">
              <div class="async-status-card">
                <div class="async-top">
                  <div>
                    <h3>{{ activeDraft.title }}</h3>
                    <p class="async-sub">{{ activeDraft.stage_detail || '后台线程池编排生成中，切至其他页面不会中断…' }}</p>
                  </div>
                  <el-button
                    type="danger"
                    plain
                    size="small"
                    :loading="cancellingDraft"
                    @click="handleCancelDraft"
                  >
                    中断生成
                  </el-button>
                </div>
                <el-progress :percentage="activeDraft.progress || 10" :stroke-width="10" striped striped-flow />
              </div>
            </template>

            <template v-else-if="activeDraft.status === 'failed'">
              <el-alert
                type="error"
                :closable="false"
                show-icon
                :title="`生成任务未完成：${activeDraft.error_msg || '已中断或异常'}`"
              />
            </template>

            <template v-else-if="activeBlueprint">
              <div class="bp-header">
                <div>
                  <h3>{{ activeBlueprint.project_name }}</h3>
                  <div class="bp-sub">
                    编号：{{ activeBlueprint.project_code }} ｜ 开标日：{{ activeBlueprint.open_date || '待人工核对' }} ｜ 参考限价：{{ activeBlueprint.price_cap ? `${activeBlueprint.price_cap} 元` : '未识别/待核对' }}
                  </div>
                  <div class="bp-sub-tags">
                    <el-tag size="small" type="info">Skill：{{ activeBlueprint.skill_name || activeDraft.skill_name }}</el-tag>
                    <el-tag size="small" :type="bizLineTagType(activeDraft.biz_line)">
                      业务线：{{ bizLineLabel(activeDraft.biz_line) }}
                    </el-tag>
                    <el-tag
                      v-if="activeDraft.use_indicator_library || Boolean((activeBlueprint.stats as Record<string, unknown>)?.use_indicator_library)"
                      size="small"
                      type="success"
                      effect="plain"
                    >
                      公司指标库已注入（命中 {{ (activeBlueprint.stats as Record<string, unknown>)?.indicator_matched_topics || 0 }} 节）
                    </el-tag>
                    <el-tag size="small" :type="activeBlueprint.ai_status === 'done' ? 'success' : 'warning'">
                      {{
                        activeBlueprint.ai_status === 'done'
                          ? 'AI 动态规划章节与正文已生成'
                          : activeBlueprint.ai_status === 'partial'
                            ? 'AI 部分章节已生成'
                            : '0-Token 骨架模式（未启用或未配模型）'
                      }}
                    </el-tag>
                  </div>
                </div>
                <el-button
                  type="success"
                  @click="handleDownloadDraft(activeDraft)"
                >
                  下载排版初稿 (.docx)
                </el-button>
              </div>

              <!-- 紧凑分组人工待核对条（取代原 40 标签墙，点击展开明细抽屉） -->
              <div class="manual-compact-bar">
                <div class="mc-summary">
                  <strong>人工终核提醒（共 {{ manualGroupStats.total }} 项）</strong>
                  <el-tag size="small" type="danger" effect="plain">资格证照 {{ manualGroupStats.qual }}</el-tag>
                  <el-tag size="small" type="warning" effect="plain">报价锁定 {{ manualGroupStats.price }}</el-tag>
                  <el-tag size="small" type="info" effect="plain">偏离核对 {{ manualGroupStats.deviation }}</el-tag>
                  <el-tag size="small" type="success" effect="plain">方案审校 {{ manualGroupStats.scoring }}</el-tag>
                </div>
                <el-button size="small" link type="primary" @click="showManualDrawer = !showManualDrawer">
                  {{ showManualDrawer ? '收起待核对明细' : '展开待核对明细' }}
                </el-button>
              </div>

              <div v-if="showManualDrawer" class="manual-drawer-panel">
                <el-table :data="activeBlueprint.manual_checklist" size="small" max-height="260" border>
                  <el-table-column prop="field" label="待处理项" min-width="180" />
                  <el-table-column prop="reason" label="原因说明" min-width="200" />
                  <el-table-column prop="hint" label="建议动作" min-width="200" />
                  <el-table-column label="定位" width="80" align="center">
                    <template #default="{ row }">
                      <el-button
                        link
                        type="primary"
                        size="small"
                        @click="jumpToSection(String((row as ComposeManualItem).section_id))"
                      >
                        跳转
                      </el-button>
                    </template>
                  </el-table-column>
                </el-table>
              </div>

              <el-collapse v-model="expandedSections" class="bp-sections">
                <el-collapse-item
                  v-for="sec in activeBlueprint.sections.filter((s) => s.slot_type !== 'cert_ledger_appendix')"
                  :key="sec.section_id"
                  :name="sec.section_id"
                  :id="`composer-sec-${sec.section_id}`"
                  :class="{ 'sec-highlighted': highlightedSectionId === sec.section_id }"
                >
                  <template #title>
                    <div class="sec-title-row">
                      <span>{{ sec.title }}</span>
                      <el-tag size="small" effect="plain">{{ sec.fill_mode }}</el-tag>
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
                          <span :class="{ 'highlight-manual-text': Boolean((row as Record<string, unknown>).highlight) }">
                            {{ (row as Record<string, unknown>).value }}
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
                    <p class="bp-slot-tip">
                      导出 Word 后将在 2.0 总览索引表后逐项单列 2.1~2.N 独立附页（台账已上传图片原件将自动居中嵌图，未传原件则预留 A4 宽度扫描件粘贴装订框）。
                    </p>
                    <el-table :data="sectionBlocks(sec, 'cert_row')" size="small" border>
                      <el-table-column prop="cert_name" label="招标所需证照" min-width="170" />
                      <el-table-column label="角色级别" width="145">
                        <template #default="{ row }">
                          {{
                            (row as Record<string, unknown>).role_label ||
                            ((row as Record<string, unknown>).requirement_level === 'mandatory'
                              ? '必交资格项'
                              : '加分/参考项')
                          }}
                        </template>
                      </el-table-column>
                      <el-table-column prop="valid_until" label="台账有效期" width="110" />
                      <el-table-column label="附页形态" width="140">
                        <template #default="{ row }">
                          <el-tag
                            size="small"
                            :type="
                              (row as Record<string, unknown>).has_embedded_image
                                ? 'success'
                                : (row as Record<string, unknown>).has_attachment
                                  ? 'info'
                                  : 'warning'
                            "
                          >
                            {{
                              (row as Record<string, unknown>).has_embedded_image
                                ? 'Word 自动嵌图'
                                : (row as Record<string, unknown>).has_attachment
                                  ? 'PDF 原件装订框'
                                  : '全幅待粘贴框'
                            }}
                          </el-tag>
                        </template>
                      </el-table-column>
                      <el-table-column label="对账结果" min-width="220">
                        <template #default="{ row }">
                          <span :class="{ 'highlight-manual-text': (row as Record<string, unknown>).ledger_status !== 'valid' }">
                            {{ (row as Record<string, unknown>).display_status }}
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
                      <el-table-column prop="bid_response" label="我方响应（初稿）" min-width="220" />
                      <el-table-column prop="deviation" label="状态" width="140" />
                    </el-table>
                  </template>

                  <!-- Dynamic AI Chapters OR Fallback Scoring Topics -->
                  <template v-else-if="sec.slot_type === 'scoring_response_outline'">
                    <!-- AI 动态规划并撰写正本章节 -->
                    <div
                      v-for="(ch, cidx) in sectionBlocks(sec, 'ai_chapter')"
                      :key="`ai-${cidx}`"
                      class="ai-chapter-card"
                    >
                      <div class="topic-head">
                        <strong>{{ ch.chapter_no }} {{ ch.title }}</strong>
                        <el-tag size="small" :type="ch.ai_generated ? 'success' : 'warning'">
                          {{ ch.ai_generated ? 'AI 草稿·待人工审定' : '结构兜底' }}
                        </el-tag>
                      </div>
                      <div v-if="(ch.covered_topics as string[])?.length" class="topic-quote">
                        覆盖评分要点：{{ (ch.covered_topics as string[]).join('；') }}
                      </div>
                      <p
                        v-for="(para, pi) in ((ch.paragraphs as string[]) || [])"
                        :key="pi"
                        class="ai-para"
                      >
                        {{ para }}
                      </p>
                      <div
                        v-if="ch.table && (ch.table as Record<string, unknown>).headers"
                        class="ai-table-wrap"
                      >
                        <div class="ai-table-title">{{ (ch.table as Record<string, unknown>).title }}</div>
                        <table class="mini-grid-tbl">
                          <thead>
                            <tr>
                              <th
                                v-for="(h, hi) in ((ch.table as Record<string, unknown>).headers as string[])"
                                :key="hi"
                              >
                                {{ h }}
                              </th>
                            </tr>
                          </thead>
                          <tbody>
                            <tr
                              v-for="(rvals, ri) in ((ch.table as Record<string, unknown>).rows as string[][])"
                              :key="ri"
                            >
                              <td v-for="(cv, ci) in rvals" :key="ci">{{ cv }}</td>
                            </tr>
                          </tbody>
                        </table>
                      </div>
                      <div
                        v-for="(sn, sidx) in ((ch.reference_snippets as Array<Record<string, unknown>>) || [])"
                        :key="sidx"
                        class="rag-snippet-box"
                      >
                        <div class="rag-notice">{{ sn.notice }}</div>
                        <div class="rag-body">{{ sn.snippet }}</div>
                      </div>
                    </div>

                    <!-- 0-LLM 兜底评分大纲 -->
                    <div
                      v-for="(tp, tidx) in sectionBlocks(sec, 'scoring_topic')"
                      :key="`tp-${tidx}`"
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
            title="仅对在体检详情点击过「采纳整改」的发现执行安全原位替换，自动清除全文 w:comment 批注标记；大段漏应答若未填替换正文将诚实列入待人工补写清单。"
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
                <div class="quote-preview">{{ findOldTargetForFinding(row as BidFindingOut) || '（无明确原文定位，需人工补页）' }}</div>
              </template>
            </el-table-column>
            <el-table-column label="安全替换目标文本（留空则安全计入待人工清单）" min-width="260">
              <template #default="{ row }">
                <el-input
                  v-model="replacementDrafts[(row as BidFindingOut).id]"
                  :placeholder="
                    (row as BidFindingOut).code === 'MISSING_RESPONSE'
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

    <!-- ── AI 写作 Skills 管理与 GitHub / Gitee / 本地导入弹窗 ──────────── -->
    <el-dialog
      v-model="skillModalVisible"
      title="AI 标书写作 Skills 管理中心（支持 GitHub / Gitee / 本地导入）"
      width="780px"
    >
      <el-tabs v-model="skillModalTab">
        <el-tab-pane label="已有 Skills 列表" name="list">
          <div class="skill-modal-bar">
            <el-button type="primary" size="small" @click="resetSkillForm(); skillModalTab = 'edit'">
              + 新建自定义 Skill
            </el-button>
            <el-button size="small" @click="skillModalTab = 'import'">
              从 GitHub / Gitee / 本地文件导入
            </el-button>
          </div>
          <el-table :data="skillList" border size="small">
            <el-table-column prop="name" label="名称" min-width="150" />
            <el-table-column label="所属业务线" width="145">
              <template #default="{ row }">
                <el-tag size="small" :type="bizLineTagType((row as ComposerSkillOut).biz_line)" effect="plain">
                  {{ bizLineLabel((row as ComposerSkillOut).biz_line) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="source_type" label="来源" width="85" />
            <el-table-column label="版式特征" min-width="155">
              <template #default="{ row }">
                <span>
                  {{ (row as ComposerSkillOut).layout_spec.font_zh }} /
                  {{ (row as ComposerSkillOut).layout_spec.cover_style }} /
                  {{ (row as ComposerSkillOut).layout_spec.target_section_words }}字
                </span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="150" align="center">
              <template #default="{ row }">
                <el-button link type="primary" size="small" @click="startEditSkill(row as ComposerSkillOut)">编辑</el-button>
                <el-button link type="danger" size="small" @click="handleDeleteSkill(row as ComposerSkillOut)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="新建 / 编辑 Skill" name="edit">
          <el-form label-width="120px" size="small">
            <el-form-item label="Skill 名称" required>
              <el-input v-model="skillForm.name" placeholder="例如：政采医疗信息化高分模板" />
            </el-form-item>
            <el-form-item label="所属业务线">
              <el-select v-model="skillForm.biz_line" style="width: 240px">
                <el-option value="general" label="通用政采（仅挂载通用常见指标）" />
                <el-option value="security" label="网络安全与数据安全（专精+通用）" />
                <el-option value="maas" label="并行科技 MaaS 平台（专精+通用）" />
              </el-select>
            </el-form-item>
            <el-form-item label="适用场景说明">
              <el-input v-model="skillForm.description" placeholder="一句话说明写作风格与版式特点" />
            </el-form-item>
            <el-form-item label="AI 写作规范指令" required>
              <el-input
                v-model="skillForm.writing_instructions"
                type="textarea"
                :rows="5"
                placeholder="定义 AI 写作视角、应答格式、技术侧重与禁止事项…"
              />
            </el-form-item>
            <el-form-item label="封面与色调">
              <div class="skill-inline-grid">
                <el-select v-model="skillForm.layout_spec.cover_style" style="width: 150px">
                  <el-option value="gov_clean" label="政务公文风 (gov_clean)" />
                  <el-option value="navy_business" label="商务深青风 (navy_business)" />
                  <el-option value="tech_modern" label="现代科技风 (tech_modern)" />
                </el-select>
                <el-select v-model="skillForm.layout_spec.font_zh" style="width: 120px">
                  <el-option value="仿宋" label="仿宋" />
                  <el-option value="宋体" label="宋体" />
                  <el-option value="微软雅黑" label="微软雅黑" />
                  <el-option value="黑体" label="黑体" />
                </el-select>
                <el-select v-model="skillForm.layout_spec.tone" style="width: 140px">
                  <el-option value="formal_gov" label="严谨政采风" />
                  <el-option value="technical_deep" label="深度技术架构风" />
                  <el-option value="concise_direct" label="简洁条文风" />
                </el-select>
              </div>
            </el-form-item>
            <el-form-item label="字号 / 行距 / 字数">
              <div class="skill-inline-grid">
                <el-input-number v-model="skillForm.layout_spec.font_size_pt" :min="9" :max="16" :step="0.5" />
                <el-input-number v-model="skillForm.layout_spec.line_spacing" :min="1" :max="2.2" :step="0.1" />
                <el-input-number v-model="skillForm.layout_spec.target_section_words" :min="150" :max="2000" :step="50" />
              </div>
            </el-form-item>
            <el-form-item label="版式开关">
              <div class="skill-inline-grid">
                <el-checkbox v-model="skillForm.layout_spec.include_toc">生成 Word 自动目录</el-checkbox>
                <el-checkbox v-model="skillForm.layout_spec.append_checklist_at_end">文末附内部核对附录</el-checkbox>
                <el-checkbox v-model="skillForm.is_default">设为默认 Skill</el-checkbox>
              </div>
            </el-form-item>
          </el-form>
          <div class="action-bar">
            <el-button @click="resetSkillForm">重置</el-button>
            <el-button type="primary" :loading="savingSkill" @click="handleSaveSkill">保存 Skill</el-button>
          </div>
        </el-tab-pane>

        <el-tab-pane label="GitHub / Gitee / 本地导入" name="import">
          <el-alert
            type="info"
            :closable="false"
            show-icon
            title="仅解析纯文本 SKILL.md / JSON 规则（严禁执行任何仓库脚本）。如客户处于无外网内网或未开代理，可直接下方上传本地文件导入。"
            class="mb-12"
          />
          <div class="import-url-row">
            <el-input
              v-model="gitUrlInput"
              placeholder="输入 GitHub / Gitee 链接或 owner/repo（如 owner/repo 或 .../blob/main/SKILL.md）"
              clearable
            />
            <el-button type="primary" :loading="previewingUrl" @click="handlePreviewSkillUrl">
              拉取预览
            </el-button>
          </div>
          <div class="mt-8">
            <el-checkbox v-model="allowGiteeFallback">
              GitHub 不可达或超时时自动优雅降级尝试 Gitee 同路径
            </el-checkbox>
          </div>

          <el-alert
            v-if="urlImportError"
            type="warning"
            :closable="false"
            show-icon
            :title="urlImportError"
            class="mt-12"
          />

          <div v-if="urlPreview" class="preview-box mt-12">
            <div class="pv-head">
              <strong>{{ urlPreview.name }}</strong>
              <el-tag size="small" type="success">来源：{{ urlPreview.fetched_from_provider || urlPreview.source_type }}</el-tag>
            </div>
            <p class="pv-desc">{{ urlPreview.description }}</p>
            <pre class="pv-body">{{ urlPreview.writing_instructions.slice(0, 600) }}</pre>
            <div class="action-bar">
              <el-button type="success" :loading="importingUrl" @click="handleConfirmImportUrl">
                确认导入此 Skill 到工作台
              </el-button>
            </div>
          </div>

          <el-divider>或直接上传本地 Skill 文件（内网离线兜底）</el-divider>
          <div class="local-skill-upload">
            <span>选择本地 <code>SKILL.md</code> / <code>.md</code> / <code>.json</code> 文件一键导入：</span>
            <input type="file" accept=".md,.markdown,.txt,.json" @change="handleLocalSkillFileChange" />
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-dialog>
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

.skill-row-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.skill-picker-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.skill-summary-line {
  font-size: 12px;
  color: #475569;
  background: #f8fafc;
  border-left: 3px solid #2563eb;
  padding: 6px 10px;
  border-radius: 4px;
}

.skill-opt-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  width: 100%;
}

.mr-6 {
  margin-right: 6px;
}

.indicator-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 6px 10px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
}

.ind-switch-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  font-weight: 500;
  color: #1e293b;
}

.indicator-preview-box {
  padding: 10px 12px;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  background: #ffffff;
}

.ind-pv-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 13px;
  color: #0f172a;
}

.ind-score-summary {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-bottom: 8px;
}

.ind-sub-title {
  font-size: 12px;
  font-weight: 600;
  color: #334155;
  margin-bottom: 4px;
}

.ind-score-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #334155;
}

.ind-weight {
  font-weight: 600;
  color: #1d4ed8;
}

.ind-focus {
  color: #475569;
}

.ind-cell-text {
  font-size: 12px;
  color: #334155;
  line-height: 1.45;
}

.ind-tip-line {
  margin-top: 3px;
  color: #0f766e;
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
  gap: 8px;
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

.async-status-card {
  padding: 20px;
  border: 1px solid #bfdbfe;
  background: #eff6ff;
  border-radius: 8px;
}

.async-top {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 14px;
}

.async-top h3 {
  margin: 0 0 4px;
  font-size: 16px;
  color: #0f172a;
}

.async-sub {
  margin: 0;
  font-size: 13px;
  color: #1e40af;
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

.bp-sub-tags {
  display: flex;
  gap: 6px;
  margin-top: 6px;
}

.manual-compact-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid #fde68a;
  background: #fffbeb;
  border-radius: 6px;
  margin-bottom: 10px;
}

.mc-summary {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12.5px;
  color: #92400e;
}

.manual-drawer-panel {
  margin-bottom: 12px;
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

.bp-slot-tip {
  margin: 0 0 10px;
  font-size: 12px;
  color: #475569;
  background: #f8fafc;
  padding: 6px 10px;
  border-radius: 4px;
  border-left: 3px solid #3b82f6;
}

.ai-chapter-card,
.topic-card {
  padding: 12px 14px;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  margin-bottom: 12px;
  background: #ffffff;
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
  margin-bottom: 8px;
}

.ai-para {
  margin: 6px 0;
  font-size: 13px;
  line-height: 1.7;
  color: #0f172a;
  text-indent: 2em;
}

.ai-table-wrap {
  margin: 10px 0;
}

.ai-table-title {
  font-size: 12.5px;
  font-weight: 600;
  color: #1e293b;
  margin-bottom: 4px;
}

.mini-grid-tbl {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.mini-grid-tbl th,
.mini-grid-tbl td {
  border: 1px solid #cbd5e1;
  padding: 5px 8px;
  text-align: left;
}

.mini-grid-tbl th {
  background: #f1f5f9;
  font-weight: 600;
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

.skill-modal-bar {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}

.skill-inline-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.import-url-row {
  display: flex;
  gap: 8px;
}

.preview-box {
  padding: 12px;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  background: #f8fafc;
}

.pv-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.pv-desc {
  margin: 0 0 6px;
  font-size: 12.5px;
  color: #475569;
}

.pv-body {
  margin: 0 0 10px;
  font-size: 12px;
  color: #0f172a;
  white-space: pre-wrap;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  padding: 8px;
  border-radius: 4px;
  max-height: 180px;
  overflow-y: auto;
}

.local-skill-upload {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
}

.mb-8 {
  margin-bottom: 8px;
}

.mb-12 {
  margin-bottom: 12px;
}

.mt-8 {
  margin-top: 8px;
}

.mt-12 {
  margin-top: 12px;
}
</style>
