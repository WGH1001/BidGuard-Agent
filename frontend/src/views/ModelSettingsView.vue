<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { modelConfigApi } from '@/api'

const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const deleting = ref(false)
const testResult = ref<{
  ok: boolean
  latencyMs: number
  chatMessage: string
  embeddingOk: boolean | null
  embeddingMessage: string
} | null>(null)

const meta = reactive({
  configured: false,
  api_key_last4: '',
  api_key_masked: '',
  updated_at: '' as string | null | undefined,
})

const form = reactive({
  base_url: 'http://10.0.2.81:3000/v1',
  api_key: '',
  model_flash: 'GLM-5.3-Flash',
  model_flagship: 'GLM-5.3-Flash',
  model_embedding: '',
  supports_reasoning_effort: true,
  supports_vision: false,
})

interface EndpointPreset {
  /** 模板标识：仅前端用于选中高亮与回显匹配，不提交后端 */
  key: string
  label: string
  /** 服务商显示名：仅前端展示（后端契约无此字段），按 base_url 匹配回显 */
  provider_name: string
  base_url: string
  model_flash: string
  model_flagship: string
  model_embedding: string
  supports_reasoning_effort: boolean
  supports_vision?: boolean
}

const PRESETS: EndpointPreset[] = [
  {
    key: 'custom-intranet',
    label: '自定义 / 企业内网（vLLM · Ollama · MaaS，推荐 · 标书数据不出域）',
    provider_name: '企业内网 / 自定义',
    base_url: 'http://10.0.2.81:3000/v1',
    model_flash: 'GLM-5.3-Flash',
    model_flagship: 'GLM-5.3-Flash',
    model_embedding: '',
    supports_reasoning_effort: true,
  },
  {
    key: 'zhipu',
    label: '智谱开放平台 (BigModel OpenAI 兼容端点)',
    provider_name: '智谱开放平台',
    base_url: 'https://open.bigmodel.cn/api/paas/v4',
    model_flash: 'glm-5.3-flash',
    model_flagship: 'glm-5.3-flash',
    model_embedding: 'embedding-3',
    supports_reasoning_effort: true,
  },
  {
    key: 'deepseek',
    label: 'DeepSeek 官方开放平台',
    provider_name: 'DeepSeek',
    base_url: 'https://api.deepseek.com',
    model_flash: 'deepseek-chat',
    model_flagship: 'deepseek-reasoner',
    model_embedding: '',
    supports_reasoning_effort: false,
  },
  {
    key: 'kimi',
    label: 'Kimi (月之暗面 Moonshot)',
    provider_name: 'Kimi (月之暗面)',
    base_url: 'https://api.moonshot.cn/v1',
    model_flash: 'kimi-k3.1-pro-highspeed',
    model_flagship: 'kimi-k3.1-pro',
    model_embedding: '',
    supports_reasoning_effort: false,
  },
  {
    key: 'qwen',
    label: '通义千问 (阿里云百炼 DashScope)',
    provider_name: '通义千问 (阿里云百炼)',
    base_url: 'https://maas.qianwenaiapi.com/compatible-mode/v1',
    model_flash: 'qwen3.8-flash',
    model_flagship: 'qwen3.8-max',
    model_embedding: '',
    supports_reasoning_effort: false,
  },
  {
    key: 'siliconflow',
    label: 'SiliconFlow (硅基流动)',
    provider_name: 'SiliconFlow (硅基流动)',
    base_url: 'https://api.siliconflow.cn/v1',
    model_flash: 'deepseek-ai/DeepSeek-V4-Flash',
    model_flagship: 'deepseek-ai/DeepSeek-V4-Pro',
    model_embedding: 'BAAI/bge-m3',
    supports_reasoning_effort: false,
  },
]

const selectedKey = ref<string>('custom')

/** 按 base_url 反查当前服务商名：匹配到模板用其 provider_name，否则视为自定义 */
const currentProviderName = computed<string>(() => {
  const matched = PRESETS.find((p) => p.base_url === form.base_url.trim())
  return matched ? matched.provider_name : '自定义 / 手动填写'
})

function applyPreset(p: EndpointPreset): void {
  selectedKey.value = p.key
  form.base_url = p.base_url
  form.model_flash = p.model_flash
  form.model_flagship = p.model_flagship
  form.model_embedding = p.model_embedding || ''
  form.supports_reasoning_effort = p.supports_reasoning_effort
  form.supports_vision = Boolean(p.supports_vision)
}

