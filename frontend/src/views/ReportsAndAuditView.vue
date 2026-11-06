<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { auditApi, checksApi, reportsApi } from '@/api'
import type { AuditLogItemOut, BidTaskOut } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

defineOptions({ name: 'ReportsAndAuditView' })

const authStore = useAuthStore()

const activeTab = ref<'deliver' | 'audit'>('deliver')

// ── 页签 ①：封标交付中心状态 ───────────────────────────────────────────
const loadingTasks = ref(false)
const deliveredTasks = ref<BidTaskOut[]>([])
const busyDownload = ref<Record<string, boolean>>({})

async function fetchDeliveredTasks(): Promise<void> {
  loadingTasks.value = true
  try {
    const res = await checksApi.list({
      task_type: 'pre_check',
      page: 1,
      page_size: 50,
    })
    deliveredTasks.value = res.items || []
  } finally {
    loadingTasks.value = false
  }
}

function getFileExt(filename?: string | null): string {
  if (!filename || !filename.includes('.')) return '.docx'
  return filename.slice(filename.lastIndexOf('.'))
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

// ── 页签 ②：安全审计日志状态（member 非管理员显示“仅管理员可见”，不弹错误） ──
const loadingAudit = ref(false)
const auditForbidden = ref(false)
const actionFilter = ref('')
const auditPage = ref(1)
const auditPageSize = ref(20)
const auditTotal = ref(0)
const auditLogs = ref<AuditLogItemOut[]>([])

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
        <!-- 页签 ①：封标交付中心（每任务三项下载：投标书原件 / 标红原件 / 交付包 ZIP） -->
        <el-tab-pane label="① 封标交付中心（三件套导出）" name="deliver">
          <div v-loading="loadingTasks" class="deliver-pane">
            <div class="dp-hd">
              <div>
                <h3>封标前防废标体检交付归档台账</h3>
                <p>
                  每个体检任务支持一键导出：投标书原件、Word/PDF 原位标红批注件、以及包含《技术与商务偏离表.docx》和《人工终核签字确认单.docx》的完整交付包 ZIP。
                </p>
              </div>
              <el-button size="small" @click="fetchDeliveredTasks">刷新台账</el-button>
            </div>

            <el-table
              v-if="deliveredTasks.length > 0"
              :data="deliveredTasks"
              border
              stripe
              style="width: 100%"
            >
              <el-table-column prop="id" label="编号" width="80">
                <template #default="{ row }">
                  <span class="mono-num">#{{ row.id }}</span>
                </template>
              </el-table-column>

              <el-table-column label="投标项目名称与双书文件" min-width="260">
                <template #default="{ row }">
                  <div class="proj-title">{{ row.title }}</div>
                  <div class="proj-sub">
                    招标书：{{ row.tender_filename || '—' }} ｜ 投标书：{{ row.bid_filename || '—' }}
                  </div>
                </template>
              </el-table-column>

              <el-table-column label="体检状态" width="120">
                <template #default="{ row }">
                  <span class="sev-pill" :class="row.status === 'delivered' ? 'ok' : 'info'">
                    {{ row.status === 'delivered' ? '已交付就绪' : row.status }}
                  </span>
                </template>
              </el-table-column>

              <el-table-column label="检出风险统计" width="210">
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

              <el-table-column label="封标交付物下载（文件名自动附带项目名）" width="380" fixed="right">
                <template #default="{ row }">
                  <div class="dl-btn-group">
                    <el-button
                      size="small"
                      :loading="Boolean(busyDownload[`${row.id}_orig`])"
                      @click="handleDownloadOriginalBid(row)"
                    >
                      投标书原件
                    </el-button>
                    <el-button
                      size="small"
                      type="primary"
                      plain
                      :loading="Boolean(busyDownload[`${row.id}_redline`])"
                      @click="handleDownloadRedlineBid(row)"
                    >
                      标红原件
                    </el-button>
                    <el-button
                      size="small"
                      type="success"
                      :loading="Boolean(busyDownload[`${row.id}_zip`])"
                      @click="handleDownloadDeliverZip(row)"
                    >
                      交付包 ZIP
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
  gap: 8px;
  flex-wrap: nowrap;
}

.muted-txt {
  color: var(--txt-muted);
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
</style>