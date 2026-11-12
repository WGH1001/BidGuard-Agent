<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus/es/components/message/index'
import { useAuthStore } from '@/stores/auth'
import { SIDEBAR_THEMES, useAppStore } from '@/stores/app'
import type { SidebarThemeMode } from '@/api/types'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const appStore = useAppStore()

const isLoginRoute = computed(() => route.path === '/login')

interface NavItem {
  path: string
  label: string
  badge?: string
  group: 'core' | 'asset' | 'system'
}

const NAV_ITEMS: NavItem[] = [
  { path: '/dashboard', label: '防废标体检总览', badge: '大盘', group: 'core' },
  { path: '/extract', label: '招标红线与★提取', badge: 'F1', group: 'core' },
  { path: '/checks', label: '封标前防废标体检台', badge: 'F2-F4', group: 'core' },
  { path: '/deviation', label: '偏离表一键生成', badge: 'F5', group: 'core' },
  { path: '/radar', label: '投前控标雷达', badge: 'F6', group: 'core' },
  { path: '/qa', label: '标书专家追问', badge: 'F7', group: 'core' },
  { path: '/rules', label: '废标规则与证照底表', group: 'asset' },
  { path: '/reports', label: '封标交付中心与审计', group: 'asset' },
  { path: '/model-settings', label: '私有模型服务配置', group: 'system' },
  { path: '/change-password', label: '修改登录密码', group: 'system' },
]

const coreNavs = computed(() => NAV_ITEMS.filter((i) => i.group === 'core'))
const assetNavs = computed(() => NAV_ITEMS.filter((i) => i.group === 'asset'))
const systemNavs = computed(() => NAV_ITEMS.filter((i) => i.group === 'system'))

const currentPageTitle = computed(() => (route.meta.title as string) || '防废标体检工作台')
const roleLabel = computed(() => (authStore.isAdmin ? '系统管理员' : '投标商务专员'))
const userDisplayName = computed(
  () => (authStore.user?.display_name || authStore.user?.username || '').trim(),
)
const showSeparateRoleBadge = computed(
  () => Boolean(userDisplayName.value && userDisplayName.value !== roleLabel.value),
)

function switchTheme(mode: SidebarThemeMode): void {
  appStore.setSidebarTheme(mode)
}

function handleLogout(): void {
  authStore.logout()
  ElMessage.info('已安全退出投标防废标工作台')
  void router.push('/login')
}
</script>

