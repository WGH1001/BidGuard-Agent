<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts/core'
import { BarChart, PieChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { checksApi, dashboardApi, deviationApi } from '@/api'
import type { BidTaskOut, DashboardStatsOut } from '@/api/types'
import { useAppStore } from '@/stores/app'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'

echarts.use([BarChart, PieChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

defineOptions({ name: 'DashboardView' })

const router = useRouter()
const appStore = useAppStore()
const loading = ref(false)
const stats = ref<DashboardStatsOut | null>(null)

const severityChartRef = ref<HTMLElement | null>(null)
let severityChart: echarts.ECharts | null = null

const defenseMatrix = computed(() => stats.value?.defense_matrix || [])
const countdownAlerts = computed(() => stats.value?.countdown_alerts || [])
const recentTasks = computed(() => stats.value?.recent_tasks || [])

const defenseMaxCount = computed(() => {
  const nums = defenseMatrix.value.map((i) => i.hit_count || 0)
  return Math.max(1, ...nums)
})

function defenseBarPct(count: number): number {
  if (!count || count <= 0) return 0
  const raw = Math.round((count / defenseMaxCount.value) * 100)
  return Math.max(6, Math.min(100, raw))
}

function countdownBorderClass(daysLeft?: number | null): string {
  if (daysLeft === null || daysLeft === undefined) return 'is-calm'
  if (daysLeft < 0) return 'is-past'
  if (daysLeft <= 7) return 'is-urgent'
  return 'is-calm'
}

function statusLabel(st: string): { text: string; cls: string } {
  const map: Record<string, { text: string; cls: string }> = {
    created: { text: '待启动核验', cls: 'info' },
    extracting: { text: 'F1 提取红线中', cls: 'info' },
    checking: { text: '双轨核查中', cls: 'warning' },
    grading: { text: '风险定级中', cls: 'warning' },
    delivered: { text: '已完成体检', cls: 'ok' },
    need_manual: { text: '待回填报价', cls: 'warning' },
    failed: { text: '执行异常', cls: 'error' },
  }
  return map[st] || { text: st, cls: 'info' }
}

function defenseModuleLabel(mod: string): string {
  const map: Record<string, string> = {
    F1: '红线提取',
    F2: '报价沙盒',
    F3: '证照串名',
    F4: '双书勾稽',
    F5: '偏离制表',
    F6: '控标雷达',
  }
  return map[mod] || mod
}

function renderCharts(): void {
  if (!stats.value) return

  if (severityChartRef.value) {
    if (!severityChart) {
      severityChart = echarts.init(severityChartRef.value)
    }
    const veto = stats.value.veto_intercepted_count || 0
    const warn = stats.value.warning_count || 0
    const devCount = stats.value.deviation_tables_generated || 0
    const totalSignals = veto + warn + devCount
    severityChart.setOption({
      title: {
        text: String(totalSignals),
        subtext: '风险与交付合计',
        left: 'center',
        top: '34%',
        textStyle: { fontSize: 22, fontWeight: 800, color: '#0b1324' },
        subtextStyle: { fontSize: 11, color: '#64748b' },
        itemGap: 3,
      },
      tooltip: { trigger: 'item' },
      legend: { bottom: 0, itemWidth: 10, itemHeight: 10 },
      series: [
        {
          name: '封标体检态势分布',
          type: 'pie',
          radius: ['46%', '72%'],
          center: ['50%', '43%'],
          label: { show: false },
          data: [
            { value: veto, name: '一票否决废标项', itemStyle: { color: '#dc2626' } },
            { value: warn, name: '扣分与临期预警项', itemStyle: { color: '#ea580c' } },
            { value: devCount, name: '已达标生成偏离表', itemStyle: { color: '#059669' } },
          ],
        },
      ],
    })
  }
}

async function loadDashboard(): Promise<void> {
  loading.value = true
  try {
    stats.value = await dashboardApi.getStats()
    await nextTick()
    renderCharts()
  } finally {
    loading.value = false
  }
}

async function handleExportDeviation(task: { id?: number | string; title?: string }): Promise<void> {
  try {
    await deviationApi.download(Number(task.id), `${task.title ?? '体检任务'}_偏离表.docx`)
    ElMessage.success('已触发《技术与商务偏离表.docx》下载')
  } catch {
    // 拦截器已提示
  }
}

function handleEnterTask(row: Partial<BidTaskOut>): void {
  if (row.task_type === 'radar') {
    void router.push('/radar')
    return
  }
  if (row.id !== undefined) {
    appStore.setActiveTaskId(row.id)
  }
  void router.push('/checks')
}

async function handleDeleteTask(row: Partial<BidTaskOut>): Promise<void> {
  if (row.id === undefined) return
  if (row.status && ['extracting', 'checking', 'grading'].includes(row.status)) {
    ElMessage.warning('任务正在运行中，请先等待完成')
    return
  }
  const typeLabel = row.task_type === 'radar' ? '投前控标评估记录' : '封标防废标体检任务'
  try {
    await ElMessageBox.confirm(
      `确认删除${typeLabel}「#${row.id} ${row.title ?? ''}」及其关联风险发现与上传文件吗？删除后不可恢复。`,
      '确认删除任务记录',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }

  await checksApi.remove(row.id)
  if (appStore.activeTaskId === row.id) {
    appStore.setActiveTaskId(null)
  }
  ElMessage.success(`任务 #${row.id} 已删除`)
  await loadDashboard()
}

function handleResize(): void {
  severityChart?.resize()
}

onMounted(() => {
  void loadDashboard()
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  severityChart?.dispose()
})
</script>

<template>
  <div v-loading="loading" class="bg-dashboard">
    <!-- 顶部横幅与操作区 -->
    <div class="db-hero-banner bg-panel">
      <div>
        <div class="db-hero-tag">智标 · 封标前最后一道安全锁</div>
        <h1 class="db-hero-title">投标文件防废标体检与偏离表生成总览</h1>
        <p class="db-hero-sub">
          先从招标文件结构化提取四张必审清单（★零漏检），再执行精确报价与证照效期核验及双书条款级勾稽。
        </p>
      </div>
      <div class="db-hero-actions">
        <el-button type="primary" size="large" @click="router.push('/checks')">
          + 发起封标前防废标体检
        </el-button>
        <el-button size="large" @click="router.push('/extract')">
          招标红线提取
        </el-button>
        <el-button size="large" @click="loadDashboard">刷新态势</el-button>
      </div>
    </div>

    <!-- 第一排：4 张工业遥测统计卡 -->
    <div class="db-kpi-grid">
      <div class="kpi-tile bg-panel border-brand">
        <div class="kpi-label">本部门参标体检项目总数</div>
        <div class="kpi-value mono-num">
          {{ stats?.total_projects ?? 0 }}
          <span class="kpi-unit">个项目</span>
        </div>
        <div class="kpi-foot">
          封标终审 {{ stats?.pre_check_count ?? 0 }} 个 · 投前控标雷达 {{ stats?.radar_count ?? 0 }} 个
        </div>
      </div>

      <div class="kpi-tile bg-panel border-error">
        <div class="kpi-label">封标前拦截一票否决废标项</div>
        <div class="kpi-value mono-num text-error">
          {{ stats?.veto_intercepted_count ?? 0 }}
          <span class="kpi-unit">处硬伤</span>
        </div>
        <div class="kpi-foot">
          另含扣分/排他预警 {{ stats?.warning_count ?? 0 }} 处（已全部定位双书偏移）
        </div>
      </div>

      <div class="kpi-tile bg-panel border-ok">
        <div class="kpi-label">一键生成标准 Word 偏离表</div>
        <div class="kpi-value mono-num text-ok">
          {{ stats?.deviation_tables_generated ?? 0 }}
          <span class="kpi-unit">份完成</span>
        </div>
        <div class="kpi-foot">
          制表耗时由人工 2 天压缩至 15 分钟内（严格同序对位）
        </div>
      </div>

      <div class="kpi-tile bg-panel border-info">
        <div class="kpi-label">★/▲ 实质参数召回率 &amp; 证照预警</div>
        <div class="kpi-value mono-num text-info">
          {{ ((stats?.star_recall_rate ?? 1) * 100).toFixed(0) }}%
          <span class="kpi-unit">零漏检</span>
        </div>
        <div class="kpi-foot">
          企业证照库临期/过期预警：<b>{{ stats?.expiring_certs_count ?? 0 }}</b> 本
        </div>
      </div>
    </div>

    <!-- 第二排：六大一票否决防线健康度矩阵（消费 defense_matrix 6元素数组） + ECharts 图表舱 -->
    <div class="db-middle-split">
      <section class="bg-panel db-section-box">
        <div class="sec-hd">
          <div>
            <h3>封标前六大一票否决防线健康度矩阵</h3>
            <p>实时聚合本部门各防线指标（统计提取红线与★/▲条款数 · 检出风险项 · 已交付偏离表数）</p>
          </div>
          <span class="sev-pill info">6 大防线在线</span>
        </div>

        <div class="matrix-6-grid">
          <div v-for="item in defenseMatrix" :key="item.module" class="m6-card">
            <div class="m6-top">
              <span class="m6-mod">{{ defenseModuleLabel(item.module) }}</span>
              <span class="m6-count mono-num" :class="{ 'is-zero': item.hit_count === 0 }">
                {{ item.hit_count }} <small>项</small>
              </span>
            </div>
            <div class="m6-title">{{ item.title }}</div>
            <div class="m6-bar-track">
              <div class="m6-bar-fill" :style="{ width: `${defenseBarPct(item.hit_count)}%` }"></div>
            </div>
          </div>
        </div>
      </section>

      <!-- 右侧：截标倒计时预警列表 + ECharts 风险分布环形图 -->
      <section class="bg-panel db-section-box">
        <div class="sec-hd">
          <div>
            <h3>近期项目截标倒计时与废标雷区分布</h3>
            <p>按租户默认开标日或项目设定开标日实时计算剩余天数</p>
          </div>
          <span class="sev-pill error">封标前必查</span>
        </div>

        <div ref="severityChartRef" class="pie-chart-box"></div>

        <div v-if="countdownAlerts.length > 0" class="countdown-list">
          <div v-for="alert in countdownAlerts" :key="alert.task_id" class="cd-row" :class="countdownBorderClass(alert.days_left)">
            <div class="cd-main">
              <div class="cd-title-line">
                <span
                  class="sev-pill cd-type-pill"
                  :class="alert.task_type === 'radar' ? 'warning' : 'info'"
                >
                  {{ alert.task_type === 'radar' ? '控标扫描' : '双书体检' }}
                </span>
                <span class="cd-title" :title="alert.title">{{ alert.title }}</span>
              </div>
              <div class="cd-meta">
                开标日：{{ alert.open_date || '待设定' }}
                <template v-if="alert.days_left != null">
                  <span v-if="alert.days_left < 0">（已开标）</span>
                  <span v-else-if="alert.days_left === 0">（今日开标）</span>
                  <span v-else>（距开标 <b class="mono-num">{{ alert.days_left }}</b> 天）</span>
                </template>
              </div>
            </div>
            <div class="cd-badges">
              <span v-if="alert.veto_count > 0" class="sev-pill error">
                否决项 {{ alert.veto_count }}
              </span>
              <span v-if="alert.warning_count > 0" class="sev-pill warning">
                预警 {{ alert.warning_count }}
              </span>
              <span v-if="alert.veto_count === 0 && alert.warning_count === 0" class="sev-pill ok">
                {{ alert.task_type === 'radar' ? '零排他项' : '零否决项' }}
              </span>
            </div>
          </div>
        </div>
        <el-empty v-else description="暂无在途截标预警项目" :image-size="68" />
      </section>
    </div>

    <!-- 第三排：在途投标项目防废标体检台账表 -->
    <section class="bg-panel db-section-box">
      <div class="sec-hd">
        <div>
          <h3>在途投标项目防废标体检台账</h3>
          <p>实时汇总封标防废标体检与投前控标雷达记录，支持直达详情、导出偏离表或交付打包</p>
        </div>
        <el-button size="small" @click="router.push('/checks')">查看全部任务 →</el-button>
      </div>

      <el-table
        v-if="recentTasks.length > 0"
        :data="recentTasks"
        border
        stripe
        style="width: 100%"
      >
        <el-table-column prop="id" label="编号" width="80">
          <template #default="{ row }">
            <span class="mono-num">#{{ row.id }}</span>
          </template>
        </el-table-column>

        <el-table-column prop="title" label="投标项目名称与双书文件" min-width="260">
          <template #default="{ row }">
            <div class="tbl-proj-title">{{ row.title }}</div>
            <div class="tbl-proj-files">
              <span>招标书：{{ row.tender_filename || '—' }}</span>
              <span v-if="row.bid_filename || row.task_type !== 'radar'" class="tbl-file-bid">
                ｜ 投标书：{{ row.bid_filename || '—' }}
              </span>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="任务类型" width="130">
          <template #default="{ row }">
            <span class="sev-pill" :class="row.task_type === 'radar' ? 'warning' : 'info'">
              {{ row.task_type === 'radar' ? '投前控标评估' : '封标防废标体检' }}
            </span>
          </template>
        </el-table-column>

        <el-table-column label="核验状态" width="140">
          <template #default="{ row }">
            <span class="sev-pill" :class="statusLabel(row.status).cls">
              {{ statusLabel(row.status).text }}
            </span>
          </template>
        </el-table-column>

        <el-table-column label="检出统计" width="210">
          <template #default="{ row }">
            <template v-if="row.stats_json">
              <span
                class="sev-pill"
                :class="(row.stats_json.error_count ?? 0) > 0 ? 'error' : 'neutral'"
                style="margin-right: 6px"
              >
                否决 {{ row.stats_json.error_count ?? 0 }}
              </span>
              <span
                class="sev-pill"
                :class="(row.stats_json.warning_count ?? row.stats_json.suspicious_count ?? 0) > 0 ? 'warning' : 'neutral'"
              >
                预警 {{ row.stats_json.warning_count ?? row.stats_json.suspicious_count ?? 0 }}
              </span>
            </template>
            <span v-else class="text-muted">待运行核验</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="260" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="handleEnterTask(row)">
              {{ row.task_type === 'radar' ? '查看雷达' : '进入体检台' }}
            </el-button>
            <el-button
              v-if="row.task_type === 'pre_check'"
              link
              type="success"
              @click="handleExportDeviation(row)"
            >
              导出偏离表
            </el-button>
            <el-button
              v-if="row.task_type === 'pre_check'"
              link
              @click="router.push('/reports')"
            >
              交付包
            </el-button>
            <el-button
              link
              type="danger"
              :disabled="['extracting', 'checking', 'grading'].includes(row.status)"
              @click="handleDeleteTask(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-empty
        v-else
        description="当前部门暂无投标体检记录，点击右上角「+ 发起封标前防废标体检」上传招投标双书开始核验"
        :image-size="90"
      />
    </section>
  </div>
</template>

<style scoped>
.bg-dashboard {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.db-hero-banner {
  padding: 22px 26px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  flex-wrap: wrap;
  background: linear-gradient(120deg, #ffffff 0%, #f0f7ff 100%);
  border-left: 5px solid var(--brand);
}

.db-hero-tag {
  font-size: 11px;
  font-weight: 800;
  color: var(--brand);
  letter-spacing: 0.6px;
  margin-bottom: 4px;
}

.db-hero-title {
  font-size: 21px;
  font-weight: 900;
  color: var(--txt-strong);
}

.db-hero-sub {
  font-size: 12.5px;
  color: var(--txt-sec);
  margin-top: 4px;
}

.db-hero-actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

.db-kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}

.kpi-tile {
  padding: 18px 20px;
  border-top: 3px solid transparent;
}

.kpi-tile.border-brand {
  border-top-color: var(--brand);
}
.kpi-tile.border-error {
  border-top-color: var(--sev-error);
}
.kpi-tile.border-ok {
  border-top-color: var(--sev-ok);
}
.kpi-tile.border-info {
  border-top-color: var(--sev-info);
}

.kpi-label {
  font-size: 12.5px;
  font-weight: 700;
  color: var(--txt-sec);
  margin-bottom: 8px;
}

.kpi-value {
  font-size: 29px;
  font-weight: 900;
  color: var(--txt-strong);
  line-height: 1.15;
  margin-bottom: 8px;
}

.kpi-value.text-error {
  color: var(--sev-error);
}
.kpi-value.text-ok {
  color: var(--sev-ok);
}
.kpi-value.text-info {
  color: var(--sev-info);
}

.kpi-unit {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--txt-muted);
  margin-left: 4px;
}

.kpi-foot {
  font-size: 12px;
  color: var(--txt-muted);
}

.db-middle-split {
  display: grid;
  grid-template-columns: 1.2fr 1fr;
  gap: 16px;
}

.db-section-box {
  padding: 20px 22px;
}

.sec-hd {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-line);
}

.sec-hd h3 {
  font-size: 15.5px;
  font-weight: 800;
  color: var(--txt-strong);
}

.sec-hd p {
  font-size: 12px;
  color: var(--txt-muted);
  margin-top: 2px;
}

.matrix-6-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
  margin-bottom: 14px;
}

.m6-card {
  padding: 11px 13px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-line);
  border-radius: 8px;
}

.m6-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.m6-mod {
  font-size: 12px;
  font-weight: 900;
  color: var(--brand);
}

.m6-title {
  font-size: 12.5px;
  font-weight: 700;
  color: var(--txt-strong);
}

.matrix-chart-box {
  width: 100%;
  height: 210px;
}

.pie-chart-box {
  width: 100%;
  height: 190px;
  margin-bottom: 12px;
}

.countdown-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 290px;
  overflow-y: auto;
  padding-right: 4px;
}

