<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { auditApi, checksApi, reportsApi } from '@/api'
import type {
  AuditLogItemOut,
  BatchDeliverFailedItem,
  BatchDeliverZipOut,
  BidTaskOut,
  DeliverIncludeKey,
} from '@/api/types'
import { useAppStore } from '@/stores/app'
import { useAuthStore } from '@/stores/auth'

defineOptions({ name: 'ReportsAndAuditView' })

const authStore = useAuthStore()
const appStore = useAppStore()

const activeTab = ref<'deliver' | 'audit'>('deliver')

// ── 页签 ①：封标交付中心状态 ───────────────────────────────────────────
const loadingTasks = ref(false)
const deliveredTasks = ref<BidTaskOut[]>([])
const busyDownload = ref<Record<string, boolean>>({})

// E16：多选与批量打包配置弹窗状态
const selectedTasks = ref<BidTaskOut[]>([])
const batchDialogVisible = ref(false)
const batchSubmitting = ref(false)
const batchInclude = ref<DeliverIncludeKey[]>([
  'original',
  'redline',
  'deviation',
  'signoff',
])

const INCLUDE_OPTIONS: Array<{ key: DeliverIncludeKey; label: string; desc: string }> = [
  { key: 'original', label: '投标书原件', desc: '上传时的原始投标文件（.docx / .pdf）' },
  { key: 'redline', label: '标红批注件', desc: '带原位高亮与风险批注的投标书副本' },
  { key: 'deviation', label: '偏离表.docx', desc: '双书 ★/▲ 条款逐条对齐《技术与商务偏离表.docx》' },
  { key: 'signoff', label: '终核签字单.docx', desc: '《封标前人工终核签字确认单.docx》' },
]

async function fetchDeliveredTasks(): Promise<void> {
  loadingTasks.value = true
  try {
    const res = await checksApi.list({
      task_type: 'pre_check',
      page: 1,
      page_size: 50,
    })
    deliveredTasks.value = res.items || []
    selectedTasks.value = []
  } finally {
    loadingTasks.value = false
  }
}

function getFileExt(filename?: string | null): string {
  if (!filename || !filename.includes('.')) return '.docx'
  return filename.slice(filename.lastIndexOf('.'))
}

function handleSelectionChange(rows: BidTaskOut[]): void {
  selectedTasks.value = rows || []
}

function openBatchDialog(): void {
  if (selectedTasks.value.length === 0) {
    ElMessage.warning('请先在表格中勾选至少 1 个体检任务')
    return
  }
  batchInclude.value = ['original', 'redline', 'deviation', 'signoff']
  batchDialogVisible.value = true
}

async function handleBatchZipSubmit(): Promise<void> {
  if (selectedTasks.value.length === 0) return
  if (batchInclude.value.length === 0) {
    ElMessage.warning('请至少勾选 1 项需要打包的交付物内容')
    return
  }
  batchSubmitting.value = true
  try {
    const taskIds = selectedTasks.value.map((t) => t.id)
    const data = await reportsApi.batchDeliverZip({
      task_ids: taskIds,
      include: batchInclude.value,
    })

    if (data.failed && data.failed.length > 0) {
      const failDesc = data.failed
        .map((f: BatchDeliverFailedItem) => `#${f.task_id}（${f.reason}）`)
        .join('；')
      ElMessage.warning({
        message: `部分任务已跳过：${failDesc}`,
        duration: 6000,
      })
    }

    const fn = await checksApi.downloadStoredFile(
      data.download_url,
      data.output_filename || '批量封标交付包.zip',
    )
    batchDialogVisible.value = false
    ElMessage.success(
      `批量交付包已下载：${fn}（成功打包 ${data.succeeded_task_ids.length} 个任务）`,
    )
  } finally {
    batchSubmitting.value = false
  }
}

async function handleDownloadOriginalBid(task: Partial<BidTaskOut>): Promise<void> {
  if (task.id === undefined) return
  if (!task.bid_url) {
    ElMessage.warning('该任务未关联投标书原件路径')
    return
  }
  const key = `${task.id}_orig`
  if (busyDownload.value[key]) return
  busyDownload.value[key] = true
  try {
    const ext = getFileExt(task.bid_filename)
    const fn = await checksApi.downloadStoredFile(
      task.bid_url,
      `${task.title}_投标书原件${ext}`,
    )
    ElMessage.success(`已下载投标书原件：${fn}`)
  } finally {
    busyDownload.value[key] = false
  }
}

