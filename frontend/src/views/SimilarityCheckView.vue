<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { similarityApi, validateUploadFile } from '@/api'
import {
  formatMoneyDisplay,
  formatPageOrOffset,
  type SimilarityPairOut,
  type SimilarityReportDetailOut,
  type SimilarityReportSummaryOut,
  type SimilarityRiskLevel,
} from '@/api/types'

defineOptions({ name: 'SimilarityCheckView' })

interface PendingBidItem {
  id: string
  file: File
  label: string
}

const titleInput = ref('')
const tenderFile = ref<File | null>(null)
const pendingBids = ref<PendingBidItem[]>([])
const comparing = ref(false)
const exporting = ref(false)

const loadingHistory = ref(false)
const historyList = ref<SimilarityReportSummaryOut[]>([])
const activeReport = ref<SimilarityReportDetailOut | null>(null)

const activeTab = ref<'findings' | 'passages' | 'bidders'>('findings')
const dimFilter = ref<string>('ALL')
const selectedPairKey = ref<string>('ALL')

const DIMENSION_LABELS: Record<string, string> = {
  metadata_fingerprint: '元数据与内嵌图片',
  net_text_similarity: '净正文相似度',
  fingerprint_error: '异常指纹与外部串名',
  entity_collision: '主体与脱敏隐私号碰撞',
  price_regularity: '报价规律性关联',
}

function riskLabel(level: SimilarityRiskLevel | string): string {
  if (level === 'high') return '高风险'
  if (level === 'medium') return '中风险'
  return '低风险'
}

function riskPillClass(level: SimilarityRiskLevel | string): string {
  if (level === 'high') return 'error'
  if (level === 'medium') return 'warning'
  return 'ok'
}

function severityPillClass(sev: string): string {
  if (sev === 'error') return 'error'
  if (sev === 'warning') return 'warning'
  return 'neutral'
}

function formatDateTime(iso?: string | null): string {
  if (!iso) return '—'
  const clean = iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`
  const dt = new Date(clean)
  if (Number.isNaN(dt.getTime())) return iso.slice(0, 19).replace('T', ' ')
  return dt.toLocaleString('zh-CN', { hour12: false })
}

function onTenderSelect(ev: Event): void {
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

function onBidsSelect(ev: Event): void {
  const input = ev.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  if (files.length === 0) return

  for (const f of files) {
    if (pendingBids.value.length >= 6) {
      ElMessage.warning('单次最多支持比对 6 份投标文件')
      break
    }
    const chk = validateUploadFile(f)
    if (!chk.valid) {
      ElMessage.warning(chk.message)
      continue
    }
    const stem = f.name.includes('.') ? f.name.slice(0, f.name.lastIndexOf('.')) : f.name
    pendingBids.value.push({
      id: `${f.name}-${f.size}-${Date.now()}-${Math.random()}`,
      file: f,
      label: stem.slice(0, 32) || `标书${pendingBids.value.length + 1}`,
    })
  }
}

function removePendingBid(idx: number): void {
  pendingBids.value.splice(idx, 1)
}

async function fetchHistory(): Promise<void> {
  loadingHistory.value = true
  try {
    const res = await similarityApi.list()
    historyList.value = res.items || []
    if (!activeReport.value && historyList.value.length > 0) {
      await loadReportDetail(historyList.value[0].id)
    }
  } finally {
    loadingHistory.value = false
  }
}

async function loadReportDetail(reportId: number): Promise<void> {
  const detail = await similarityApi.detail(reportId)
  activeReport.value = detail
  dimFilter.value = 'ALL'
  selectedPairKey.value = 'ALL'
}

async function handleRunCompare(): Promise<void> {
  if (pendingBids.value.length < 2 || pendingBids.value.length > 6) {
    ElMessage.warning('请上传 2 至 6 份参选投标文件进行交叉比对')
    return
  }
  comparing.value = true
  try {
    const detail = await similarityApi.compare({
      bidFiles: pendingBids.value.map((x) => x.file),
      labels: pendingBids.value.map((x) => x.label.trim() || x.file.name),
      tenderFile: tenderFile.value,
      title: titleInput.value.trim() || undefined,
    })
    activeReport.value = detail
    dimFilter.value = 'ALL'
    selectedPairKey.value = 'ALL'
    activeTab.value = 'findings'
    ElMessage.success('多标书交叉比对分析完成')
    await fetchHistory()
  } finally {
    comparing.value = false
  }
}

async function handleExportDocx(): Promise<void> {
  if (!activeReport.value) return
  exporting.value = true
  try {
    const fn = await similarityApi.downloadReport(
      activeReport.value.id,
      `${activeReport.value.title}_交叉比对分析报告.docx`,
    )
    ElMessage.success(`已导出对比报告：${fn}`)
  } finally {
    exporting.value = false
  }
}

async function handleDeleteReport(item: SimilarityReportSummaryOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除比对记录「${item.title}」及关联的上传标书文件吗？此操作不可撤销。`,
      '确认删除比对记录',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  await similarityApi.remove(item.id)
  ElMessage.success('已删除比对记录')
  if (activeReport.value?.id === item.id) {
    activeReport.value = null
  }
  await fetchHistory()
}