.cd-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid var(--border-line);
  border-left: 3px solid var(--sev-error);
  background: #ffffff;
}

.cd-main {
  min-width: 0;
  flex: 1;
}

.cd-title-line {
  display: flex;
  align-items: center;
  gap: 6px;
}

.cd-type-pill {
  flex-shrink: 0;
  font-size: 10.5px;
  padding: 1px 6px;
}

.cd-title {
  font-size: 13px;
  font-weight: 800;
  color: var(--txt-strong);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cd-meta {
  font-size: 11.5px;
  color: var(--txt-muted);
  margin-top: 2px;
}

.cd-badges {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

.tbl-proj-title {
  font-weight: 800;
  color: var(--txt-strong);
}

.tbl-proj-files {
  font-size: 11.5px;
  color: var(--txt-muted);
  margin-top: 2px;
}

.text-muted {
  color: var(--txt-muted);
  font-size: 12px;
}

@media (max-width: 1200px) {
  .db-kpi-grid {
    grid-template-columns: repeat(2, 1fr);
  }
  .db-middle-split {
    grid-template-columns: 1fr;
  }
}

.m6-count {
  font-size: 16px;
  font-weight: 800;
  color: var(--txt-strong, #0b1324);
  line-height: 1.2;
}

.m6-count small {
  font-size: 11px;
  font-weight: 500;
  color: var(--txt-muted, #64748b);
}

.m6-count.is-zero {
  color: var(--txt-muted, #64748b);
}

.m6-bar-track {
  margin-top: 8px;
  height: 4px;
  border-radius: 99px;
  background: #e6ebf2;
  overflow: hidden;
}

.m6-bar-fill {
  height: 100%;
  border-radius: 99px;
  background: var(--brand, #1d4ed8);
  transition: width 0.3s ease;
}

.cd-row.is-past {
  border-left-color: #cbd5e1;
}

.cd-row.is-calm {
  border-left-color: var(--brand, #1d4ed8);
}

.cd-row.is-urgent {
  border-left-color: var(--sev-error, #dc2626);
}

.tbl-file-bid {
  white-space: nowrap;
}
</style>