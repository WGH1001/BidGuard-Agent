<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

const title = computed(() => (route.meta.title as string) || '功能模块')
const moduleCode = computed(() => (route.meta.moduleCode as string) || 'MODULE')
const desc = computed(
  () => (route.meta.desc as string) || '该业务工作台视图将在后续 C2 / C3 任务卡中完整接入。',
)
</script>

<template>
  <div class="bg-panel placeholder-box">
    <span class="ph-badge mono-num">{{ moduleCode }} · 业务视图就绪位</span>
    <h2 class="ph-title">{{ title }}（建设中）</h2>
    <p class="ph-desc">{{ desc }}</p>
    <div class="ph-actions">
      <el-button type="primary" @click="router.push('/dashboard')">返回防废标总览大盘</el-button>
      <el-button @click="router.push('/model-settings')">配置私有化模型</el-button>
    </div>
  </div>
</template>

<style scoped>
.placeholder-box {
  padding: 56px 32px;
  text-align: center;
  max-width: 680px;
  margin: 36px auto;
}

.ph-badge {
  display: inline-block;
  padding: 4px 12px;
  border-radius: 4px;
  background: var(--brand-soft);
  color: var(--brand);
  font-size: 12px;
  font-weight: 800;
  margin-bottom: 14px;
}

.ph-title {
  font-size: 20px;
  font-weight: 900;
  color: var(--txt-strong);
  margin-bottom: 8px;
}

.ph-desc {
  font-size: 13.5px;
  color: var(--txt-sec);
  margin-bottom: 24px;
}

.ph-actions {
  display: flex;
  justify-content: center;
  gap: 12px;
}
</style>