<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { radarApi, validateUploadFile } from '@/api'
import { formatPageOrOffset, type BidTaskOut, type RadarScanOut } from '@/api/types'
import { RADAR_CACHE_KEY, useAuthStore } from '@/stores/auth'

defineOptions({ name: 'RadarView' })

const authStore = useAuthStore()

const loading = ref(false)
const listLoading = ref(false)
const historyTasks = ref<BidTaskOut[]>([])
const tenderFile = ref<File | null>(null)
const customTitle = ref('')
const lastAutoTitle = ref('')
const scanResult = ref<RadarScanOut | null>(null)

interface CachedRadarPayload {
  username: string
  data: RadarScanOut
}

function saveToSessionStorage(data: RadarScanOut): void {
  try {
    const payload: CachedRadarPayload = {
      username: authStore.user?.username || '',
      data,
    }
    sessionStorage.setItem(RADAR_CACHE_KEY, JSON.stringify(payload))
  } catch {
    // ignore storage errors
  }
}

function restoreFromSessionStorage(): void {
  if (scanResult.value) return
  try {
    const raw = sessionStorage.getItem(RADAR_CACHE_KEY)
    if (!raw) return
    const parsed = JSON.parse(raw) as Partial<CachedRadarPayload>
    const curUser = authStore.user?.username || ''
    if (parsed && parsed.data && (!parsed.username || parsed.username === curUser)) {
      scanResult.value = parsed.data
      customTitle.value = parsed.data.title || ''
    } else {
      sessionStorage.removeItem(RADAR_CACHE_KEY)
    }
  } catch {
    // ignore
  }
}

async function loadHistoryList(autoSelectFirst = false): Promise<void> {
  listLoading.value = true
  try {
    const res = await radarApi.list()
    historyTasks.value = res.items || []
    if (
      scanResult.value &&
      historyTasks.value.length > 0 &&
      !historyTasks.value.some((t) => t.id === scanResult.value?.task_id)
    ) {
      scanResult.value = null
      sessionStorage.removeItem(RADAR_CACHE_KEY)
    } else if (scanResult.value && historyTasks.value.length === 0) {
      scanResult.value = null
      sessionStorage.removeItem(RADAR_CACHE_KEY)
    }
    if (autoSelectFirst && !scanResult.value && historyTasks.value.length > 0) {
      const first = historyTasks.value[0]
      if (first) {
        await handleSelectHistory(first)
      }
    }
  } finally {
    listLoading.value = false
  }
}

async function handleSelectHistory(task: BidTaskOut): Promise<void> {
  loading.value = true
  try {
    const res = await radarApi.detail(task.id)
    scanResult.value = res
    saveToSessionStorage(res)
  } finally {
    loading.value = false
  }
}

async function handleDeleteHistory(task: BidTaskOut): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除控标雷达扫描记录「#${task.id} ${task.title}」及其关联发现与上传标书文件吗？删除后不可恢复。`,
      '确认删除扫描记录',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }

  await radarApi.remove(task.id)
  ElMessage.success(`控标雷达记录 #${task.id} 已删除`)

  if (scanResult.value?.task_id === task.id) {
    scanResult.value = null
    sessionStorage.removeItem(RADAR_CACHE_KEY)
    await loadHistoryList(true)
  } else {
    await loadHistoryList(false)
  }
}

function handleAuthChanged(): void {
  scanResult.value = null
  historyTasks.value = []
  tenderFile.value = null
  customTitle.value = ''
  lastAutoTitle.value = ''
}

function onPickFile(ev: Event): void {
  const input = ev.target as HTMLInputElement
  const f = input.files?.[0]
  if (!f) return
  const check = validateUploadFile(f)
  if (!check.valid) {
    ElMessage.warning(check.message)
    input.value = ''
    return
  }
  tenderFile.value = f
  const autoTitle = `${f.name.replace(/\.[^.]+$/, '')}-投前控标评估`
  // 标题为空或仍是上一个文件的自动标题（用户未手动定制）时，跟随新文件刷新，
  // 否则连续扫描多份标书时右侧文本框永远显示第一次的标书名（生产实锤 2026-11-09）
  if (!customTitle.value.trim() || customTitle.value === lastAutoTitle.value) {
    customTitle.value = autoTitle
  }
  lastAutoTitle.value = autoTitle
}

