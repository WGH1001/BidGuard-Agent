<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { regulationsApi, rulesApi } from '@/api'
import {
  formatMoneyDisplay,
  type CertificateOut,
  type CustomRedlineItem,
  type RegulationArticleOut,
  type RegulationCrawlOut,
  type RegulationLawOut,
  type VerifyArticleOut,
} from '@/api/types'
import { useAuthStore } from '@/stores/auth'

defineOptions({ name: 'RulesAndCertsView' })

const authStore = useAuthStore()

const activeTab = ref<'rules' | 'certs' | 'regulations'>('rules')
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

// ── 页签 ③：E4 法规库浏览、关键词检索、法条三态真伪核验与官网更新状态 ─────
const loadingLaws = ref(false)
const loadingArticles = ref(false)
const laws = ref<RegulationLawOut[]>([])
const selectedLawId = ref<number | null>(null)
const selectedLaw = ref<RegulationLawOut | null>(null)
const articles = ref<RegulationArticleOut[]>([])
const totalArticles = ref(0)
const articlePage = ref(1)
const articlePageSize = ref(30)
const articleKeyword = ref('')

// 法条真伪核验状态
const verifyInputText = ref('')
const verifying = ref(false)
const verifyResult = ref<VerifyArticleOut | null>(null)

// 官网更新与手动导入状态（admin）
const crawling = ref(false)
const crawlReport = ref<RegulationCrawlOut | null>(null)
const importingFile = ref(false)
const importLawNameHint = ref('')

async function fetchLaws(autoSelectFirst = true): Promise<void> {
  loadingLaws.value = true
  try {
    laws.value = await regulationsApi.listLaws()
    if (autoSelectFirst && laws.value.length > 0 && selectedLawId.value === null) {
      await handleSelectLaw(laws.value[0])
    } else if (selectedLawId.value !== null) {
      const matched = laws.value.find((l) => l.id === selectedLawId.value)
      if (matched) {
        selectedLaw.value = matched
        await fetchLawArticles()
      }
    }
  } finally {
    loadingLaws.value = false
  }
}

async function handleSelectLaw(law: RegulationLawOut): Promise<void> {
  selectedLawId.value = law.id
  selectedLaw.value = law
  articlePage.value = 1
  await fetchLawArticles()
}

async function fetchLawArticles(): Promise<void> {
  if (selectedLawId.value === null) return
  loadingArticles.value = true
  try {
    const detail = await regulationsApi.getLawDetail(selectedLawId.value, {
      keyword: articleKeyword.value.trim() || undefined,
      page: articlePage.value,
      page_size: articlePageSize.value,
    })
    selectedLaw.value = detail.law
    articles.value = detail.articles || []
    totalArticles.value = detail.total_articles || 0
  } finally {
    loadingArticles.value = false
  }
}

function handleSearchArticles(): void {
  articlePage.value = 1
  void fetchLawArticles()
}

function handleArticlePageChange(p: number): void {
  articlePage.value = p
  void fetchLawArticles()
}

async function handleVerifyArticle(): Promise<void> {
  const clean = verifyInputText.value.trim()
  if (!clean) {
    ElMessage.warning('请粘贴或输入待核验的法条文本')
    return
  }
  verifying.value = true
  try {
    verifyResult.value = await regulationsApi.verifyArticle(clean)
  } finally {
    verifying.value = false
  }
}

function fillSampleForVerify(art: RegulationArticleOut): void {
  const lawTitle = selectedLaw.value?.name || ''
  verifyInputText.value = `${lawTitle}${art.article_no} ${art.content}`
  void handleVerifyArticle()
}

async function handleCrawlUpdates(): Promise<void> {
  crawling.value = true
  try {
    crawlReport.value = await regulationsApi.crawlUpdates()
    await fetchLaws(false)
    ElMessage.info(
      `官网更新检查完成：共检查 ${crawlReport.value.total_sources} 个源，` +
        `更新 ${crawlReport.value.updated_laws_count} 部，未配置占位源 ${crawlReport.value.unconfigured_count} 个`,
    )
  } finally {
    crawling.value = false
  }
}