async function handleDownloadRedlineBid(task: Partial<BidTaskOut>): Promise<void> {
  if (task.id === undefined) return
  const key = `${task.id}_redline`
  if (busyDownload.value[key]) return
  busyDownload.value[key] = true
  try {
    const ext = getFileExt(task.bid_filename)
    const fn = await reportsApi.downloadRedline(
      task.id,
      `${task.title}_标红原件${ext}`,
    )
    ElMessage.success(`已下载投标书原位标红批注件：${fn}`)
  } finally {
    busyDownload.value[key] = false
  }
}

async function handleDownloadDeviationDocx(task: Partial<BidTaskOut>): Promise<void> {
  if (task.id === undefined) return
  const key = `${task.id}_deviation`
  if (busyDownload.value[key]) return
  busyDownload.value[key] = true
  try {
    const fn = await checksApi.downloadStoredFile(
      `/api/deviation/${task.id}/download`,
      `${task.title}_技术与商务偏离表.docx`,
    )
    ElMessage.success(`已下载技术与商务偏离表：${fn}`)
  } finally {
    busyDownload.value[key] = false
  }
}

async function handleDownloadSignoffDocx(task: Partial<BidTaskOut>): Promise<void> {
  if (task.id === undefined) return
  const key = `${task.id}_signoff`
  if (busyDownload.value[key]) return
  busyDownload.value[key] = true
  try {
    const fn = await reportsApi.downloadSignoff(
      task.id,
      `${task.title}_封标前人工终核签字确认单.docx`,
    )
    ElMessage.success(`已下载人工终核签字确认单：${fn}`)
  } finally {
    busyDownload.value[key] = false
  }
}

async function handleDownloadDeliverZip(task: Partial<BidTaskOut>): Promise<void> {
  if (task.id === undefined) return
  const key = `${task.id}_zip`
  if (busyDownload.value[key]) return
  busyDownload.value[key] = true
  try {
    const fn = await reportsApi.downloadDeliverZip(
      task.id,
      `${task.title}_封标交付包.zip`,
    )
    ElMessage.success(`已下载封标三件套交付包：${fn}`)
  } finally {
    busyDownload.value[key] = false
  }
}

async function handleExportCleanBidRow(task: Partial<BidTaskOut>): Promise<void> {
  if (task.id === undefined) return
  const key = `${task.id}_clean`
  if (busyDownload.value[key]) return
  if (task.bid_filename?.toLowerCase().endsWith('.pdf')) {
    ElMessage.warning('PDF 原件不支持安全无损修订导出，请使用 Word (.docx) 原件在编制中心操作')
    return
  }
  busyDownload.value[key] = true
  try {
    const res = await reportsApi.exportCleanBid(task.id, {})
    await reportsApi.downloadCleanBid(
      task.id,
      `${task.title}_修订定稿版投标书.docx`,
    )
    ElMessage.success(
      `已生成 Clean 定稿（原位替换 ${res.applied_count} 处，待人工 ${res.manual_pending_count} 项）`,
    )
  } finally {
    busyDownload.value[key] = false
  }
}