async function handleRunRadar(): Promise<void> {
  if (!tenderFile.value) {
    ElMessage.warning('请先选择待扫描的招标文件（.docx / .pdf / .txt）')
    return
  }
  loading.value = true
  try {
    const res = await radarApi.scan(tenderFile.value, customTitle.value)
    scanResult.value = res
    saveToSessionStorage(res)
    await loadHistoryList(false)
    ElMessage.success('F6 投前控标雷达扫描完成')
  } finally {
    loading.value = false
  }
}

function recommendationBannerCls(rec?: string): string {
  if (rec === '值得投') return 'rec-ok'
  if (rec === '疑似控标高') return 'rec-high'
  return 'rec-caution'
}

function recommendationPillCls(rec?: string): string {
  if (rec === '值得投') return 'ok'
  if (rec === '疑似控标高') return 'error'
  return 'warning'
}

function exclusiveTypeCls(exType: string): string {
  if (exType === '品牌绑定') return 'error'
  if (exType === '资质定向') return 'warning'
  if (exType === '业绩定向') return 'info'
  return 'warning'
}

onMounted(() => {
  restoreFromSessionStorage()
  void loadHistoryList(true)
  window.addEventListener('bidguard-auth-changed', handleAuthChanged)
})

onBeforeUnmount(() => {
  window.removeEventListener('bidguard-auth-changed', handleAuthChanged)
})
</script>