async function handleImportLawFile(ev: Event): Promise<void> {
  const input = ev.target as HTMLInputElement
  const f = input.files?.[0]
  if (!f) return
  importingFile.value = true
  try {
    const res = await regulationsApi.importFile(f, importLawNameHint.value)
    ElMessage.success(
      `成功导入 ${res.law_name}：共 ${res.article_count} 条（新增 ${res.added_count} 条，修订 ${res.modified_count} 条）`,
    )
    importLawNameHint.value = ''
    await fetchLaws(false)
    const found = laws.value.find((l) => l.id === res.law_id)
    if (found) {
      await handleSelectLaw(found)
    }
  } finally {
    importingFile.value = false
    input.value = ''
  }
}

onMounted(() => {
  void fetchRuleConfig()
  void fetchCerts()
  void fetchLaws(true)
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

            <!-- 自定义红线动态行编辑 -->
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

        <!-- 页签 ③：E4 法规库浏览、法条三态真伪核验与官网更新 -->
        <el-tab-pane label="③ 法规库与法条核验" name="regulations">
          <div class="regulations-pane">
            <!-- 1. 法条三态真伪核验区（exact_match 绿勾 / fuzzy_hint 灰标 / unverifiable 红标） -->
            <div class="verify-box">
              <div class="vb-hd">
                <div>
                  <h4>法条真伪三态溯源核验台（LLM 零真伪裁决权 · 纯确定性底表对账）</h4>
                  <p>
                    粘贴任意法律依据或条文文本（自动兼容“第1条”与“第一条”变体），实时与本地权威法规库逐字对账。
                  </p>
                </div>
                <el-button type="primary" :loading="verifying" @click="handleVerifyArticle">
                  立即核验法条真伪
                </el-button>
              </div>

              <el-input
                v-model="verifyInputText"
                type="textarea"
                :rows="2"
                placeholder="粘贴待核验法条，例如：《中华人民共和国招标投标法》第1条 为了规范招标投标活动，保护国家利益、社会公共利益和招标投标活动当事人的合法权益，提高经济效益，保证项目质量，制定本法。"
              />

              <!-- 三态核验结果展示卡片 -->
              <div
                v-if="verifyResult"
                class="verify-result-card"
                :class="verifyResult.status"
              >
                <div class="vr-top">
                  <!-- 状态 1：exact_match 绿勾 -->
                  <span v-if="verifyResult.status === 'exact_match'" class="vr-badge exact">
                    ✓ 库内原文（{{ verifyResult.law }}{{ verifyResult.article_no }}）
                  </span>
                  <!-- 状态 2：fuzzy_hint 灰标 -->
                  <span v-else-if="verifyResult.status === 'fuzzy_hint'" class="vr-badge fuzzy">
                    疑似系{{ verifyResult.law }}{{ verifyResult.article_no }}改写，未经核验
                  </span>
                  <!-- 状态 3：unverifiable 红标 -->
                  <span v-else class="vr-badge unverified">
                    ✕ 库中未收录，真伪未经核验
                  </span>

                  <span class="vr-sim mono-num">
                    匹配度：{{ (verifyResult.similarity * 100).toFixed(1) }}%
                  </span>
                </div>

                <div class="vr-msg">{{ verifyResult.message }}</div>

                <div v-if="verifyResult.official_content" class="vr-official">
                  <b>库内权威原文（{{ verifyResult.law }} · {{ verifyResult.article_no }}）：</b>
                  <span>{{ verifyResult.official_content }}</span>
                </div>
              </div>
            </div>

            <!-- 2. 顶部管理员操作条：检查官网更新 + 手动上传 json/txt/docx/pdf 四格式法规 -->
            <div class="rp-top-bar" style="margin-top: 18px">
              <div>
                <h3>招投标权威法律法规底表（已收录 {{ laws.length }} 部法规）</h3>
                <p>点击左侧法规卡片可浏览全部条文并按关键词检索；点击任意条文可一键填入上方核验台测试。</p>
              </div>

              <div v-if="authStore.isAdmin" class="admin-reg-actions">
                <el-input
                  v-model.trim="importLawNameHint"
                  size="small"
                  placeholder="导入法规名称（可选）"
                  style="width: 180px"
                />
                <label class="upload-law-btn">
                  <span>{{ importingFile ? '导入解析中…' : '+ 上传法规 (json/txt/docx/pdf)' }}</span>
                  <input
                    type="file"
                    accept=".json,.txt,.docx,.pdf"
                    style="display: none"
                    :disabled="importingFile"
                    @change="handleImportLawFile"
                  />
                </label>
                <el-button
                  size="small"
                  type="primary"
                  plain
                  :loading="crawling"
                  @click="handleCrawlUpdates"
                >
                  检查官网更新
                </el-button>
              </div>
            </div>

            <!-- 官网更新检查结果面板 -->
            <div v-if="crawlReport" class="crawl-report-box">
              <div class="crb-title">
                官网更新检查报告：共检查 {{ crawlReport.total_sources }} 个配置源，
                更新 {{ crawlReport.updated_laws_count }} 部，待配置占位源 {{ crawlReport.unconfigured_count }} 个
              </div>
              <div class="crb-grid">
                <div v-for="item in crawlReport.results" :key="item.name" class="crb-item">
                  <span
                    class="sev-pill"
                    :class="
                      item.status === 'updated' || item.status === 'unchanged'
                        ? 'ok'
                        : item.status === 'unconfigured'
                          ? 'info'
                          : 'warning'
                    "
                  >
                    {{ item.status }}
                  </span>
                  <b>{{ item.name }}</b>
                  <span class="crb-msg">{{ item.message }}</span>
                </div>
              </div>
            </div>

            <!-- 3. 左法规列表卡 + 右条文浏览与关键词检索分栏 -->
            <div v-loading="loadingLaws" class="reg-split-layout">
              <!-- 左栏：法规列表卡片 -->
              <div class="law-cards-col">
                <div
                  v-for="law in laws"
                  :key="law.id"
                  class="law-card-item"
                  :class="{ active: law.id === selectedLawId }"
                  @click="handleSelectLaw(law)"
                >
                  <div class="lc-row1">
                    <span class="lc-name">{{ law.name }}</span>
                    <span class="sev-pill info">{{ law.article_count }} 条</span>
                  </div>
                  <div class="lc-meta">
                    <div>发文机关：{{ law.issuing_body || '国家立法机关' }}</div>
                    <div>
                      来源：<code>{{ law.source_url || '内置种子' }}</code>
                    </div>
                    <div v-if="law.updated_at">
                      更新：{{ law.updated_at.slice(0, 19).replace('T', ' ') }}
                    </div>
                  </div>
                </div>

                <el-empty v-if="laws.length === 0" description="法规库为空" :image-size="60" />
              </div>

              <!-- 右栏：选中法规的条文浏览与关键词检索 -->
              <div v-loading="loadingArticles" class="law-articles-col">
                <div class="la-toolbar">
                  <div class="la-current-title">
                    <b>{{ selectedLaw?.name || '请选择左侧法规' }}</b>
                    <span class="mono-num">（共 {{ totalArticles }} 条匹配）</span>
                  </div>

                  <div class="la-search">
                    <el-input
                      v-model="articleKeyword"
                      size="small"
                      clearable
                      placeholder="按条号（如 第三十三条）或正文关键词检索…"
                      style="width: 260px"
                      @keyup.enter="handleSearchArticles"
                      @clear="handleSearchArticles"
                    />
                    <el-button size="small" type="primary" @click="handleSearchArticles">
                      检索条文
                    </el-button>
                  </div>
                </div>

                <div class="articles-scroll-list">
                  <div
                    v-for="art in articles"
                    :key="art.id"
                    class="art-item"
                  >
                    <div class="art-head">
                      <span class="art-no">{{ art.article_no }}</span>
                      <span class="art-hash mono-num">SHA256: {{ art.content_hash.slice(0, 10) }}</span>
                      <el-button
                        size="small"
                        link
                        type="primary"
                        style="margin-left: auto"
                        @click="fillSampleForVerify(art)"
                      >
                        送入核验台测试 ↑
                      </el-button>
                    </div>
                    <div class="art-content">{{ art.content }}</div>
                  </div>

                  <el-empty
                    v-if="articles.length === 0"
                    description="未检索到匹配的法条记录"
                    :image-size="64"
                  />
                </div>

                <div v-if="totalArticles > articlePageSize" class="art-pager">
                  <el-pagination
                    small
                    background
                    layout="total, prev, pager, next"
                    :total="totalArticles"
                    :page-size="articlePageSize"
                    :current-page="articlePage"
                    @current-change="handleArticlePageChange"
                  />
                </div>
              </div>
            </div>
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

:deep(.cert-expiring-row) {
  background-color: #fff7ed !important;
}

:deep(.cert-expired-row) {
  background-color: #fef2f2 !important;
}

/* ── 页签 ③：法规库与三态核验样式 ── */
.verify-box {
  padding: 16px 18px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-line);
  border-radius: 10px;
}

.vb-hd {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 10px;
}

.vb-hd h4 {
  font-size: 14.5px;
  font-weight: 900;
  color: var(--txt-strong);
}

.vb-hd p {
  font-size: 12px;
  color: var(--txt-sec);
  margin-top: 2px;
}

.verify-result-card {
  margin-top: 12px;
  padding: 12px 15px;
  border-radius: 8px;
  border: 1px solid var(--border-line);
  border-left: 4px solid var(--txt-muted);
  background: #ffffff;
}

.verify-result-card.exact_match {
  border-left-color: var(--sev-ok);
  background: #f0fdf4;
}

.verify-result-card.fuzzy_hint {
  border-left-color: #64748b;
  background: #f8fafc;
}

.verify-result-card.unverifiable {
  border-left-color: var(--sev-error);
  background: #fef2f2;
}

.vr-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.vr-badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 800;
}

