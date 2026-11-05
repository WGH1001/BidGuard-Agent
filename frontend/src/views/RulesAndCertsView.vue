<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { rulesApi } from '@/api'
import {
  formatMoneyDisplay,
  type CertificateOut,
  type CustomRedlineItem,
} from '@/api/types'

const activeTab = ref<'rules' | 'certs'>('rules')
const loadingRules = ref(false)
const savingRules = ref(false)
const loadingCerts = ref(false)
const expiringOnly = ref(false)

// ── 页签 ①：核查规则与自定义红线状态 ────────────────────────────────────
const AVAILABLE_MONTHS = [
  '2026-01', '2026-02', '2026-03', '2026-04', '2026-05', '2026-06',
  '2026-07', '2026-08', '2026-09', '2026-10', '2026-11', '2026-12',
]

const ruleForm = reactive({
  name: '政企投标默认防废标规则',
  price_cap: '1450000.00',
  open_date: '2026-11-05',
  buyer_name: '',
  required_social_months_json: ['2026-08', '2026-09', '2026-10'] as string[],
  custom_redlines_json: [] as CustomRedlineItem[],
})

async function fetchRuleConfig(): Promise<void> {
  loadingRules.value = true
  try {
    const list = await rulesApi.listRules()
    const cfg = list.find((r) => r.is_default) || list[0]
    if (cfg) {
      ruleForm.name = cfg.name || '默认防废标核查规则'
      ruleForm.price_cap = cfg.price_cap || ''
      ruleForm.open_date = cfg.open_date || '2026-11-05'
      ruleForm.buyer_name = cfg.buyer_name || ''
      ruleForm.required_social_months_json = [...(cfg.required_social_months_json || [])]
      ruleForm.custom_redlines_json = (cfg.custom_redlines_json || []).map((item) => ({
        keyword: item.keyword,
        severity: item.severity,
        consequence: item.consequence,
      }))
    }
  } finally {
    loadingRules.value = false
  }
}

function addRedlineRow(): void {
  ruleForm.custom_redlines_json.push({
    keyword: '',
    severity: 'error',
    consequence: 'veto',
  })
}

function removeRedlineRow(idx: number): void {
  ruleForm.custom_redlines_json.splice(idx, 1)
}

async function handleSaveRuleConfig(): Promise<void> {
  const cleanedRedlines = ruleForm.custom_redlines_json
    .map((r) => ({
      keyword: r.keyword.trim(),
      severity: r.severity,
      consequence: r.consequence,
    }))
    .filter((r) => r.keyword.length > 0)

  const cleanCap = String(ruleForm.price_cap || '').replace(/,/g, '').trim()

  savingRules.value = true
  try {
    const saved = await rulesApi.saveRule({
      name: ruleForm.name.trim() || '默认核查规则',
      price_cap: cleanCap || null,
      open_date: ruleForm.open_date.trim() || null,
      buyer_name: ruleForm.buyer_name.trim() || null,
      required_social_months_json: ruleForm.required_social_months_json,
      custom_redlines_json: cleanedRedlines,
      is_default: true,
    })
    ruleForm.custom_redlines_json = [...(saved.custom_redlines_json || [])]
    ElMessage.success('防废标核查参数与自定义红线已保存生效')
  } finally {
    savingRules.value = false
  }
}

// ── 页签 ②：企业证照效期台账 CRUD 状态 ──────────────────────────────────
const certs = ref<CertificateOut[]>([])
const certDialogVisible = ref(false)
const savingCert = ref(false)
const editingCertId = ref<number | null>(null)

const certForm = reactive({
  cert_name: '',
  cert_no: '',
  valid_until: '2026-11-20',
  warn_days: 30,
  remark: '',
})

async function fetchCerts(): Promise<void> {
  loadingCerts.value = true
  try {
    certs.value = await rulesApi.listCertificates(expiringOnly.value)
  } finally {
    loadingCerts.value = false
  }
}