function pairKey(aLabel: string, bLabel: string): string {
  return `${aLabel}__vs__${bLabel}`
}

function findPairCell(idxA: number, idxB: number): SimilarityPairOut | null {
  if (!activeReport.value || idxA === idxB) return null
  const lo = Math.min(idxA, idxB)
  const hi = Math.max(idxA, idxB)
  return (
    activeReport.value.pair_matrix.find((p) => p.a_index === lo && p.b_index === hi) || null
  )
}

function selectPairFromMatrix(pair: SimilarityPairOut | null): void {
  if (!pair) return
  const key = pairKey(pair.a_label, pair.b_label)
  selectedPairKey.value = selectedPairKey.value === key ? 'ALL' : key
  activeTab.value = pair.shared_passage_count > 0 ? 'passages' : 'findings'
}

const filteredFindings = computed(() => {
  const list = activeReport.value?.findings || []
  return list.filter((f) => {
    if (dimFilter.value !== 'ALL' && f.dimension !== dimFilter.value) return false
    if (selectedPairKey.value !== 'ALL') {
      const k1 = pairKey(f.a_label, f.b_label)
      const k2 = pairKey(f.b_label, f.a_label)
      if (k1 !== selectedPairKey.value && k2 !== selectedPairKey.value) return false
    }
    return true
  })
})

const filteredPairs = computed(() => {
  const pairs = activeReport.value?.pair_matrix || []
  if (selectedPairKey.value === 'ALL') return pairs
  return pairs.filter((p) => pairKey(p.a_label, p.b_label) === selectedPairKey.value)
})

onMounted(() => {
  void fetchHistory()
})
</script>