.vr-badge.exact {
  background: var(--sev-ok);
  color: #ffffff;
}

.vr-badge.fuzzy {
  background: #cbd5e1;
  color: #1e293b;
}

.vr-badge.unverified {
  background: var(--sev-error);
  color: #ffffff;
}

.vr-sim {
  font-size: 12px;
  font-weight: 800;
  color: var(--txt-sec);
}

.vr-msg {
  font-size: 13px;
  font-weight: 700;
  color: var(--txt-strong);
  margin-bottom: 6px;
}

.vr-official {
  font-size: 12.5px;
  color: var(--txt-body);
  background: rgba(255, 255, 255, 0.75);
  padding: 8px 10px;
  border-radius: 6px;
  border: 1px dashed var(--border-strong);
  line-height: 1.6;
}

.admin-reg-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.upload-law-btn {
  display: inline-flex;
  align-items: center;
  height: 28px;
  padding: 0 11px;
  border-radius: 4px;
  border: 1px solid var(--brand);
  background: var(--brand-soft);
  color: var(--brand);
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}

.crawl-report-box {
  margin-bottom: 16px;
  padding: 12px 16px;
  background: #eff6ff;
  border: 1px solid #bfdbfe;
  border-radius: 8px;
}

.crb-title {
  font-size: 13px;
  font-weight: 800;
  color: #1e3a8a;
  margin-bottom: 8px;
}