function openCreateCert(): void {
  editingCertId.value = null
  certForm.cert_name = ''
  certForm.cert_no = ''
  certForm.valid_until = '2027-06-30'
  certForm.warn_days = 30
  certForm.remark = ''
  certDialogVisible.value = true
}

function openEditCert(row: Partial<CertificateOut>): void {
  editingCertId.value = row.id ?? null
  certForm.cert_name = row.cert_name ?? ''
  certForm.cert_no = row.cert_no || ''
  certForm.valid_until = row.valid_until ?? ''
  certForm.warn_days = row.warn_days ?? 30
  certForm.remark = row.remark || ''
  certDialogVisible.value = true
}

async function handleSaveCert(): Promise<void> {
  if (!certForm.cert_name.trim() || !certForm.valid_until.trim()) {
    ElMessage.warning('请填写证照名称与有效期截止日（YYYY-MM-DD）')
    return
  }
  savingCert.value = true
  try {
    const payload = {
      cert_name: certForm.cert_name.trim(),
      cert_no: certForm.cert_no.trim() || null,
      valid_until: certForm.valid_until.trim(),
      warn_days: Number(certForm.warn_days) || 30,
      remark: certForm.remark.trim() || null,
    }
    if (editingCertId.value === null) {
      await rulesApi.createCertificate(payload)
      ElMessage.success('已新增企业投标证照记录')
    } else {
      await rulesApi.updateCertificate(editingCertId.value, payload)
      ElMessage.success('证照记录已更新')
    }
    certDialogVisible.value = false
    await fetchCerts()
  } finally {
    savingCert.value = false
  }
}

async function handleDeleteCert(row: Partial<CertificateOut>): Promise<void> {
  if (row.id === undefined) return
  await rulesApi.deleteCertificate(row.id)
  ElMessage.success(`已删除证照「${row.cert_name ?? ''}」`)
  await fetchCerts()
}

function certRowClassName({ row }: { row: CertificateOut }): string {
  if (row.is_expired) return 'cert-expired-row'
  if (row.is_expiring_soon) return 'cert-expiring-row'
  return ''
}

onMounted(() => {
  void fetchRuleConfig()
  void fetchCerts()
})
</script>