<template>
  <div class="radar-workbench">
    <!-- 左栏：F6 控标雷达扫描历史列表 -->
    <aside class="radar-rail bg-panel">
      <div class="rr-hd">
        <div>
          <div class="rr-title">控标雷达扫描历史</div>
          <div class="rr-sub">共 {{ historyTasks.length }} 条记录</div>
        </div>
        <el-button size="small" @click="loadHistoryList(false)">刷新</el-button>
      </div>

      <div v-loading="listLoading" class="rr-scroll">
        <div
          v-for="item in historyTasks"
          :key="item.id"
          class="rr-item"
          :class="{ active: scanResult?.task_id === item.id }"
          @click="handleSelectHistory(item)"
        >
          <div class="rri-row1">
            <span class="rri-id mono-num">#{{ item.id }}</span>
            <div class="rri-row1-right">
              <span
                class="sev-pill"
                :class="recommendationPillCls(item.stats_json?.recommendation)"
              >
                {{ item.stats_json?.recommendation || '已扫描' }}
              </span>
              <el-button
                type="danger"
                link
                size="small"
                class="rri-del-btn"
                title="删除该扫描记录"
                @click.stop="handleDeleteHistory(item)"
              >
                删除
              </el-button>
            </div>
          </div>

          <div class="rri-title" :title="item.title">{{ item.title }}</div>
          <div class="rri-file" :title="item.tender_filename || ''">
            招：{{ item.tender_filename || '—' }}
          </div>
          <div class="rri-meta">
            <span>疑似项 {{ item.stats_json?.suspicious_count ?? 0 }} 处</span>
            <span class="mono-num">{{ (item.created_at || '').slice(0, 16).replace('T', ' ') }}</span>
          </div>
        </div>

        <el-empty
          v-if="!listLoading && historyTasks.length === 0"
          description="暂无扫描历史，请在右侧上传招标书启动扫描"
          :image-size="60"
        />
      </div>
    </aside>

    <!-- 右栏：上传面板 + 扫描详情 -->
    <div v-loading="loading" class="radar-view">
      <!-- 顶部上传与扫描触发面板 -->
      <section class="bg-panel radar-upload-card">
        <div class="ru-left">
          <div class="ru-tag">F6 · 投前控标雷达（立项决策辅助流）</div>
          <h2>刚拿到招标书？30 秒扫描疑似排他性控标参数</h2>
          <p>
            识别指定或参照特定品牌型号、原厂唯一授权函、特定软著全称绑定及毫米级非标尺寸；
            合法国产化/信创政策导向自动免检不误报。
          </p>
        </div>

        <div class="ru-controls">
          <input
            type="file"
            accept=".docx,.pdf,.txt"
            class="radar-file-input"
            @change="onPickFile"
          />
          <el-input
            v-model.trim="customTitle"
            placeholder="立项评估任务名称（可选）"
            style="width: 240px"
          />
          <el-button type="primary" :loading="loading" @click="handleRunRadar">
            启动控标雷达扫描
          </el-button>
        </div>
      </section>

      <template v-if="scanResult">
        <!-- 结论横幅（强制带"疑似"标注，明确声明不替用户拍板） -->
        <section
          class="conclusion-banner bg-panel"
          :class="recommendationBannerCls(scanResult.conclusion.recommendation)"
        >
          <div class="cb-top">
            <div class="cb-badge-row">
              <span class="cb-rec-pill">
                #{{ scanResult.task_id }} {{ scanResult.title }} · 投前评估参考意见：{{
                  scanResult.conclusion.recommendation
                }}（疑似筛查）
              </span>
              <span class="sev-pill info">
                检出疑似排他参数 {{ scanResult.suspicious_params.length }} 处
              </span>
              <span class="sev-pill" :class="scanResult.llm_status === 'done' ? 'ok' : 'info'">
                {{ scanResult.llm_status === 'done' ? '确定性 + 大模型④ 双层复核' : '0-Token 确定性模式' }}
              </span>
            </div>
          </div>
          <div class="cb-summary">{{ scanResult.conclusion.summary }}</div>
          <div class="cb-disclaimer">
            合规边界说明：以上结论及下方参数均标注为“疑似”，仅作为投标立项与发起标前澄清提疑的参考线索，最终是否参标由业务负责人决策拍板。
          </div>
        </section>

        <!-- 可疑排他参数卡片流 -->
        <section class="bg-panel radar-list-panel">
          <div class="rl-hd">
            <h3>检出疑似排他性 / 倾向性控标参数明细（{{ scanResult.suspicious_params.length }} 处）</h3>
          </div>

          <div v-if="scanResult.suspicious_params.length > 0" class="param-cards">
            <div
              v-for="(item, idx) in scanResult.suspicious_params"
              :key="`${item.offset}_${idx}`"
              class="p-card"
            >
              <div class="pc-head">
                <span class="pc-seq mono-num">#{{ idx + 1 }}</span>
                <span class="sev-pill" :class="exclusiveTypeCls(item.exclusive_type)">
                  疑似{{ item.exclusive_type }}
                </span>
                <span class="pc-loc mono-num">
                  招标书 {{ formatPageOrOffset(item.page ?? null, item.offset) }}
                </span>
                <span v-if="item.quote_verified" class="pc-verified">✓ 原文逐字溯源</span>

                <div class="pc-conf-wrap">
                  <span class="conf-lbl">疑似置信度</span>
                  <div class="conf-bar-Track">
                    <el-progress
                      :percentage="Math.round((item.confidence || 0.8) * 100)"
                      :stroke-width="8"
                      color="#ea580c"
                    />
                  </div>
                </div>
              </div>

              <div class="pc-reason">
                <b>疑似排他研判理由：</b>{{ item.reason }}
              </div>

              <div class="pc-quote">
                <div class="pq-label">招标文件原文逐字摘录：</div>
                <div class="pq-body">“{{ item.quote }}”</div>
              </div>
            </div>
          </div>

          <el-empty
            v-else
            description="本次扫描未检出明显疑似排他性或品牌定向参数，招标参数整体开放合规"
            :image-size="80"
          />
        </section>
      </template>

      <div v-else class="bg-panel radar-empty">
        <el-empty description="请在上方选择招标书启动控标雷达扫描，或在左侧点击历史记录回放" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.radar-workbench {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: 18px;
  align-items: start;
}

.radar-rail {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 110px);
  min-height: 580px;
  padding: 16px;
}

.rr-hd {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-line);
  margin-bottom: 12px;
}

.rr-title {
  font-size: 14.5px;
  font-weight: 900;
  color: var(--txt-strong);
}

.rr-sub {
  font-size: 11.5px;
  color: var(--txt-muted);
}