<template>
  <router-view v-if="isLoginRoute" />

  <div v-else class="bg-workspace-layout">
    <!-- 左置全高工业控制台侧栏（支持三色主题实时切换，默认 #e2e8f0 工业浅岩灰） -->
    <aside class="bg-sidebar">
      <div class="sb-brand-block" @click="router.push('/dashboard')">
        <div class="sb-shield-mark">BG</div>
        <div class="sb-brand-meta">
          <div class="sb-brand-name">BidGuard-Agent</div>
          <div class="sb-brand-sub">卖方投标防废标体检台</div>
        </div>
      </div>

      <!-- 侧栏内嵌式三色主题切换器 -->
      <div class="sb-theme-switcher">
        <span class="sb-theme-label">侧栏外观</span>
        <div class="sb-theme-pills">
          <button
            v-for="t in SIDEBAR_THEMES"
            :key="t.key"
            type="button"
            class="sb-theme-btn"
            :class="{ active: appStore.sidebarTheme === t.key }"
            :title="t.label"
            @click="switchTheme(t.key)"
          >
            <span class="swatch-dot" :style="{ backgroundColor: t.swatch }"></span>
            <span>{{ t.label.slice(0, 2) }}</span>
          </button>
        </div>
      </div>

      <!-- 11 项菜单导航区（8 业务视图 + 2 系统视图 + 底部登出） -->
      <nav class="sb-nav-scroll">
        <div class="sb-group-hd">封标防废标核心链路</div>
        <button
          v-for="item in coreNavs"
          :key="item.path"
          type="button"
          class="sb-nav-link"
          :class="{ 'is-active': route.path === item.path }"
          @click="router.push(item.path)"
        >
          <span class="nav-txt">{{ item.label }}</span>
          <span v-if="item.badge" class="nav-code-badge">{{ item.badge }}</span>
        </button>

        <div class="sb-group-hd">规则底座与封标归档</div>
        <button
          v-for="item in assetNavs"
          :key="item.path"
          type="button"
          class="sb-nav-link"
          :class="{ 'is-active': route.path === item.path }"
          @click="router.push(item.path)"
        >
          <span class="nav-txt">{{ item.label }}</span>
        </button>

        <div class="sb-group-hd">模型与账户安全</div>
        <button
          v-for="item in systemNavs"
          :key="item.path"
          type="button"
          class="sb-nav-link"
          :class="{ 'is-active': route.path === item.path }"
          @click="router.push(item.path)"
        >
          <span class="nav-txt">{{ item.label }}</span>
        </button>

        <button type="button" class="sb-nav-link logout-item" @click="handleLogout">
          <span class="nav-txt">退出当前账号</span>
        </button>
      </nav>

      <!-- 侧栏底栏高对比度工程铭牌 -->
      <div class="sb-telemetry-foot">
        <div class="foot-line"><b>算术/效期沙盒：</b>0-Token 确定性核验</div>
        <div class="foot-line"><b>防幻觉铁闸：</b>逐字 verify_quote 溯源</div>
        <div class="foot-line"><b>商业标书保密：</b>支持内网私有模型网关</div>
      </div>
    </aside>

    <!-- 右侧主工作舱：顶部悬浮态势带 + 内容画布 -->
    <div class="bg-main-stage">
      <header class="bg-topbar">
        <div class="tb-left">
          <span class="tb-dept-pill">{{ authStore.tenantName }}</span>
          <span class="tb-divider">/</span>
          <h2 class="tb-page-title">{{ currentPageTitle }}</h2>
        </div>

        <div class="tb-right">
          <div class="tb-user-chip">
            <span class="u-name">{{ userDisplayName }}</span>
            <span
              v-if="showSeparateRoleBadge"
              class="u-role"
              :class="authStore.isAdmin ? 'is-admin' : 'is-member'"
            >
              {{ roleLabel }}
            </span>
          </div>
        </div>
      </header>

      <main class="bg-content-canvas">
        <router-view v-slot="{ Component }">
          <keep-alive :include="['ExtractView', 'RadarView', 'QaChatView']">
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </main>
    </div>
  </div>
</template>

<style scoped>
.bg-workspace-layout {
  display: grid;
  grid-template-columns: 248px 1fr;
  min-height: 100vh;
  background: var(--bg-canvas);
}

.bg-sidebar {
  background: var(--sb-bg);
  color: var(--sb-text);
  border-right: 1px solid var(--sb-border);
  display: flex;
  flex-direction: column;
  padding: 16px 12px;
  position: sticky;
  top: 0;
  height: 100vh;
  transition: background-color 0.2s ease, color 0.2s ease;
}

.sb-brand-block {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 8px 10px 14px;
  border-bottom: 1px solid var(--sb-border);
  cursor: pointer;
}

.sb-shield-mark {
  width: 36px;
  height: 36px;
  border-radius: 8px;
  background: var(--sb-active-bg);
  color: #ffffff;
  font-weight: 900;
  font-size: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  letter-spacing: 0.5px;
  flex-shrink: 0;
}

.sb-brand-name {
  font-size: 15.5px;
  font-weight: 900;
  color: var(--sb-text);
  line-height: 1.2;
}

.sb-brand-sub {
  font-size: 11.5px;
  color: var(--sb-muted);
  margin-top: 2px;
}

.sb-theme-switcher {
  margin: 12px 4px 8px;
  padding: 8px 10px;
  background: var(--sb-surface);
  border: 1px solid var(--sb-border);
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}