<template>
  <div class="similarity-view">
    <!-- 顶部上传与比对控制台 -->
    <section class="bg-panel sim-upload-card">
      <div class="sim-hd">
        <div>
          <h2>多标书交叉比对与相似度分析</h2>
          <p>
            上传 2~6 份参选投标文件（.docx / .pdf / .txt），建议同时传入《招标文件》以自动剥离公共条款消除误报；全程 0-Token 本地计算，隐私字段严格脱敏。
          </p>
        </div>
        <div class="sim-hd-actions">
          <el-input
            v-model="titleInput"
            placeholder="可选：输入本次比对任务名称"
            style="width: 240px"
            clearable
          />
          <el-button
            type="primary"
            :loading="comparing"
            :disabled="pendingBids.length < 2"
            @click="handleRunCompare"
          >
            {{ comparing ? '正在执行五维比对…' : `开始交叉比对 (${pendingBids.length}/6)` }}
          </el-button>
        </div>
      </div>

      <div class="sim-upload-grid">
        <!-- 左：可选招标底稿 -->
        <div class="sim-drop-box tender-box">
          <div class="box-title">
            <span>① 可选参照：《招标文件》</span>
            <el-tag size="small" type="info">防误报核心</el-tag>
          </div>
          <p class="box-desc">上传招标书后，比对引擎会自动扣除招标原条款与格式要求，只算真正自编正文的净相似度。</p>
          <div class="box-controls">
            <label class="pick-btn">
              <span>{{ tenderFile ? '更换招标文件' : '+ 选择招标文件 (.docx/.pdf/.txt)' }}</span>
              <input type="file" accept=".docx,.pdf,.txt" style="display: none" @change="onTenderSelect" />
            </label>
            <div v-if="tenderFile" class="picked-chip">
              <span class="fn">{{ tenderFile.name }}</span>
              <el-button link type="danger" size="small" @click="tenderFile = null">移除</el-button>
            </div>
          </div>
        </div>

        <!-- 右：2~6 份参选投标书 -->
        <div class="sim-drop-box bids-box">
          <div class="box-title">
            <span>② 参选投标文件列表（必填 2~6 份）</span>
            <label class="pick-btn primary">
              <span>+ 添加投标书（可多选）</span>
              <input
                type="file"
                multiple
                accept=".docx,.pdf,.txt"
                style="display: none"
                @change="onBidsSelect"
              />
            </label>
          </div>

          <div v-if="pendingBids.length === 0" class="empty-bids-hint">
            尚未选择投标书，请点击右上角选择 2 至 6 份参选文件（支持自定义每份文件简称）。
          </div>
          <div v-else class="pending-bid-list">
            <div v-for="(item, idx) in pendingBids" :key="item.id" class="pending-bid-row">
              <span class="idx-badge mono-num">#{{ idx + 1 }}</span>
              <el-input
                v-model="item.label"
                size="small"
                placeholder="文件简称（如：单位A）"
                style="width: 150px"
              />
              <span class="fn-txt" :title="item.file.name">{{ item.file.name }}</span>
              <el-button link type="danger" size="small" @click="removePendingBid(idx)">移除</el-button>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- 主工作区：左历史列表 + 右对比总览与五维证据 -->
    <div class="sim-workspace">
      <aside class="bg-panel sim-history-pane" v-loading="loadingHistory">
        <div class="pane-hd">
          <span>历史比对记录 ({{ historyList.length }})</span>
          <el-button link type="primary" size="small" @click="fetchHistory">刷新</el-button>
        </div>

        <el-empty v-if="historyList.length === 0" description="暂无比对记录" :image-size="56" />
        <div v-else class="history-items">
          <div
            v-for="item in historyList"
            :key="item.id"
            class="history-item"
            :class="{ 'is-active': activeReport?.id === item.id }"
            @click="loadReportDetail(item.id)"
          >
            <div class="hi-top">
              <span class="hi-title">{{ item.title }}</span>
              <span class="sev-pill" :class="riskPillClass(item.risk_level)">
                {{ riskLabel(item.risk_level) }}
              </span>
            </div>
            <div class="hi-meta mono-num">
              <span>{{ item.bidder_count }} 份标书</span>
              <span>最高净相似 {{ item.max_similarity_pct.toFixed(2) }}%</span>
            </div>
            <div class="hi-foot">
              <span class="hi-time">{{ formatDateTime(item.created_at) }}</span>
              <el-button link type="danger" size="small" @click.stop="handleDeleteReport(item)">
                删除
              </el-button>
            </div>
          </div>
        </div>
      </aside>

      <main class="bg-panel sim-detail-pane">
        <el-empty
          v-if="!activeReport"
          description="请上传 2~6 份投标文件发起比对，或从左侧选择历史记录查看分析结果"
        />

        <template v-else>
          <div class="report-hd">
            <div>
              <div class="rh-title-row">
                <h3>{{ activeReport.title }}</h3>
                <span class="sev-pill" :class="riskPillClass(activeReport.risk_level)">
                  {{ riskLabel(activeReport.risk_level) }} · 最高净相似度 {{ activeReport.max_similarity_pct.toFixed(2) }}%
                </span>
                <el-tag v-if="activeReport.tender_filename" size="small" type="success">
                  已剔除招标底稿：{{ activeReport.tender_filename }}
                </el-tag>
                <el-tag v-else size="small" type="warning">
                  未上传招标底稿（含公共条款）
                </el-tag>
              </div>
            </div>
            <el-button type="primary" :loading="exporting" @click="handleExportDocx">
              导出比对分析报告 (.docx)
            </el-button>
          </div>

          <!-- 两两 N x N 净相似度矩阵热力表 -->
          <div class="matrix-section">
            <div class="sec-sub-hd">
              <span>两两对比净相似度矩阵（点击非对角格子可按组合筛选下方明细）</span>
              <el-button
                v-if="selectedPairKey !== 'ALL'"
                link
                type="primary"
                size="small"
                @click="selectedPairKey = 'ALL'"
              >
                清除组合筛选
              </el-button>
            </div>

            <div class="matrix-scroll">
              <table class="sim-matrix-table">
                <thead>
                  <tr>
                    <th>参选文件</th>
                    <th v-for="b in activeReport.bidders" :key="b.index">{{ b.label }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="rowB in activeReport.bidders" :key="rowB.index">
                    <td class="row-label">{{ rowB.label }}</td>
                    <td
                      v-for="colB in activeReport.bidders"
                      :key="colB.index"
                      :class="[
                        rowB.index === colB.index
                          ? 'diag-cell'
                          : `risk-cell-${findPairCell(rowB.index, colB.index)?.risk_level || 'low'}`,
                        {
                          'is-selected':
                            rowB.index !== colB.index &&
                            pairKey(
                              findPairCell(rowB.index, colB.index)?.a_label || '',
                              findPairCell(rowB.index, colB.index)?.b_label || '',
                            ) === selectedPairKey,
                        },
                      ]"
                      @click="selectPairFromMatrix(findPairCell(rowB.index, colB.index))"
                    >
                      <template v-if="rowB.index === colB.index">—</template>
                      <template v-else-if="findPairCell(rowB.index, colB.index)">
                        <div class="cell-net mono-num">
                          净 {{ findPairCell(rowB.index, colB.index)!.net_similarity_pct.toFixed(2) }}%
                        </div>
                        <div class="cell-raw mono-num">
                          原文 {{ findPairCell(rowB.index, colB.index)!.raw_similarity_pct.toFixed(2) }}%
                        </div>
                      </template>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <!-- 证据明细页签 -->
          <el-tabs v-model="activeTab" class="sim-tabs">
            <el-tab-pane :label="`五维检测证据 (${filteredFindings.length})`" name="findings">
              <div class="dim-filter-bar">
                <el-radio-group v-model="dimFilter" size="small">
                  <el-radio-button value="ALL">全部维度</el-radio-button>
                  <el-radio-button v-for="(lbl, key) in DIMENSION_LABELS" :key="key" :value="key">
                    {{ lbl }}
                  </el-radio-button>
                </el-radio-group>
              </div>

              <el-empty v-if="filteredFindings.length === 0" description="当前筛选下暂无异常撞车提示" :image-size="56" />
              <div v-else class="findings-list">
                <div v-for="(f, idx) in filteredFindings" :key="idx" class="finding-card">
                  <div class="fc-top">
                    <span class="sev-pill" :class="severityPillClass(f.severity)">
                      {{ f.severity === 'error' ? '高危异常' : f.severity === 'warning' ? '关注预警' : '提示' }}
                    </span>
                    <span class="fc-dim">{{ DIMENSION_LABELS[f.dimension] || f.dimension }}</span>
                    <span class="fc-code mono-num">{{ f.code }}</span>
                    <span class="fc-pair">{{ f.a_label }} ↔ {{ f.b_label }}</span>
                  </div>
                  <div class="fc-msg">{{ f.message }}</div>
                </div>
              </div>
            </el-tab-pane>

            <el-tab-pane label="连续雷同原句双栏对照" name="passages">
              <div v-for="pr in filteredPairs" :key="`${pr.a_index}-${pr.b_index}`" class="pair-passages-block">
                <div class="pp-hd">
                  <b>{{ pr.a_label }} ↔ {{ pr.b_label }}</b>
                  <span class="mono-num">
                    净相似度 {{ pr.net_similarity_pct.toFixed(2) }}% ｜ 共 {{ pr.shared_passage_count }} 处 ≥25 字一致自编句段
                  </span>
                </div>
                <div v-if="pr.shared_passages.length === 0" class="muted-empty">
                  剔除公共条款后未检出 ≥25 字连续一致自编长句。
                </div>
                <div v-else class="passage-cards">
                  <div v-for="(ps, pIdx) in pr.shared_passages" :key="pIdx" class="passage-card">
                    <div class="pc-head mono-num">
                      <span>一致长句 #{{ pIdx + 1 }}（连续 {{ ps.norm_chars }} 字）</span>
                      <span>
                        {{ pr.a_label }}: {{ formatPageOrOffset(ps.a_page ?? null, ps.a_offset) }}
                        ｜ {{ pr.b_label }}: {{ formatPageOrOffset(ps.b_page ?? null, ps.b_offset) }}
                      </span>
                    </div>
                    <div class="pc-cols">
                      <div class="pc-col">
                        <div class="pc-col-label">{{ pr.a_label }} 原文</div>
                        <div class="pc-quote">{{ ps.a_quote }}</div>
                      </div>
                      <div class="pc-col">
                        <div class="pc-col-label">{{ pr.b_label }} 原文</div>
                        <div class="pc-quote">{{ ps.b_quote }}</div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </el-tab-pane>

            <el-tab-pane :label="`文档指纹与报价概览 (${activeReport.bidders.length})`" name="bidders">
              <el-table :data="activeReport.bidders" border size="small" style="width: 100%">
                <el-table-column prop="label" label="简称" width="140" />
                <el-table-column prop="filename" label="文件名" min-width="200" />
                <el-table-column label="总字数 / 净字数" width="145">
                  <template #default="{ row }">
                    <span class="mono-num">{{ row.chars }} / {{ row.net_chars }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="文档作者 / 修改人" min-width="170">
                  <template #default="{ row }">
                    <span>{{
                      [row.metadata?.author, row.metadata?.last_modified_by].filter(Boolean).join(' / ') || '—'
                    }}</span>
                  </template>
                </el-table-column>
                <el-table-column prop="media_count" label="内嵌图片" width="90" align="center" />
                <el-table-column label="投标总价 (元)" width="150">
                  <template #default="{ row }">
                    <span class="mono-num">{{ formatMoneyDisplay(row.bid_total) }}</span>
                  </template>
                </el-table-column>
              </el-table>
            </el-tab-pane>
          </el-tabs>
        </template>
      </main>
    </div>
  </div>
</template>

<style scoped>
.similarity-view {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.sim-upload-card {
  padding: 18px 22px;
}

.sim-hd {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 14px;
  margin-bottom: 14px;
}

.sim-hd h2 {
  margin: 0 0 4px 0;
  font-size: 16px;
  font-weight: 800;
  color: var(--txt-strong);
}

.sim-hd p {
  margin: 0;
  font-size: 12.5px;
  color: var(--txt-sec);
}

.sim-hd-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.sim-upload-grid {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 14px;
}

@media (max-width: 960px) {
  .sim-upload-grid {
    grid-template-columns: 1fr;
  }
}

.sim-drop-box {
  border: 1px dashed #cbd5e1;
  border-radius: 8px;
  padding: 12px 14px;
  background: #f8fafc;
}

.box-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: 700;
  font-size: 13px;
  color: var(--txt-strong);
  margin-bottom: 6px;
}

.box-desc {
  font-size: 12px;
  color: var(--txt-sec);
  margin: 0 0 10px 0;
  line-height: 1.45;
}

.box-controls {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.pick-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 5px 12px;
  border-radius: 6px;
  border: 1px solid #cbd5e1;
  background: #fff;
  color: #334155;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}

.pick-btn.primary {
  border-color: var(--brand);
  color: var(--brand);
}

.picked-chip {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 8px;
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  font-size: 12px;
}

.picked-chip .fn {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.empty-bids-hint {
  font-size: 12px;
  color: #64748b;
  padding: 14px 0;
}

.pending-bid-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 8px;
}

.pending-bid-row {
  display: flex;
  align-items: center;
  gap: 8px;
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 5px 8px;
}

.idx-badge {
  font-size: 11px;
  font-weight: 700;
  color: #64748b;
  width: 24px;
}

.fn-txt {
  flex: 1;
  font-size: 12px;
  color: #334155;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sim-workspace {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: 16px;
  align-items: start;
}

@media (max-width: 1024px) {
  .sim-workspace {
    grid-template-columns: 1fr;
  }
}

.sim-history-pane {
  padding: 14px;
}

.pane-hd {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
  font-weight: 700;
  margin-bottom: 10px;
}

.history-items {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.history-item {
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 10px;
  cursor: pointer;
  background: #fff;
  transition: border-color 0.15s;
}

.history-item.is-active {
  border-color: var(--brand);
  background: var(--brand-soft);
}

.hi-top {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 6px;
  margin-bottom: 4px;
}

.hi-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--txt-strong);
  word-break: break-word;
}

.hi-meta {
  display: flex;
  justify-content: space-between;
  font-size: 11.5px;
  color: #475569;
  margin-bottom: 4px;
}

.hi-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.hi-time {
  font-size: 11px;
  color: #94a3b8;
}

.sim-detail-pane {
  padding: 18px 20px;
  min-width: 0;
}

.report-hd {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-line);
  margin-bottom: 14px;
}