async function handleDeleteDeliveredTask(task: Partial<BidTaskOut>): Promise<void> {
  if (task.id === undefined) return
  if (task.status && ['extracting', 'checking', 'grading'].includes(task.status)) {
    ElMessage.warning('任务正在运行中，请先等待完成')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确认删除体检任务「#${task.id} ${task.title ?? ''}」及其全部风险发现、偏离表与交付产物吗？删除后不可恢复。`,
      '确认删除体检任务',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  await checksApi.remove(task.id)
  if (appStore.activeTaskId === task.id) {
    appStore.setActiveTaskId(null)
  }
  ElMessage.success(`体检任务 #${task.id} 已删除`)
  await fetchDeliveredTasks()
}

// ── 页签 ②：安全审计日志状态（member 非管理员显示“仅管理员可见”，不弹错误） ──
const loadingAudit = ref(false)
const exportingAudit = ref(false)
const auditForbidden = ref(false)
const actionFilter = ref('')
const auditPage = ref(1)
const auditPageSize = ref(20)
const auditTotal = ref(0)
const auditLogs = ref<AuditLogItemOut[]>([])

async function handleExportAuditCsv(): Promise<void> {
  if (!authStore.isAdmin || exportingAudit.value) return
  exportingAudit.value = true
  try {
    const fn = await auditApi.exportLogsCsv(actionFilter.value.trim() || undefined)
    ElMessage.success(`已导出审计台账：${fn}`)
  } finally {
    exportingAudit.value = false
  }
}

async function fetchAuditLogs(): Promise<void> {
  if (!authStore.isAdmin) {
    auditForbidden.value = true
    return
  }
  loadingAudit.value = true
  auditForbidden.value = false
  try {
    const res = await auditApi.listLogs({
      action: actionFilter.value.trim() || undefined,
      page: auditPage.value,
      page_size: auditPageSize.value,
    })
    auditTotal.value = res.total || 0
    auditLogs.value = res.items || []
  } catch (err: unknown) {
    const status = (err as { response?: { status?: number } })?.response?.status
    if (status === 403) {
      auditForbidden.value = true
    }
  } finally {
    loadingAudit.value = false
  }
}

function handleTabChange(name: string | number): void {
  if (name === 'audit') {
    void fetchAuditLogs()
  } else {
    void fetchDeliveredTasks()
  }
}

function handleAuditPageChange(p: number): void {
  auditPage.value = p
  void fetchAuditLogs()
}

onMounted(() => {
  void fetchDeliveredTasks()
  if (authStore.isAdmin) {
    void fetchAuditLogs()
  } else {
    auditForbidden.value = true
  }
})
</script>

<template>
  <div class="reports-audit-view">
    <div class="bg-panel ra-card">
      <el-tabs v-model="activeTab" @tab-change="handleTabChange">
        <!-- 页签 ①：封标交付中心（行内四大件独立下载 + 交付包 ZIP + 顶部批量自选打包） -->
        <el-tab-pane label="① 封标交付中心（四大件与批量打包）" name="deliver">
          <div v-loading="loadingTasks" class="deliver-pane">
            <div class="dp-hd">
              <div>
                <h3>封标前防废标体检交付归档台账</h3>
                <p>
                  每行支持独立下载四大件（原件 / 标红件 / 偏离表 / 终核单）及一键交付包 ZIP；勾选多行可顶部批量自选打包。
                </p>
              </div>
              <div class="dp-hd-actions">
                <el-button
                  type="primary"
                  size="small"
                  :disabled="selectedTasks.length === 0"
                  @click="openBatchDialog"
                >
                  批量打包下载{{ selectedTasks.length > 0 ? ` (${selectedTasks.length})` : '' }}
                </el-button>
                <el-button size="small" @click="fetchDeliveredTasks">刷新台账</el-button>
              </div>
            </div>

            <el-table
              v-if="deliveredTasks.length > 0"
              :data="deliveredTasks"
              row-key="id"
              border
              stripe
              style="width: 100%"
              @selection-change="handleSelectionChange"
            >
              <el-table-column type="selection" width="48" />

              <el-table-column prop="id" label="编号" width="76">
                <template #default="{ row }">
                  <span class="mono-num">#{{ row.id }}</span>
                </template>
              </el-table-column>

              <el-table-column label="投标项目名称与双书文件" min-width="240">
                <template #default="{ row }">
                  <div class="proj-title">{{ row.title }}</div>
                  <div class="proj-sub">
                    招标书：{{ row.tender_filename || '—' }} ｜ 投标书：{{ row.bid_filename || '—' }}
                  </div>
                  <div class="proj-sub mono-num">
                    开标日期：
                    <span :class="row.config_json?.open_date ? 'od-ok' : 'muted-txt'">
                      {{ row.config_json?.open_date || '未识别' }}
                    </span>
                  </div>
                </template>
              </el-table-column>

              <el-table-column label="体检状态" width="115">
                <template #default="{ row }">
                  <span class="sev-pill" :class="row.status === 'delivered' ? 'ok' : 'info'">
                    {{ row.status === 'delivered' ? '已交付就绪' : row.status }}
                  </span>
                </template>
              </el-table-column>

              <el-table-column label="检出风险统计" width="195">
                <template #default="{ row }">
                  <template v-if="row.stats_json">
                    <span class="sev-pill error" style="margin-right: 6px">
                      一票否决 {{ row.stats_json.error_count ?? 0 }}
                    </span>
                    <span class="sev-pill warning">
                      预警 {{ row.stats_json.warning_count ?? 0 }}
                    </span>
                  </template>
                  <span v-else class="muted-txt">—</span>
                </template>
              </el-table-column>

              <el-table-column label="封标交付物独立下载与打包管理" width="540" fixed="right">
                <template #default="{ row }">
                  <div class="dl-btn-group">
                    <el-button
                      size="small"
                      :loading="Boolean(busyDownload[`${row.id}_orig`])"
                      @click="handleDownloadOriginalBid(row)"
                    >
                      原件
                    </el-button>
                    <el-button
                      size="small"
                      type="primary"
                      plain
                      :loading="Boolean(busyDownload[`${row.id}_redline`])"
                      @click="handleDownloadRedlineBid(row)"
                    >
                      标红件
                    </el-button>
                    <el-button
                      size="small"
                      type="warning"
                      plain
                      :loading="Boolean(busyDownload[`${row.id}_deviation`])"
                      @click="handleDownloadDeviationDocx(row)"
                    >
                      偏离表
                    </el-button>
                    <el-button
                      size="small"
                      type="info"
                      plain
                      :loading="Boolean(busyDownload[`${row.id}_signoff`])"
                      @click="handleDownloadSignoffDocx(row)"
                    >
                      终核单
                    </el-button>
                    <el-button
                      size="small"
                      type="success"
                      :loading="Boolean(busyDownload[`${row.id}_zip`])"
                      @click="handleDownloadDeliverZip(row)"
                    >
                      交付包 ZIP
                    </el-button>
                    <el-button
                      size="small"
                      type="primary"
                      plain
                      :loading="Boolean(busyDownload[`${row.id}_clean`])"
                      @click="handleExportCleanBidRow(row)"
                    >
                      Clean 定稿
                    </el-button>
                    <el-button
                      size="small"
                      type="danger"
                      plain
                      :disabled="['extracting', 'checking', 'grading'].includes(row.status)"
                      @click="handleDeleteDeliveredTask(row)"
                    >
                      删除
                    </el-button>
                  </div>
                </template>
              </el-table-column>
            </el-table>

            <el-empty v-else description="暂无可导出的投标体检任务" />
          </div>
        </el-tab-pane>

        <!-- 页签 ②：安全审计日志（member 非管理员显示“仅管理员可见”友好提示，不弹错误） -->
        <el-tab-pane label="② 安全审计留痕日志" name="audit">
          <div v-if="auditForbidden || !authStore.isAdmin" class="audit-forbidden-box">
            <div class="af-badge">ADMIN ONLY · 权限隔离</div>
            <h3>安全审计日志仅管理员可见</h3>
            <p>
              当前登录账号（{{ authStore.user?.username }}）角色为「投标商务专员 (member)」。
              如需查阅全量安全审计留痕记录，请使用系统管理员（admin）账号登录。
            </p>
          </div>

          <div v-else v-loading="loadingAudit" class="audit-pane">
            <div class="dp-hd">
              <div>
                <h3>本部门安全操作审计留痕台账（共 {{ auditTotal }} 条）</h3>
                <p>自动记录登录、双书上传、防废标核验、报价人工回填、偏离表导出、规则修改与模型配置变更。</p>
              </div>

              <div class="audit-filters">
                <el-input
                  v-model.trim="actionFilter"
                  size="small"
                  clearable
                  placeholder="按动作过滤（如 login / export_deliver_zip）"
                  style="width: 260px"
                  @keyup.enter="fetchAuditLogs"
                  @clear="fetchAuditLogs"
                />
                <el-button size="small" type="primary" @click="fetchAuditLogs">
                  筛选日志
                </el-button>
                <el-button
                  size="small"
                  type="success"
                  plain
                  :loading="exportingAudit"
                  @click="handleExportAuditCsv"
                >
                  导出日志 (CSV)
                </el-button>
              </div>
            </div>

            <el-table :data="auditLogs" border stripe style="width: 100%">
              <el-table-column prop="id" label="ID" width="80">
                <template #default="{ row }">
                  <span class="mono-num">#{{ row.id }}</span>
                </template>
              </el-table-column>

              <el-table-column prop="action" label="审计动作 (action)" width="210">
                <template #default="{ row }">
                  <code class="act-code mono-num">{{ row.action }}</code>
                </template>
              </el-table-column>

              <el-table-column prop="target" label="操作对象 (target)" width="180">
                <template #default="{ row }">
                  <span class="mono-num">{{ row.target || '—' }}</span>
                </template>
              </el-table-column>

              <el-table-column label="详情快照 (detail_json)" min-width="280">
                <template #default="{ row }">
                  <span class="mono-num detail-txt">
                    {{ row.detail_json ? JSON.stringify(row.detail_json) : '—' }}
                  </span>
                </template>
              </el-table-column>

              <el-table-column prop="created_at" label="记录时间 (UTC)" width="180">
                <template #default="{ row }">
                  <span class="mono-num">
                    {{ row.created_at ? row.created_at.slice(0, 19).replace('T', ' ') : '—' }}
                  </span>
                </template>
              </el-table-column>
            </el-table>

            <div v-if="auditTotal > auditPageSize" class="audit-pager">
              <el-pagination
                background
                layout="total, prev, pager, next"
                :total="auditTotal"
                :page-size="auditPageSize"
                :current-page="auditPage"
                @current-change="handleAuditPageChange"
              />
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>

    <!-- E16：批量打包与内容自选配置弹窗 -->
    <el-dialog
      v-model="batchDialogVisible"
      title="批量打包封标交付物（自主选择打包内容）"
      width="520px"
      :close-on-click-modal="false"
      destroy-on-close
    >
      <div class="batch-dialog-body">
        <div class="batch-summary-tip">
          已勾选 <b class="mono-num">{{ selectedTasks.length }}</b> 个体检任务，打包后每个任务将在 ZIP 内生成独立子目录（<code class="mono-num">{task_id}_{项目名称}/</code>）。
        </div>

        <div class="batch-chk-label">请选择要包含在每个任务子包中的交付物文件：</div>
        <el-checkbox-group v-model="batchInclude" class="batch-chk-group">
          <div
            v-for="opt in INCLUDE_OPTIONS"
            :key="opt.key"
            class="batch-chk-item"
          >
            <el-checkbox :value="opt.key" :label="opt.key">
              <span class="bci-title">{{ opt.label }}</span>
              <span class="bci-desc">—— {{ opt.desc }}</span>
            </el-checkbox>
          </div>
        </el-checkbox-group>
      </div>

      <template #footer>
        <el-button @click="batchDialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="batchSubmitting"
          :disabled="batchInclude.length === 0"
          @click="handleBatchZipSubmit"
        >
          开始批量打包 ({{ selectedTasks.length }} 个任务)
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.ra-card {
  padding: 20px 24px;
}

.dp-hd {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  flex-wrap: wrap;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--border-line);
  margin-bottom: 16px;
}