<template>
  <div class="rules-certs-view">
    <div class="bg-panel rc-main-card">
      <el-tabs v-model="activeTab">
        <!-- 页签 ①：核查规则与自定义禁用词红线 -->
        <el-tab-pane label="① 封标防废标核查规则配置" name="rules">
          <div v-loading="loadingRules" class="rule-pane">
            <div class="rp-top-bar">
              <div>
                <h3>部门默认防废标体检基准参数（F2 / F3 确定性核验底座）</h3>
                <p>配置最高投标限价、计划开标日、采购人名称、社保证明必含月份与自定义红线禁用词。</p>
              </div>
              <el-button type="primary" :loading="savingRules" @click="handleSaveRuleConfig">
                保存核查规则配置
              </el-button>
            </div>

            <div class="rule-grid-2">
              <div class="rf-field">
                <label>规则配置方案名称</label>
                <el-input v-model="ruleForm.name" placeholder="例如：政企集成部默认核查规则" />
              </div>

              <div class="rf-field">
                <label>招标最高限价 / 预算上限 (元)</label>
                <el-input v-model="ruleForm.price_cap" placeholder="例如：1450000.00" />
                <div class="money-preview mono-num">
                  千分位预览：¥{{ formatMoneyDisplay(ruleForm.price_cap) }}
                </div>
              </div>

              <div class="rf-field">
                <label>默认计划开标日期 (YYYY-MM-DD · F3 证照有效期核验基准日)</label>
                <el-input v-model="ruleForm.open_date" placeholder="例如：2026-11-05" />
              </div>

              <div class="rf-field">
                <label>本项目采购人 / 招标单位全称（F3 跨项目串名扫描防自伤保护）</label>
                <el-input v-model="ruleForm.buyer_name" placeholder="例如：某省政务云管理中心" />
              </div>
            </div>

            <div class="rf-field" style="margin-top: 16px">
              <label>招标要求的社保证明覆盖月份（多选或直接输入 YYYY-MM 后回车）</label>
              <el-select
                v-model="ruleForm.required_social_months_json"
                multiple
                filterable
                allow-create
                default-first-option
                placeholder="选择或输入社保所属期月份（如 2026-08）"
                style="width: 100%"
              >
                <el-option v-for="m in AVAILABLE_MONTHS" :key="m" :label="m" :value="m" />
              </el-select>
            </div>

            <!-- 自定义红线动态行编辑（keyword + severity + consequence 增删行） -->
            <div class="custom-redlines-section">
              <div class="crs-hd">
                <div>
                  <h4>部门自定义禁用词与跨项目串名红线列表（{{ ruleForm.custom_redlines_json.length }} 条）</h4>
                  <p>投标书全文一旦命中以下禁用关键词（如历史投标项目的采购单位名、竞争对手品牌名），立即触发告警。</p>
                </div>
                <el-button size="small" type="primary" plain @click="addRedlineRow">
                  + 新增红线词行
                </el-button>
              </div>

              <el-table
                :data="ruleForm.custom_redlines_json"
                border
                size="small"
                style="width: 100%"
              >
                <el-table-column label="禁用关键词 / 历史外部单位名称 (keyword)" min-width="260">
                  <template #default="{ row }">
                    <el-input
                      v-model="row.keyword"
                      size="small"
                      placeholder="例如：沈阳市某某大数据局 / 某某历史项目名称"
                    />
                  </template>
                </el-table-column>

                <el-table-column label="严重级别 (severity)" width="180">
                  <template #default="{ row }">
                    <el-select v-model="row.severity" size="small" style="width: 100%">
                      <el-option label="error · 废标红线" value="error" />
                      <el-option label="warning · 扣分预警" value="warning" />
                      <el-option label="info · 一般提示" value="info" />
                    </el-select>
                  </template>
                </el-table-column>

                <el-table-column label="评分后果 (consequence)" width="190">
                  <template #default="{ row }">
                    <el-select v-model="row.consequence" size="small" style="width: 100%">
                      <el-option label="veto · ★一票否决" value="veto" />
                      <el-option label="deduction · ▲重要扣分" value="deduction" />
                      <el-option label="bonus · ▲重要加分" value="bonus" />
                      <el-option label="none · 无评分后果" value="none" />
                    </el-select>
                  </template>
                </el-table-column>

                <el-table-column label="操作" width="90" align="center">
                  <template #default="{ $index }">
                    <el-button link type="danger" @click="removeRedlineRow($index)">
                      删除行
                    </el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </div>
        </el-tab-pane>

        <!-- 页签 ②：企业投标证照效期台账 -->
        <el-tab-pane label="② 企业投标证照效期台账" name="certs">
          <div v-loading="loadingCerts" class="cert-pane">
            <div class="rp-top-bar">
              <div>
                <h3>企业常备投标证照与信创互认证书效期台账</h3>
                <p>自动计算距今剩余有效天数；距离到期不足预警天数（warn_days）的行自动以橙色高亮预警。</p>
              </div>

              <div class="cert-actions">
                <el-checkbox v-model="expiringOnly" @change="fetchCerts">
                  仅看临期 / 已过期证照
                </el-checkbox>
                <el-button size="small" @click="fetchCerts">刷新</el-button>
                <el-button type="primary" size="small" @click="openCreateCert">
                  + 新增企业证照
                </el-button>
              </div>
            </div>

            <el-table
              :data="certs"
              border
              :row-class-name="certRowClassName"
              style="width: 100%"
            >
              <el-table-column prop="id" label="ID" width="70">
                <template #default="{ row }">
                  <span class="mono-num">#{{ row.id }}</span>
                </template>
              </el-table-column>

              <el-table-column prop="cert_name" label="证照 / 资质证书名称" min-width="240" />
              <el-table-column prop="cert_no" label="证书编号" width="190">
                <template #default="{ row }">
                  <span class="mono-num">{{ row.cert_no || '—' }}</span>
                </template>
              </el-table-column>

              <el-table-column prop="valid_until" label="有效期截止日" width="140">
                <template #default="{ row }">
                  <span class="mono-num">{{ row.valid_until }}</span>
                </template>
              </el-table-column>

              <el-table-column label="剩余天数与状态" width="210">
                <template #default="{ row }">
                  <span v-if="row.is_expired" class="sev-pill error">
                    已过期 {{ Math.abs(row.days_remaining) }} 天
                  </span>
                  <span v-else-if="row.is_expiring_soon" class="sev-pill warning">
                    临期预警 · 仅剩 {{ row.days_remaining }} 天
                  </span>
                  <span v-else class="sev-pill ok">
                    有效 · 剩余 {{ row.days_remaining }} 天
                  </span>
                </template>
              </el-table-column>

              <el-table-column prop="warn_days" label="预警阈值" width="100">
                <template #default="{ row }">
                  <span class="mono-num">≤ {{ row.warn_days }} 天</span>
                </template>
              </el-table-column>

              <el-table-column prop="remark" label="适用投标场景备注" min-width="180" />

              <el-table-column label="操作" width="140" fixed="right">
                <template #default="{ row }">
                  <el-button link type="primary" @click="openEditCert(row)">编辑</el-button>
                  <el-button link type="danger" @click="handleDeleteCert(row)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>

    <!-- 新增/编辑证照弹窗 -->
    <el-dialog
      v-model="certDialogVisible"
      :title="editingCertId === null ? '新增企业投标证照' : '编辑企业投标证照'"
      width="520px"
    >
      <el-form label-position="top">
        <el-form-item label="证照名称" required>
          <el-input
            v-model="certForm.cert_name"
            placeholder="例如：ISO/IEC 27001 信息安全管理体系认证证书"
          />
        </el-form-item>

        <el-form-item label="证书编号">
          <el-input v-model="certForm.cert_no" placeholder="例如：ISO27001-CN-2026001" />
        </el-form-item>

        <div class="rule-grid-2">
          <el-form-item label="有效期截止日期 (YYYY-MM-DD)" required>
            <el-input v-model="certForm.valid_until" placeholder="2026-11-25" />
          </el-form-item>

          <el-form-item label="提前预警天数 (warn_days)">
            <el-input v-model.number="certForm.warn_days" type="number" />
          </el-form-item>
        </div>

        <el-form-item label="备注说明">
          <el-input v-model="certForm.remark" placeholder="例如：政企安全项目资格审查必附项" />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="certDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingCert" @click="handleSaveCert">
          保存证照
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.rc-main-card {
  padding: 20px 24px;
}