.rh-title-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.rh-title-row h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 800;
  color: var(--txt-strong);
}

.matrix-section {
  margin-bottom: 16px;
}

.sec-sub-hd {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12.5px;
  font-weight: 700;
  color: #334155;
  margin-bottom: 8px;
}

.matrix-scroll {
  overflow-x: auto;
}

.sim-matrix-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  text-align: center;
}

.sim-matrix-table th,
.sim-matrix-table td {
  border: 1px solid #cbd5e1;
  padding: 8px 10px;
}

.sim-matrix-table th {
  background: #f1f5f9;
  font-weight: 700;
  color: #0f172a;
}

.row-label {
  background: #f8fafc;
  font-weight: 700;
  text-align: left !important;
}

.diag-cell {
  background: #f8fafc;
  color: #94a3b8;
}

.risk-cell-low {
  background: #ecfdf5;
  color: #065f46;
  cursor: pointer;
}

.risk-cell-medium {
  background: #fffbeb;
  color: #92400e;
  cursor: pointer;
}

.risk-cell-high {
  background: #fef2f2;
  color: #991b1b;
  cursor: pointer;
}

.is-selected {
  outline: 2px solid var(--brand);
  outline-offset: -2px;
}

.cell-net {
  font-weight: 700;
}

.cell-raw {
  font-size: 11px;
  opacity: 0.8;
}