.crb-grid {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.crb-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}

.crb-msg {
  color: var(--txt-sec);
}

.reg-split-layout {
  display: grid;
  grid-template-columns: 330px 1fr;
  gap: 16px;
  align-items: start;
}

.law-cards-col {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.law-card-item {
  padding: 12px 14px;
  border-radius: 8px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
  cursor: pointer;
  transition: all 0.15s;
}

.law-card-item:hover {
  border-color: var(--brand);
  background: #ffffff;
}

.law-card-item.active {
  border-color: var(--brand);
  background: var(--brand-soft);
  box-shadow: 0 0 0 2px rgba(29, 78, 216, 0.12);
}

.lc-row1 {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 6px;
}

.lc-name {
  font-size: 13.5px;
  font-weight: 900;
  color: var(--txt-strong);
}

.lc-meta {
  font-size: 11.5px;
  color: var(--txt-sec);
  line-height: 1.55;
  /* 长来源 URL 断行防溢出（生产实锤：gov.cn 长 URL 超出卡片边框不可读） */
  word-break: break-all;
  overflow-wrap: anywhere;
  min-width: 0;
}

.lc-meta code {
  font-size: 11px;
  word-break: break-all;
  overflow-wrap: anywhere;
}

.law-articles-col {
  border: 1px solid var(--border-line);
  border-radius: 8px;
  padding: 14px 16px;
  background: #ffffff;
}

.la-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-line);
  margin-bottom: 12px;
}

.la-search {
  display: flex;
  gap: 8px;
  align-items: center;
}

.articles-scroll-list {
  max-height: 520px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.art-item {
  padding: 10px 12px;
  border-radius: 6px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
}

.art-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 4px;
}

.art-no {
  font-size: 13px;
  font-weight: 900;
  color: var(--brand);
}

.art-hash {
  font-size: 11px;
  color: var(--txt-muted);
}

.art-content {
  font-size: 13px;
  color: var(--txt-body);
  line-height: 1.65;
}

.art-pager {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}
</style>