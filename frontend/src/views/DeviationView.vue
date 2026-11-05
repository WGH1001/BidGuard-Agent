<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus/es/components/message/index'
import { checksApi, deviationApi } from '@/api'
import type { BidTaskOut, DeviationExportOut } from '@/api/types'
import { useAppStore } from '@/stores/app'

const route = useRoute()
const appStore = useAppStore()

const loadingTasks = ref(false)
const generating = ref(false)
const downloading = ref(false)

const tasks = ref<BidTaskOut[]>([])
const selectedTaskId = ref<number | null>(null)
const devResult = ref<DeviationExportOut | null>(null)

async function loadAvailableTasks(): Promise<void> {
  loadingTasks.value = true
  try {
    const res = await checksApi.list({ task_type: 'pre_check', page: 1, page_size: 50 })
    tasks.value = res.items || []

    const queryTid = Number(route.query.task_id)
    if (queryTid && tasks.value.some((t) => t.id === queryTid)) {
      selectedTaskId.value = queryTid
    } else if (appStore.activeTaskId && tasks.value.some((t) => t.id === appStore.activeTaskId)) {
      selectedTaskId.value = appStore.activeTaskId
    } else if (tasks.value.length > 0) {
      selectedTaskId.value = tasks.value[0].id
    }

    if (selectedTaskId.value) {
      await handleLoadDeviation()
    }
  } finally {
    loadingTasks.value = false
  }
}

/** 进页面/切换任务：调 GET，产物已存在时后端秒回复用 */
async function handleLoadDeviation(): Promise<void> {
  if (!selectedTaskId.value) return
  generating.value = true
  try {
    devResult.value = await deviationApi.get(selectedTaskId.value)
  } finally {
    generating.value = false
  }
}

/** 仅"重新生成"按钮：强制全量重算 */
async function handleGenerateDeviation(): Promise<void> {
  if (!selectedTaskId.value) {
    ElMessage.warning('请先选择一个防废标体检任务')
    return
  }
  generating.value = true
  try {
    devResult.value = await deviationApi.generate(selectedTaskId.value)
    ElMessage.success(`偏离表生成完成：共对齐 ${devResult.value.total_rows} 条实质性条款`)
  } finally {
    generating.value = false
  }
}

async function handleDownloadDocx(): Promise<void> {
  if (!devResult.value) return
  downloading.value = true
  try {
    const fn = await deviationApi.downloadByUrl(
      devResult.value.download_url,
      devResult.value.output_filename || `技术与商务偏离表_${devResult.value.task_id}.docx`,
    )
    ElMessage.success(`已下载标准 Word 偏离表：${fn}`)
  } finally {
    downloading.value = false
  }
}

function rowClassName({ row }: { row: { is_negative?: boolean } }): string {
  return row.is_negative ? 'neg-deviation-row' : ''
}

onMounted(() => {
  void loadAvailableTasks()
})
</script>