.dim-filter-bar {
  margin-bottom: 12px;
}

.findings-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.finding-card {
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 10px 12px;
  background: #fff;
}

.fc-top {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 4px;
}

.fc-dim {
  font-size: 12px;
  font-weight: 700;
  color: #334155;
}

.fc-code {
  font-size: 11px;
  color: #64748b;
}

.fc-pair {
  font-size: 12px;
  color: #0f172a;
  font-weight: 600;
}

.fc-msg {
  font-size: 13px;
  color: #1e293b;
  line-height: 1.5;
}

.pair-passages-block {
  margin-bottom: 16px;
}

.pp-hd {
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 13px;
  padding: 6px 10px;
  background: #f1f5f9;
  border-radius: 6px;
  margin-bottom: 8px;
}

.muted-empty {
  font-size: 12px;
  color: #64748b;
  padding: 6px 10px;
}

.passage-cards {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.passage-card {
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 10px;
  background: #fff;
}

.pc-head {
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 11.5px;
  color: #475569;
  margin-bottom: 8px;
}

.pc-cols {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

@media (max-width: 820px) {
  .pc-cols {
    grid-template-columns: 1fr;
  }
}

.pc-col {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 4px;
  padding: 8px;
}

.pc-col-label {
  font-size: 11.5px;
  font-weight: 700;
  color: #334155;
  margin-bottom: 4px;
}

.pc-quote {
  font-size: 12.5px;
  color: #0f172a;
  line-height: 1.5;
  word-break: break-word;
}
</style>
