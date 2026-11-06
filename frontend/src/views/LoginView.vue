<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus/es/components/message/index'
import { useAuthStore } from '@/stores/auth'

defineOptions({ name: 'LoginView' })

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()

const loading = ref(false)
const form = reactive({
  username: '',
  password: '',
})

interface DemoAccountPreset {
  roleLabel: string
  username: string
  password: string
  dept: string
  desc: string
}

const DEMO_PRESETS: DemoAccountPreset[] = [
  {
    roleLabel: '投标商务经理（主用户）',
    username: 'liming',
    password: 'BidGuard2026!',
    dept: '政企系统集成投标部',
    desc: '双书防废标体检 · 偏离表一键生成 · 投前控标扫描',
  },
  {
    roleLabel: '系统管理员（全权限）',
    username: 'admin',
    password: 'BidGuard2026!',
    dept: '政企系统集成投标部',
    desc: '包含废标规则底表配置、证照台账与安全审计日志查询',
  },
]

function fillDemoAccount(preset: DemoAccountPreset): void {
  form.username = preset.username
  form.password = preset.password
}

async function handleLogin(): Promise<void> {
  if (!form.username.trim() || !form.password.trim()) {
    ElMessage.warning('请输入账号与登录密码')
    return
  }
  loading.value = true
  try {
    const user = await authStore.login({
      username: form.username.trim(),
      password: form.password,
    })
    ElMessage.success(`欢迎进入封标防废标工作台，${user.display_name || user.username}`)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/dashboard'
    await router.replace(redirect)
  } catch {
    // 错误已由 http 拦截器统一提示
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="bg-login-screen">
    <!-- 左翼：工业蓝图防废标六道安全锁展示舱 -->
    <section class="login-blueprint-pane">
      <div class="bp-top-tag">BIDGUARD-AGENT · 卖方投标防废标专版</div>
      <h1 class="bp-headline">封标前最后一道安全锁<br />与投前控标雷达系统</h1>
      <p class="bp-lead">
        专为系统集成商、弱电工程商与装备制造投标团队打造。先结构化抽取招标红线再逐条勾稽核销，
        将 2 天人工制作《技术与商务偏离表》压缩至 15 分钟。
      </p>

      <div class="bp-matrix-grid">
        <div class="bp-cell">
          <span class="bp-code">F1 零漏检提取</span>
          <p>拆解资格项、符合项、★/▲强制参数与评分细则表，攻克 200 页长文档注意力衰减</p>
        </div>
        <div class="bp-cell">
          <span class="bp-code">F2 报价算术沙盒</span>
          <p>0-Token Decimal 精算单价×数量、分项累加、人民币大小写与最高限价，差 1 分必报</p>
        </div>
        <div class="bp-cell">
          <span class="bp-code">F3 证照与串名扫描</span>
          <p>核验营业执照/ISO有效期、社保证明所属月份区间，全文排查残留其他项目单位名</p>
        </div>
        <div class="bp-cell">
          <span class="bp-code">F4/F5 勾稽与偏离表</span>
          <p>检出 ★ 漏应答与质保期前后承诺矛盾，一键导出带真实页码/偏移的 Word 偏离表</p>
        </div>
      </div>

      <div class="bp-footer-note">
        合规声明：本系统定位于印前体检与风险提示工具，全部引用强制经过 verify_quote 逐字溯源校验。
      </div>
    </section>

    <!-- 右翼：高密度登录门禁面板 -->
    <section class="login-vault-pane">
      <div class="vault-card">
        <div class="vault-hd">
          <h2>登录投标防废标工作台</h2>
          <p>请输入投标部授权账号或点击下方演示账号一键填入</p>
        </div>

        <form class="vault-form" @submit.prevent="handleLogin">
          <div class="field-block">
            <label>登录账号 (Username)</label>
            <el-input
              v-model.trim="form.username"
              placeholder="请输入用户名（如 liming / admin）"
              size="large"
            />
          </div>

          <div class="field-block">
            <label>安全密码 (Password)</label>
            <el-input
              v-model="form.password"
              type="password"
              show-password
              placeholder="请输入登录密码"
              size="large"
              @keyup.enter="handleLogin"
            />
          </div>

          <el-button
            type="primary"
            size="large"
            class="vault-submit-btn"
            :loading="loading"
            @click="handleLogin"
          >
            进入防废标体检工作台 →
          </el-button>
        </form>

        <!-- 预置演示账号一键填充区 -->
        <div class="demo-preset-zone">
          <div class="dp-title">预置种子账号（点击一键填充账号与密码）</div>
          <div class="dp-list">
            <div
              v-for="item in DEMO_PRESETS"
              :key="item.username"
              class="dp-item"
              @click="fillDemoAccount(item)"
            >
              <div class="dp-row1">
                <span class="dp-role">{{ item.roleLabel }}</span>
                <span class="dp-cred mono-num">{{ item.username }} / {{ item.password }}</span>
              </div>
              <div class="dp-desc">{{ item.dept }} · {{ item.desc }}</div>
            </div>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.bg-login-screen {
  min-height: 100vh;
  display: grid;
  grid-template-columns: 1.15fr 1fr;
  background: #0b1324;
}

.login-blueprint-pane {
  padding: 56px 64px;
  background:
    radial-gradient(circle at 18% 20%, rgba(29, 78, 216, 0.28), transparent 45%),
    linear-gradient(160deg, #0b1324 0%, #111c35 100%);
  color: #e2e8f0;
  display: flex;
  flex-direction: column;
  justify-content: center;
  border-right: 1px solid rgba(148, 163, 184, 0.16);
}

.bp-top-tag {
  display: inline-block;
  align-self: flex-start;
  padding: 4px 12px;
  border-radius: 4px;
  background: rgba(56, 189, 248, 0.14);
  border: 1px solid rgba(56, 189, 248, 0.35);
  color: #38bdf8;
  font-size: 11.5px;
  font-weight: 800;
  letter-spacing: 0.8px;
  margin-bottom: 20px;
}

.bp-headline {
  font-size: 34px;
  font-weight: 900;
  line-height: 1.25;
  color: #ffffff;
  margin-bottom: 16px;
}

.bp-lead {
  font-size: 14.5px;
  color: #94a3b8;
  line-height: 1.7;
  max-width: 580px;
  margin-bottom: 32px;
}

.bp-matrix-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  margin-bottom: 36px;
}

.bp-cell {
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid rgba(148, 163, 184, 0.16);
  border-left: 3px solid #38bdf8;
  border-radius: 8px;
  padding: 14px 16px;
}

.bp-code {
  display: block;
  font-size: 13px;
  font-weight: 800;
  color: #f8fafc;
  margin-bottom: 5px;
}

.bp-cell p {
  font-size: 12px;
  color: #94a3b8;
  line-height: 1.55;
}

.bp-footer-note {
  font-size: 12px;
  color: #64748b;
}

.login-vault-pane {
  background: #f3f5f8;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 36px;
}

.vault-card {
  width: 100%;
  max-width: 460px;
  background: #ffffff;
  border: 1px solid #dce3ec;
  border-radius: 14px;
  padding: 36px 34px;
  box-shadow: 0 20px 45px -12px rgba(15, 23, 42, 0.12);
}

.vault-hd h2 {
  font-size: 22px;
  font-weight: 900;
  color: #0b1324;
}

.vault-hd p {
  font-size: 12.5px;
  color: #64748b;
  margin-top: 4px;
  margin-bottom: 24px;
}

.field-block {
  margin-bottom: 18px;
}

.field-block label {
  display: block;
  font-size: 12.5px;
  font-weight: 700;
  color: #334155;
  margin-bottom: 6px;
}

.vault-submit-btn {
  width: 100%;
  font-weight: 800;
  margin-top: 6px;
}

.demo-preset-zone {
  margin-top: 26px;
  padding-top: 18px;
  border-top: 1px dashed #cbd5e1;
}

.dp-title {
  font-size: 11.5px;
  font-weight: 800;
  color: #475569;
  margin-bottom: 10px;
}

.dp-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.dp-item {
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid #e2e8f0;
  background: #f8fafc;
  cursor: pointer;
  transition: all 0.15s;
}

.dp-item:hover {
  border-color: #1d4ed8;
  background: #eff6ff;
}

.dp-row1 {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 3px;
}

.dp-role {
  font-size: 12.5px;
  font-weight: 800;
  color: #0f172a;
}

.dp-cred {
  font-size: 11.5px;
  color: #1d4ed8;
  font-weight: 700;
}

.dp-desc {
  font-size: 11.5px;
  color: #64748b;
}

@media (max-width: 960px) {
  .bg-login-screen {
    grid-template-columns: 1fr;
  }
  .login-blueprint-pane {
    display: none;
  }
}
</style>