<template>
  <div v-loading="generating" class="deviation-page">
    <!-- 顶部任务选择与操作条 -->
    <section class="bg-panel dev-header-bar">
      <div class="dh-left">
        <div class="dh-tag">F5 · 《技术与商务偏离表.docx》一键对齐生成器</div>
        <h2>招标技术/商务规范与我方投标响应逐条对齐制表</h2>
        <p>
          严格消费 F4 双书勾稽三态结果（同序对位、零二次篡改），将人工 2 天制表时间压缩至 15 分钟内。
        </p>
      </div>

      <div class="dh-controls">
        <el-select
          v-model="selectedTaskId"
          :loading="loadingTasks"
          placeholder="选择已上传双书的体检任务"
          style="width: 320px"
          @change="handleLoadDeviation"
        >
          <el-option
            v-for="t in tasks"
            :key="t.id"
            :value="t.id"
            :label="`#${t.id} ${t.title}`"
          />
        </el-select>

        <el-button type="primary" :loading="generating" @click="handleGenerateDeviation">
          重新生成偏离表
        </el-button>

        <el-button
          v-if="devResult"
          type="success"
          :loading="downloading"
          @click="handleDownloadDocx"
        >
          下载 Word 偏离表 (.docx)
        </el-button>
      </div>
    </section>

    <template v-if="devResult">
      <!-- 统计概览卡片（负偏离与漏应答红色高亮） -->
      <div class="dev-stats-grid">
        <div class="ds-card bg-panel">
          <div class="ds-lbl">核验实质性要求总数</div>
          <div class="ds-val mono-num">{{ devResult.total_rows }}</div>
        </div>

        <div class="ds-card bg-panel ok-card">
          <div class="ds-lbl">无偏离（正面响应满足）</div>
          <div class="ds-val mono-num ok-txt">{{ devResult.no_deviation_count }}</div>
        </div>

        <div class="ds-card bg-panel err-card">
          <div class="ds-lbl">负偏离总数（高危废标/扣分）</div>
          <div class="ds-val mono-num err-txt">{{ devResult.negative_deviation_count }}</div>
        </div>

        <div class="ds-card bg-panel err-card">
          <div class="ds-lbl">其中：实质性条款漏应答</div>
          <div class="ds-val mono-num err-txt">{{ devResult.missing_count }}</div>
        </div>

        <div class="ds-card bg-panel warn-card">
          <div class="ds-lbl">其中：参数响应冲突</div>
          <div class="ds-val mono-num warn-txt">{{ devResult.contradiction_count }}</div>
        </div>
      </div>

      <!-- 标准六列偏离表在线预览 -->
      <section class="bg-panel dev-table-card">
        <div class="dt-hd">
          <h3>《技术与商务偏离表》六列标准预览（{{ devResult.output_filename }}）</h3>
          <span class="dt-note">
            注：PDF 显示真实页码，docx 显示字符偏移 @N；负偏离与漏应答行已用红色加粗高亮。
          </span>
        </div>

        <el-table
          :data="devResult.rows"
          border
          :row-class-name="rowClassName"
          style="width: 100%"
        >
          <el-table-column prop="seq" label="序号" width="70" align="center" />
          <el-table-column prop="clause_no" label="招标条款号" width="120" />
          <el-table-column prop="tender_requirement" label="招标文件要求" min-width="260" />
          <el-table-column prop="bid_response" label="我方投标响应" min-width="240">
            <template #default="{ row }">
              <span :class="{ 'missing-placeholder': row.status === 'MISSING_RESPONSE' }">
                {{ row.bid_response }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="偏离情况" width="210">
            <template #default="{ row }">
              <span :class="row.is_negative ? 'dev-neg-text' : 'dev-ok-text'">
                {{ row.deviation }}
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="location" label="位置索引（页码/字符偏移）" width="220">
            <template #default="{ row }">
              <span class="mono-num loc-cell">{{ row.location }}</span>
            </template>
          </el-table-column>
        </el-table>
      </section>
    </template>

    <div v-else class="bg-panel dev-empty">
      <el-empty description="请选择一个防废标体检任务以生成并预览《技术与商务偏离表》" />
    </div>
  </div>
</template>

<style scoped>
.deviation-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.dev-header-bar {
  padding: 20px 24px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 18px;
  flex-wrap: wrap;
}

.dh-tag {
  font-size: 11px;
  font-weight: 800;
  color: var(--brand);
  margin-bottom: 4px;
}

.dh-left h2 {
  font-size: 18px;
  font-weight: 900;
  color: var(--txt-strong);
}

.dh-left p {
  font-size: 12.5px;
  color: var(--txt-sec);
  margin-top: 4px;
}

.dh-controls {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.dev-stats-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 14px;
}

.ds-card {
  padding: 16px 18px;
  border-top: 3px solid var(--brand);
}

.ds-card.ok-card {
  border-top-color: var(--sev-ok);
}

.ds-card.err-card {
  border-top-color: var(--sev-error);
  background: #fffafa;
}

.ds-card.warn-card {
  border-top-color: var(--sev-warning);
}

.ds-lbl {
  font-size: 12px;
  font-weight: 700;
  color: var(--txt-sec);
  margin-bottom: 6px;
}

.ds-val {
  font-size: 26px;
  font-weight: 900;
  color: var(--txt-strong);
}

.ds-val.ok-txt {
  color: var(--sev-ok);
}

.ds-val.err-txt {
  color: var(--sev-error);
}

.ds-val.warn-txt {
  color: var(--sev-warning);
}

.dev-table-card {
  padding: 18px 22px;
}

.dt-hd {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 14px;
}

.dt-hd h3 {
  font-size: 15px;
  font-weight: 900;
  color: var(--txt-strong);
}

.dt-note {
  font-size: 12px;
  color: var(--txt-muted);
}

.dev-neg-text {
  color: var(--sev-error);
  font-weight: 900;
}

.dev-ok-text {
  color: var(--sev-ok);
  font-weight: 700;
}

.missing-placeholder {
  color: var(--sev-error);
  font-weight: 700;
}

.loc-cell {
  font-size: 12px;
  color: var(--txt-sec);
}

:deep(.neg-deviation-row) {
  background-color: #fef2f2 !important;
}

.dev-empty {
  padding: 64px 24px;
}
</style>