async function fetchConfig(): Promise<void> {
  loading.value = true
  form.api_key = ''
  try {
    const res = await modelConfigApi.get()
    meta.configured = res.configured
    meta.api_key_last4 = res.api_key_last4 || ''
    meta.api_key_masked = res.api_key_masked || ''
    meta.updated_at = res.updated_at
    if (res.configured) {
      form.base_url = res.base_url
      form.model_flash = res.model_flash
      form.model_flagship = res.model_flagship || res.model_flash
      form.model_embedding = (res.model_embedding || '').trim()
      form.supports_reasoning_effort = res.supports_reasoning_effort
      form.supports_vision = Boolean(res.supports_vision)
      const matched = PRESETS.find((p) => p.base_url === res.base_url)
      selectedKey.value = matched ? matched.key : 'custom'
    }
  } finally {
    loading.value = false
  }
}

async function handleSave(): Promise<void> {
  if (!form.base_url.trim() || !form.model_flash.trim()) {
    ElMessage.warning('请填写模型服务地址 (base_url) 与快速模型名称 (model_flash)')
    return
  }
  if (!form.api_key.trim() && !meta.configured) {
    ElMessage.warning('首次绑定请填写有效的 API Key')
    return
  }
  saving.value = true
  try {
    const res = await modelConfigApi.save({
      base_url: form.base_url.trim(),
      api_key: form.api_key.trim() || undefined,
      model_flash: form.model_flash.trim(),
      model_flagship: form.model_flagship.trim() || form.model_flash.trim(),
      model_embedding: form.model_embedding.trim(),
      supports_reasoning_effort: form.supports_reasoning_effort,
      supports_vision: Boolean(form.supports_vision),
    })
    meta.configured = res.configured
    meta.api_key_last4 = res.api_key_last4
    meta.api_key_masked = res.api_key_masked
    meta.updated_at = res.updated_at
    form.model_embedding = (res.model_embedding || '').trim()
    form.supports_vision = Boolean(res.supports_vision)
    form.api_key = ''
    ElMessage.success(`模型配置已加密保存（当前密钥尾号：${res.api_key_last4}）`)
  } finally {
    saving.value = false
  }
}

async function handleTest(): Promise<void> {
  if (!form.base_url.trim() || !form.model_flash.trim()) {
    ElMessage.warning('请先填写接口地址与模型名称')
    return
  }
  testing.value = true
  testResult.value = null
  try {
    const res = await modelConfigApi.test({
      base_url: form.base_url.trim(),
      api_key: form.api_key.trim() || undefined,
      model_flash: form.model_flash.trim(),
      model_flagship: form.model_flagship.trim() || form.model_flash.trim(),
      model_embedding: form.model_embedding.trim() || undefined,
      supports_reasoning_effort: form.supports_reasoning_effort,
      supports_vision: Boolean(form.supports_vision),
    })
    const latency = res.latency_ms ?? 0
    const embOk = typeof res.embedding_ok === 'boolean' ? res.embedding_ok : null
    const embMsg = (res.embedding_message || '').trim()
    testResult.value = {
      ok: Boolean(res.ok),
      latencyMs: latency,
      chatMessage: (res.message || (res.ok ? `鉴权与对话端点校验通过（${latency}ms）` : `模型连通性异常（${latency}ms）`)).trim(),
      embeddingOk: embOk,
      embeddingMessage: embMsg,
    }
    if (res.ok && embOk === false) {
      ElMessage.warning(`对话端点通过（${latency}ms），向量模型不可用，详见下方回执`)
    } else if (res.ok) {
      ElMessage.success(`连通性测试通过（${latency}ms）`)
    } else {
      ElMessage.warning(`模型连通性异常（${latency}ms），详见下方回执`)
    }
  } finally {
    testing.value = false
  }
}