.rr-scroll {
  flex: 1;
  overflow-y: auto;
  padding-right: 4px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.rr-item {
  padding: 12px;
  border-radius: 8px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
  cursor: pointer;
  transition: all 0.15s;
}

.rr-item:hover {
  border-color: var(--brand);
  background: #ffffff;
}

.rr-item.active {
  border-color: var(--brand);
  background: var(--brand-soft);
  box-shadow: 0 0 0 2px rgba(29, 78, 216, 0.12);
}

.rri-row1 {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 5px;
}

.rri-id {
  font-size: 12px;
  font-weight: 800;
  color: var(--txt-muted);
}

.rri-row1-right {
  display: flex;
  align-items: center;
  gap: 6px;
}

.rri-del-btn {
  font-size: 11.5px;
  padding: 0 2px;
}

.rri-title {
  font-size: 13.5px;
  font-weight: 800;
  color: var(--txt-strong);
  margin-bottom: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rri-file {
  font-size: 11.5px;
  color: var(--txt-sec);
  margin-bottom: 6px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rri-meta {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--txt-muted);
}

.radar-view {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.radar-empty {
  padding: 60px 24px;
}

@media (max-width: 1100px) {
  .radar-workbench {
    grid-template-columns: 1fr;
  }
}

.radar-upload-card {
  padding: 20px 24px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 18px;
  flex-wrap: wrap;
}

.ru-tag {
  font-size: 11px;
  font-weight: 800;
  color: var(--brand);
  margin-bottom: 4px;
}

.ru-left h2 {
  font-size: 18px;
  font-weight: 900;
  color: var(--txt-strong);
}

.ru-left p {
  font-size: 12.5px;
  color: var(--txt-sec);
  margin-top: 4px;
}

.ru-controls {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.radar-file-input {
  font-size: 12.5px;
}

.conclusion-banner {
  padding: 20px 24px;
  border-left: 5px solid var(--sev-warning);
}

.conclusion-banner.rec-ok {
  border-left-color: var(--sev-ok);
  background: #f0fdf4;
}

.conclusion-banner.rec-caution {
  border-left-color: var(--sev-warning);
  background: #fffbeb;
}

.conclusion-banner.rec-high {
  border-left-color: var(--sev-error);
  background: #fef2f2;
}

.cb-badge-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.cb-rec-pill {
  font-size: 15px;
  font-weight: 900;
  color: var(--txt-strong);
}

.cb-summary {
  font-size: 14px;
  font-weight: 700;
  color: var(--txt-body);
  line-height: 1.6;
  margin-bottom: 8px;
}

.cb-disclaimer {
  font-size: 12px;
  color: var(--txt-muted);
}

.radar-list-panel {
  padding: 18px 22px;
}

.rl-hd {
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-line);
  margin-bottom: 14px;
}

.rl-hd h3 {
  font-size: 15px;
  font-weight: 900;
  color: var(--txt-strong);
}

.param-cards {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.p-card {
  padding: 14px 16px;
  border-radius: 9px;
  border: 1px solid var(--border-line);
  border-left: 4px solid var(--sev-warning);
  background: #ffffff;
}

.pc-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.pc-seq {
  font-weight: 900;
  color: var(--txt-strong);
}

.pc-loc {
  font-size: 12px;
  color: var(--txt-muted);
}

.pc-verified {
  font-size: 11.5px;
  font-weight: 800;
  color: var(--sev-ok);
}

.pc-conf-wrap {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}

.conf-lbl {
  font-size: 11.5px;
  font-weight: 700;
  color: var(--txt-sec);
}

.conf-bar-Track {
  width: 140px;
}

.pc-reason {
  font-size: 13.5px;
  color: var(--txt-strong);
  margin-bottom: 10px;
}

.pc-quote {
  padding: 10px 12px;
  background: var(--bg-subtle);
  border-radius: 6px;
  border: 1px solid var(--border-line);
}

.pq-label {
  font-size: 11px;
  font-weight: 800;
  color: var(--txt-muted);
  margin-bottom: 4px;
}

.pq-body {
  font-size: 12.5px;
  color: var(--txt-body);
  line-height: 1.6;
}
</style>