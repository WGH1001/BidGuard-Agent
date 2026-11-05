<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { radarApi, validateUploadFile } from '@/api'
import { formatPageOrOffset, type RadarScanOut } from '@/api/types'

const loading = ref(false)
const tenderFile = ref<File | null>(null)
const customTitle = ref('')
const scanResult = ref<RadarScanOut | null>(null)

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
  if (!customTitle.value.trim()) {
    customTitle.value = `${f.name.replace(/\.[^.]+$/, '')}-投前控标评估`
  }
}

async function handleRunRadar(): Promise<void> {
  if (!tenderFile.value) {
    ElMessage.warning('请先选择待扫描的招标文件（.docx / .pdf / .txt）')
    return
  }
  loading.value = true
  try {
    scanResult.value = await radarApi.scan(tenderFile.value, customTitle.value)
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

function exclusiveTypeCls(exType: string): string {
  if (exType === '品牌绑定') return 'error'
  if (exType === '资质定向') return 'warning'
  if (exType === '业绩定向') return 'info'
  return 'warning'
}
</script>

<template>
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
              投前评估参考意见：{{ scanResult.conclusion.recommendation }}（疑似筛查）
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
                招标书 {{ formatPageOrOffset(null, item.offset) }}
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
  </div>
</template>

<style scoped>
.radar-view {
  display: flex;
  flex-direction: column;
  gap: 16px;
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