.sb-theme-label {
  font-size: 11px;
  font-weight: 700;
  color: var(--sb-text-sec);
}

.sb-theme-pills {
  display: flex;
  gap: 4px;
}

.sb-theme-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 7px;
  border-radius: 5px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--sb-text-sec);
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
}

.sb-theme-btn.active {
  background: var(--bg-panel);
  color: #0f172a;
  border-color: var(--sb-border);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
}

.swatch-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  border: 1px solid rgba(0, 0, 0, 0.25);
}

.sb-nav-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 4px 2px;
}

.sb-group-hd {
  font-size: 10.5px;
  font-weight: 800;
  color: var(--sb-muted);
  padding: 12px 10px 5px;
  letter-spacing: 0.6px;
}

.sb-nav-link {
  width: 100%;
  height: 38px;
  padding: 0 11px;
  margin-bottom: 3px;
  border-radius: 7px;
  border: none;
  background: transparent;
  color: var(--sb-text);
  font-size: 13px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: space-between;
  cursor: pointer;
  transition: all 0.14s ease;
  text-align: left;
}

.sb-nav-link:hover {
  background: var(--sb-hover);
}

.sb-nav-link.is-active {
  background: var(--sb-active-bg);
  color: var(--sb-active-text);
  font-weight: 700;
}

.nav-code-badge {
  font-size: 10.5px;
  font-weight: 800;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--sb-badge-bg);
  color: var(--sb-badge-text);
}

.sb-nav-link.is-active .nav-code-badge {
  background: rgba(255, 255, 255, 0.22);
  color: #ffffff;
}

.logout-item {
  margin-top: 8px;
  color: #dc2626;
}

.sb-telemetry-foot {
  margin-top: auto;
  padding: 11px 12px;
  background: var(--sb-surface);
  border: 1px solid var(--sb-border);
  border-radius: 8px;
  font-size: 11px;
  color: var(--sb-text-sec);
  line-height: 1.65;
}

.sb-telemetry-foot b {
  color: var(--sb-text);
  font-weight: 800;
}

.bg-main-stage {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.bg-topbar {
  height: 44px;
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border-line);
  padding: 0 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  position: sticky;
  top: 0;
  z-index: 30;
}

.tb-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.tb-dept-pill {
  background: var(--brand-soft);
  color: var(--brand);
  border: 1px solid var(--brand-border);
  padding: 3px 10px;
  border-radius: 5px;
  font-size: 12px;
  font-weight: 700;
}

.tb-divider {
  color: var(--txt-muted);
}

.tb-page-title {
  font-size: 16px;
  font-weight: 800;
  color: var(--txt-strong);
}

.tb-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.tb-quick-btn {
  height: 34px;
  padding: 0 13px;
  border-radius: 6px;
  border: 1px solid var(--border-strong);
  background: #ffffff;
  color: var(--txt-body);
  font-size: 12.5px;
  font-weight: 700;
  cursor: pointer;
  transition: all 0.15s;
}

.tb-quick-btn:hover {
  border-color: var(--brand);
  color: var(--brand);
}

.tb-quick-btn.primary {
  background: var(--brand);
  border-color: var(--brand);
  color: #ffffff;
}

.tb-quick-btn.primary:hover {
  background: var(--brand-hover);
}

.tb-user-chip {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 12px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-line);
  border-radius: 6px;
}

.u-name {
  font-weight: 700;
  color: var(--txt-strong);
  font-size: 12.5px;
}

.u-role {
  font-size: 11px;
  font-weight: 800;
  padding: 1px 7px;
  border-radius: 4px;
}

.u-role.is-admin {
  background: #fee2e2;
  color: #991b1b;
}

.u-role.is-member {
  background: #e0f2fe;
  color: #075985;
}

.bg-content-canvas {
  flex: 1;
  padding: 14px 20px;
  max-width: 1640px;
  width: 100%;
  margin: 0 auto;
}
</style>