.rp-top-bar {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  flex-wrap: wrap;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--border-line);
  margin-bottom: 18px;
}

.rp-top-bar h3 {
  font-size: 16px;
  font-weight: 900;
  color: var(--txt-strong);
}

.rp-top-bar p {
  font-size: 12.5px;
  color: var(--txt-sec);
  margin-top: 3px;
}

.rule-grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.rf-field label {
  display: block;
  font-size: 12.5px;
  font-weight: 800;
  color: var(--txt-strong);
  margin-bottom: 6px;
}

.money-preview {
  font-size: 11.5px;
  color: var(--brand);
  margin-top: 4px;
  font-weight: 700;
}

.custom-redlines-section {
  margin-top: 24px;
  padding-top: 18px;
  border-top: 1px dashed var(--border-strong);
}

.crs-hd {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  gap: 12px;
  flex-wrap: wrap;
}

.crs-hd h4 {
  font-size: 14.5px;
  font-weight: 900;
  color: var(--txt-strong);
}

.crs-hd p {
  font-size: 12px;
  color: var(--txt-muted);
  margin-top: 2px;
}

.cert-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

/* 证照临期行橙色高亮 & 过期行红色高亮 */
:deep(.cert-expiring-row) {
  background-color: #fff7ed !important;
}

:deep(.cert-expired-row) {
  background-color: #fef2f2 !important;
}
</style>