async function handleUnbind(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '确认解绑并清空当前账号保存的个人大模型服务配置吗？解绑后将自动回退使用系统默认网关。',
      '确认解绑模型配置',
      {
        type: 'warning',
        confirmButtonText: '确认解绑',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  deleting.value = true
  try {
    await modelConfigApi.remove()
    meta.configured = false
    meta.api_key_last4 = ''
    meta.api_key_masked = ''
    form.api_key = ''
    testResult.value = null
    ElMessage.success('已解绑个人模型配置，恢复使用系统默认网关')
  } finally {
    deleting.value = false
  }
}

onMounted(() => {
  void fetchConfig()
})
</script>

<template>
  <div v-loading="loading" class="model-settings-page">
    <div class="bg-panel ms-card">
      <div class="ms-header">
        <div>
          <h2>私有化与个人大模型服务配置</h2>
          <p>
            标书涉及商业报价与核心技术方案机密。支持接入企业内网私有化模型端点（vLLM / Ollama /
            MaaS），API Key 经服务端派生密钥加密落库，永不回显明文。
          </p>
        </div>
        <span class="sev-pill" :class="meta.configured ? 'ok' : 'info'">
          {{ meta.configured ? `已启用自定义模型（尾号 ${meta.api_key_last4}）` : '当前使用系统默认网关' }}
        </span>
      </div>

      <div class="preset-note">
        任何提供 OpenAI 兼容接口的厂商（通义千问 / Kimi / OpenRouter / 本地 Ollama 等）均可直接手动填写
        Base URL 与模型名；配置未生效或调用失败时，系统自动回退系统默认网关（右上角徽标实时显示当前生效层）。
      </div>
      <div class="preset-bar">
        <span class="preset-lbl">快捷填充模板：</span>
        <el-button
          v-for="p in PRESETS"
          :key="p.key"
          size="small"
          :type="selectedKey === p.key ? 'primary' : ''"
          :plain="selectedKey !== p.key"
          @click="applyPreset(p)"
        >
          {{ p.label }}
        </el-button>
        <span class="provider-tag">当前服务商：{{ currentProviderName }}</span>
      </div>

      <el-form label-position="top" class="ms-form">
        <el-form-item label="OpenAI 兼容接口地址 (Base URL)" required>
          <el-input
            v-model.trim="form.base_url"
            autocomplete="off"
            placeholder="例如：http://10.0.2.81:3000/v1 或 https://open.bigmodel.cn/api/paas/v4"
          />
        </el-form-item>

        <el-form-item label="API Key 密钥（服务端加密存储，响应仅回显尾 4 位）" :required="!meta.configured">
          <el-input
            v-model.trim="form.api_key"
            type="password"
            show-password
            autocomplete="new-password"
            :placeholder="
              meta.configured
                ? `已配置密钥（****${meta.api_key_last4}），如不修改请留空`
                : '请输入 API Key'
            "
          />
        </el-form-item>

        <div class="two-col">
          <el-form-item label="快速核验模型" required>
            <el-input v-model.trim="form.model_flash" placeholder="例如：GLM-5.3-Flash" />
          </el-form-item>

          <el-form-item label="专家追问模型">
            <el-input v-model.trim="form.model_flagship" placeholder="留空则默认与快速模型一致" />
          </el-form-item>
        </div>

        <el-form-item label="向量精排模型（选填，用于检索与评分预估混合精排）">
          <el-input
            v-model.trim="form.model_embedding"
            clearable
            placeholder="留空自动使用 0-Token BM25；同网关支持 /v1/embeddings 时可填如 BAAI/bge-m3 或 embedding-3"
          />
          <div class="field-sub-tip">
            共用上方同一组 Base URL 与 API Key（受内网锁与网关出口脱敏统一保护）；留空或上游不支持时自动静默降级为 BM25 检索，绝不中断任务。
          </div>
        </el-form-item>

        <el-form-item label="推理预算参数支持">
          <el-switch v-model="form.supports_reasoning_effort" />
          <span class="switch-tip">开启后向网关透传轻量推理参数以加速结构化响应</span>
        </el-form-item>

        <el-form-item label="视觉多模态能力支持（支持问答原生识图）">
          <el-switch v-model="form.supports_vision" />
          <span class="switch-tip">
            仅当所填模型为多模态视觉模型（如 GLM-4V / Qwen-VL / GPT-4o）时开启；未开启或上游不支持时自动降级为离线文字识别模式
          </span>
        </el-form-item>

        <div
          v-if="testResult"
          class="test-result-strip"
          :class="testResult.ok && testResult.embeddingOk !== false ? 'is-ok' : 'is-warn'"
        >
          <div class="trs-head">
            <div class="trs-title-group">
              <strong>连通性检测回执</strong>
              <span class="trs-latency mono-num">{{ testResult.latencyMs }}ms</span>
            </div>
            <button
              type="button"
              class="trs-close"
              title="关闭回执"
              @click="testResult = null"
            >
              ×
            </button>
          </div>
          <div class="trs-row">
            <span class="sev-pill" :class="testResult.ok ? 'ok' : 'warning'">
              {{ testResult.ok ? '对话端点通过' : '对话端点异常' }}
            </span>
            <span class="trs-msg">{{ testResult.chatMessage }}</span>
          </div>
          <div v-if="testResult.embeddingMessage" class="trs-row">
            <span class="sev-pill" :class="testResult.embeddingOk ? 'ok' : 'warning'">
              {{ testResult.embeddingOk ? '向量精排就绪' : '向量自动降级' }}
            </span>
            <span class="trs-msg">{{ testResult.embeddingMessage }}</span>
          </div>
        </div>

        <div class="ms-actions">
          <el-button :loading="testing" @click="handleTest">测试连通性</el-button>
          <el-button type="primary" :loading="saving" @click="handleSave">保存并启用配置</el-button>
          <el-button
            v-if="meta.configured"
            type="danger"
            plain
            :loading="deleting"
            @click="handleUnbind"
          >
            解绑并回退默认网关
          </el-button>
        </div>
      </el-form>
    </div>

    <aside class="bg-panel ms-aside">
      <div class="msa-head">
        <h3>接入与安全说明</h3>
        <span class="sev-pill" :class="meta.configured ? 'ok' : 'info'">
          {{ meta.configured ? '自定义生效中' : '默认网关' }}
        </span>
      </div>

      <div class="msa-list">
        <div class="msa-item">
          <div class="msa-item-title">
            <span class="msa-idx">01</span>
            <span>API Key 加密落库与原件保全</span>
          </div>
          <p>
            密钥经服务端派生密钥加密存储，任何接口仅回显尾 4 位且绝不下发明文；上传的招投标原件逐字节保留，不做落地改写。
          </p>
        </div>

        <div class="msa-item">
          <div class="msa-item-title">
            <span class="msa-idx">02</span>
            <span>网关三出口统一脱敏与内网锁</span>
          </div>
          <p>
            对话、流式输出与向量请求均在模型网关出口统一执行确定性隐私脱敏（身份证号、语境银行账号及可选联系方式）；启用内网锁后仅放行私网与白名单端点，自动拦截公网地址。
          </p>
        </div>

        <div class="msa-item">
          <div class="msa-item-title">
            <span class="msa-idx">03</span>
            <span>向量 / 多模态 / 推理自动降级</span>
          </div>
          <p>
            向量精排模型留空或上游不支持时静默回退 0-Token BM25；视觉识图或推理预算参数遇上游拒参时自动单实例熔断并剥参重试，不中断业务流。
          </p>
        </div>

        <div class="msa-item">
          <div class="msa-item-title">
            <span class="msa-idx">04</span>
            <span>失效回退与连通性预检</span>
          </div>
          <p>
            未配置或解绑个人密钥后自动回退系统默认网关；修改地址或模型名后，建议先点击「测试连通性」同时核验对话与向量接口。
          </p>
        </div>
      </div>

      <div class="msa-status-box">
        <div class="msa-status-hd">当前表单配置摘要</div>
        <div class="msa-kv">
          <span>服务商</span>
          <strong>{{ currentProviderName }}</strong>
        </div>
        <div class="msa-kv">
          <span>快速 / 专家模型</span>
          <strong class="mono-num">{{ form.model_flash || '未填写' }} / {{ form.model_flagship || form.model_flash || '同上' }}</strong>
        </div>
        <div class="msa-kv">
          <span>向量精排</span>
          <strong class="mono-num">{{ form.model_embedding || 'BM25 自动降级' }}</strong>
        </div>
        <div class="msa-kv">
          <span>扩展能力</span>
          <strong>{{ form.supports_reasoning_effort ? '推理预算开' : '推理预算关' }} · {{ form.supports_vision ? '原生识图开' : '离线 OCR' }}</strong>
        </div>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.model-settings-page {
  --el-color-primary: var(--brand, #1d4ed8);
  --el-color-primary-light-3: #3b82f6;
  --el-color-primary-light-5: #93c5fd;
  --el-color-primary-light-7: #bfdbfe;
  --el-color-primary-light-8: #dbeafe;
  --el-color-primary-light-9: #eff6ff;
  --el-color-primary-dark-2: var(--brand-hover, #1e40af);
  display: grid;
  grid-template-columns: minmax(0, 1fr) 360px;
  gap: 16px;
  align-items: start;
  max-width: 1260px;
}

@media (max-width: 1120px) {
  .model-settings-page {
    grid-template-columns: minmax(0, 1fr);
  }
}

.ms-card {
  padding: 26px 30px;
}

.ms-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  padding-bottom: 18px;
  border-bottom: 1px solid var(--border-line);
  margin-bottom: 18px;
}

.ms-header h2 {
  font-size: 18px;
  font-weight: 900;
  color: var(--txt-strong);
}

.ms-header p {
  font-size: 12.5px;
  color: var(--txt-sec);
  margin-top: 4px;
  line-height: 1.6;
}

.preset-note {
  font-size: 12px;
  color: var(--color-text-tertiary, #888780);
  line-height: 1.6;
  margin-bottom: 8px;
}
.preset-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 20px;
  padding: 10px 14px;
  background: var(--bg-subtle);
  border-radius: 8px;
  border: 1px solid var(--border-line);
}

.preset-lbl {
  font-size: 12px;
  font-weight: 700;
  color: var(--txt-sec);
}

.provider-tag {
  margin-left: auto;
  font-size: 12px;
  font-weight: 700;
  color: var(--txt-sec);
  white-space: nowrap;
}

.two-col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.switch-tip {
  margin-left: 10px;
  font-size: 12px;
  color: var(--txt-muted);
}

.field-sub-tip {
  margin-top: 5px;
  font-size: 12px;
  color: var(--txt-muted);
  line-height: 1.5;
}

.ms-actions {
  display: flex;
  gap: 10px;
  margin-top: 12px;
  padding-top: 16px;
  border-top: 1px solid var(--border-line);
}

.preset-bar :deep(.el-button--primary) {
  background-color: var(--brand, #1d4ed8);
  border-color: var(--brand, #1d4ed8);
}

.preset-bar :deep(.el-button--primary:hover) {
  background-color: var(--brand-hover, #1e40af);
  border-color: var(--brand-hover, #1e40af);
}

.ms-aside {
  padding: 20px 22px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.msa-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-line);
}

.msa-head h3 {
  margin: 0;
  font-size: 14.5px;
  font-weight: 800;
  color: var(--txt-strong);
}

.msa-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.msa-item {
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--bg-subtle);
  border: 1px solid #e6ebf2;
}

.msa-item-title {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12.5px;
  font-weight: 700;
  color: var(--txt-strong);
  margin-bottom: 4px;
}

.msa-idx {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--brand-soft, #eff6ff);
  border: 1px solid #dbeafe;
  color: var(--brand, #1d4ed8);
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 10.5px;
  font-weight: 700;
  flex-shrink: 0;
}

.msa-item p {
  margin: 0;
  font-size: 12px;
  color: var(--txt-sec);
  line-height: 1.55;
}

.msa-status-box {
  padding: 12px 13px;
  border-radius: 8px;
  border: 1px solid var(--border-line);
  background: #ffffff;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.msa-status-hd {
  font-size: 12px;
  font-weight: 700;
  color: var(--txt-strong);
  margin-bottom: 2px;
}

.msa-kv {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 10px;
  font-size: 12px;
  color: var(--txt-muted);
}

.msa-kv strong {
  color: var(--txt-body);
  font-weight: 600;
  text-align: right;
  word-break: break-all;
}

.test-result-strip {
  margin-top: 8px;
  padding: 11px 14px;
  border-radius: 8px;
  border: 1px solid var(--border-line);
  border-left-width: 3px;
  background: #ffffff;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.test-result-strip.is-ok {
  border-left-color: var(--sev-ok, #059669);
  background: #f8fffb;
}

.test-result-strip.is-warn {
  border-left-color: var(--sev-warning, #ea580c);
  background: #fffcf8;
}

.trs-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.trs-title-group {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--txt-strong);
}

.trs-latency {
  padding: 1px 6px;
  border-radius: 4px;
  background: #f1f5f9;
  color: var(--txt-sec);
  font-size: 11px;
  font-weight: 700;
}

.trs-close {
  border: none;
  background: transparent;
  color: var(--txt-muted);
  font-size: 16px;
  line-height: 1;
  padding: 0 4px;
  cursor: pointer;
}

.trs-close:hover {
  color: var(--txt-strong);
}

.trs-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 12px;
  line-height: 1.55;
  color: var(--txt-body);
}

.trs-msg {
  flex: 1;
  min-width: 0;
  word-break: break-word;
}
</style>
