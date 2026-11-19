<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { modelConfigApi } from '@/api'

const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const deleting = ref(false)

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
    if (res.ok) {
      const suffix = res.embedding_ok === true && res.embedding_message ? `；${res.embedding_message}` : ''
      ElMessage.success(`${res.message || `配置校验通过（${res.latency_ms ?? 0}ms）`}${suffix}`)
      if (res.embedding_ok === false && res.embedding_message) {
        ElMessage.warning(res.embedding_message)
      }
    } else {
      ElMessage.warning(res.message || `模型连通性异常（${res.latency_ms ?? 0}ms）`)
      if (res.embedding_ok === false && res.embedding_message) {
        ElMessage.warning(res.embedding_message)
      }
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
  </div>
</template>

<style scoped>
.model-settings-page {
  max-width: 820px;
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
</style>