.dp-hd h3 {
  font-size: 16px;
  font-weight: 900;
  color: var(--txt-strong);
}

.dp-hd p {
  font-size: 12.5px;
  color: var(--txt-sec);
  margin-top: 3px;
}

.dp-hd-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.proj-title {
  font-weight: 800;
  color: var(--txt-strong);
}

.proj-sub {
  font-size: 11.5px;
  color: var(--txt-muted);
  margin-top: 2px;
}

.dl-btn-group {
  display: flex;
  gap: 6px;
  flex-wrap: nowrap;
}

.muted-txt {
  color: var(--txt-muted);
}

.batch-dialog-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.batch-summary-tip {
  padding: 10px 12px;
  border-radius: 6px;
  background: var(--brand-soft);
  color: var(--txt-strong);
  font-size: 12.5px;
  line-height: 1.5;
}

.batch-chk-label {
  font-size: 13px;
  font-weight: 800;
  color: var(--txt-strong);
}

.batch-chk-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.batch-chk-item {
  padding: 8px 12px;
  border: 1px solid var(--border-line);
  border-radius: 6px;
  background: var(--bg-subtle);
}

.bci-title {
  font-weight: 800;
  color: var(--txt-strong);
}

.bci-desc {
  font-size: 12px;
  color: var(--txt-sec);
  margin-left: 4px;
}

.audit-forbidden-box {
  padding: 56px 28px;
  text-align: center;
  background: var(--bg-subtle);
  border: 1px dashed var(--border-strong);
  border-radius: 10px;
  margin: 16px 0;
}

.af-badge {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 4px;
  background: #fee2e2;
  color: #991b1b;
  font-size: 11px;
  font-weight: 800;
  margin-bottom: 10px;
}

.audit-forbidden-box h3 {
  font-size: 18px;
  font-weight: 900;
  color: var(--txt-strong);
  margin-bottom: 6px;
}

.audit-forbidden-box p {
  font-size: 13px;
  color: var(--txt-sec);
  max-width: 520px;
  margin: 0 auto;
}

.audit-filters {
  display: flex;
  gap: 8px;
  align-items: center;
}

.act-code {
  background: var(--brand-soft);
  color: var(--brand);
  padding: 2px 7px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 700;
}

.detail-txt {
  font-size: 11.5px;
  color: var(--txt-sec);
  word-break: break-all;
}

.audit-pager {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}

.od-ok {
  color: #0f766e;
  font-weight: 700;
}
</style>
