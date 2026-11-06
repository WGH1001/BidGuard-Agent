<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus/es/components/message/index'
import { authApi } from '@/api'

defineOptions({ name: 'ChangePasswordView' })

const router = useRouter()
const submitting = ref(false)

const form = reactive({
  old_password: '',
  new_password: '',
  confirm_password: '',
})

async function handleSubmit(): Promise<void> {
  if (!form.old_password.trim()) {
    ElMessage.warning('请输入当前原密码')
    return
  }
  if (form.new_password.trim().length < 6) {
    ElMessage.warning('新密码长度不能少于 6 位')
    return
  }
  if (form.new_password !== form.confirm_password) {
    ElMessage.warning('两次输入的新密码不一致')
    return
  }

  submitting.value = true
  try {
    await authApi.changePassword({
      old_password: form.old_password,
      new_password: form.new_password.trim(),
    })
    ElMessage.success('密码修改成功')
    form.old_password = ''
    form.new_password = ''
    form.confirm_password = ''
    void router.push('/dashboard')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="pwd-page">
    <div class="bg-panel pwd-card">
      <h2>修改登录密码</h2>
      <p class="pwd-sub">定期更新投标部账户密码以保障商业标书与报价数据安全。</p>

      <el-form label-position="top" @submit.prevent="handleSubmit">
        <el-form-item label="当前原密码" required>
          <el-input
            v-model="form.old_password"
            type="password"
            show-password
            placeholder="请输入当前登录密码"
          />
        </el-form-item>

        <el-form-item label="新密码（至少 6 位字符）" required>
          <el-input
            v-model="form.new_password"
            type="password"
            show-password
            placeholder="请输入新密码"
          />
        </el-form-item>

        <el-form-item label="确认新密码" required>
          <el-input
            v-model="form.confirm_password"
            type="password"
            show-password
            placeholder="请再次输入新密码"
          />
        </el-form-item>

        <div class="pwd-actions">
          <el-button type="primary" :loading="submitting" @click="handleSubmit">
            确认修改密码
          </el-button>
          <el-button @click="router.push('/dashboard')">返回总览</el-button>
        </div>
      </el-form>
    </div>
  </div>
</template>

<style scoped>
.pwd-page {
  max-width: 520px;
}

.pwd-card {
  padding: 26px 30px;
}

.pwd-card h2 {
  font-size: 18px;
  font-weight: 900;
  color: var(--txt-strong);
}

.pwd-sub {
  font-size: 12.5px;
  color: var(--txt-muted);
  margin: 4px 0 20px;
}

.pwd-actions {
  display: flex;
  gap: 10px;
  margin-top: 10px;
}
</style>