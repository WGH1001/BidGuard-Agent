<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus/es/components/message/index'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { authApi } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { SIDEBAR_THEMES, useAppStore } from '@/stores/app'
import { useNotificationStore, type TaskNotificationItem, type TrackedBackgroundJob } from '@/stores/notifications'
import type { SidebarThemeMode } from '@/api/types'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const appStore = useAppStore()
const notificationStore = useNotificationStore()
const notifPopoverOpen = ref(false)
const userMenuOpen = ref(false)
const pwdDialogOpen = ref(false)
const pwdSubmitting = ref(false)
const pwdForm = reactive({
  old_password: '',
  new_password: '',
  confirm_password: '',
})

const isLoginRoute = computed(() => route.path === '/login')

interface NavItem {
  path: string
  label: string
  icon: string
  group: 'core' | 'asset' | 'system'
  adminOnly?: boolean
}

const NAV_ITEMS: NavItem[] = [
  { path: '/dashboard', label: '防废标体检总览', icon: 'M3 3h6v6H3V3zm8 0h6v4h-6V3zm0 6h6v8h-6V9zM3 11h6v6H3v-6z', group: 'core' },
  { path: '/extract', label: '招标红线与★提取', icon: 'M5 3h7l4 4v10a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zm3 6h5m-5 3h5m-5 3h3', group: 'core' },
  { path: '/checks', label: '封标前防废标体检台', icon: 'M10 2.5l6.5 2.7v4.6c0 4.1-2.8 7.5-6.5 8.7-3.7-1.2-6.5-4.6-6.5-8.7V5.2L10 2.5zm-2.5 8l1.8 1.8 3.5-3.8', group: 'core' },
  { path: '/deviation', label: '偏离表一键生成', icon: 'M3.5 4.5h13v11h-13v-11zm0 4h13m-8.5-4v11', group: 'core' },
  { path: '/radar', label: '投前控标雷达', icon: 'M10 17a7 7 0 1 0 0-14 7 7 0 0 0 0 14zm0-3.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zm0-3.5l3.8-3.8', group: 'core' },
  { path: '/qa', label: '标书专家追问', icon: 'M4 5.5A1.5 1.5 0 0 1 5.5 4h9A1.5 1.5 0 0 1 16 5.5v6A1.5 1.5 0 0 1 14.5 13H9l-3.5 3v-3H5.5A1.5 1.5 0 0 1 4 11.5v-6z', group: 'core' },
  { path: '/similarity', label: '多标书交叉比对', icon: 'M7 4H4.5A1.5 1.5 0 0 0 3 5.5v9A1.5 1.5 0 0 0 4.5 16H7m6-12h2.5A1.5 1.5 0 0 1 17 5.5v9a1.5 1.5 0 0 1-1.5 1.5H13M10 3v14', group: 'core' },
  { path: '/composer', label: '标书编制输出中心', icon: 'M13.2 3.3a1.8 1.8 0 0 1 2.5 2.5L7.6 13.9l-3.6.9.9-3.6 8.3-7.9z', group: 'core' },
  { path: '/rules', label: '废标规则与证照底表', icon: 'M4.5 4h11v12h-11V4zm2.5 3.5h6m-6 3h6m-6 3h4', group: 'asset' },
  { path: '/reports', label: '封标交付中心与审计', icon: 'M3.5 6.5h13v9.5a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1V6.5zm2-3h9l1.5 3h-12l1.5-3zm2.5 7h4', group: 'asset' },
  { path: '/users', label: '部门与账号管理', icon: 'M7.5 9.5a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm-4.5 6.5a4.5 4.5 0 0 1 9 0m3-7a2.2 2.2 0 1 0 0-4.4M16.5 16a3.5 3.5 0 0 0-2.5-3.3', group: 'system', adminOnly: true },
  { path: '/model-settings', label: '私有模型服务配置', icon: 'M10 6.8a3.2 3.2 0 1 0 0 6.4 3.2 3.2 0 0 0 0-6.4zm0-4.3v1.6m0 11.8v1.6m-7.5-7.5h1.6m11.8 0h1.6M4.7 4.7l1.1 1.1m8.4 8.4l1.1 1.1m0-10.6l-1.1 1.1m-8.4 8.4l-1.1 1.1', group: 'system' },
]

const coreNavs = computed(() => NAV_ITEMS.filter((i) => i.group === 'core'))
const assetNavs = computed(() => NAV_ITEMS.filter((i) => i.group === 'asset'))
const systemNavs = computed(() =>
  NAV_ITEMS.filter((i) => i.group === 'system' && (!i.adminOnly || authStore.isAdmin)),
)

const currentPageTitle = computed(() => (route.meta.title as string) || '智标合规工作台')
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

watch(
  () => [authStore.isAuthenticated, authStore.user?.username, authStore.user?.tenant_id] as const,
  ([authed, uname, tid]) => {
    if (authed && uname) {
      notificationStore.syncUserScope(uname, tid)
    } else {
      notificationStore.stopAndReset()
    }
  },
  { immediate: true },
)

function formatNotifTime(iso: string): string {
  try {
    const dt = new Date(iso)
    if (Number.isNaN(dt.getTime())) return ''
    return dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch {
    return ''
  }
}

function handleClickNotification(item: TaskNotificationItem): void {
  notifPopoverOpen.value = false
  notificationStore.openNotification(item)
}

function handleClickTrackedJob(job: TrackedBackgroundJob): void {
  notifPopoverOpen.value = false
  notificationStore.navigateToTarget(job.routePath, job.routeQuery)
}

function openPwdDialog(): void {
  userMenuOpen.value = false
  pwdForm.old_password = ''
  pwdForm.new_password = ''
  pwdForm.confirm_password = ''
  pwdDialogOpen.value = true
}

async function submitPwdChange(): Promise<void> {
  if (!pwdForm.old_password.trim()) {
    ElMessage.warning('请输入当前原密码')
    return
  }
  if (pwdForm.new_password.trim().length < 6) {
    ElMessage.warning('新密码长度不能少于 6 位')
    return
  }
  if (pwdForm.new_password !== pwdForm.confirm_password) {
    ElMessage.warning('两次输入的新密码不一致')
    return
  }
  pwdSubmitting.value = true
  try {
    await authApi.changePassword({
      old_password: pwdForm.old_password,
      new_password: pwdForm.new_password.trim(),
    })
    ElMessage.success('密码修改成功')
    pwdDialogOpen.value = false
  } finally {
    pwdSubmitting.value = false
  }
}

function handleLogout(): void {
  userMenuOpen.value = false
  notificationStore.stopAndReset()
  authStore.logout()
  ElMessage.info('已安全退出智标工作台')
  void router.push('/login')
}
</script>

<template>
  <el-config-provider :locale="zhCn">
    <router-view v-if="isLoginRoute" />

    <div v-else class="bg-workspace-layout">
      <!-- 左置全高工业控制台侧栏（支持三色主题实时切换，默认 #e2e8f0 工业浅岩灰） -->
      <aside class="bg-sidebar">
        <div class="sb-brand-block" @click="router.push('/dashboard')">
          <div class="sb-shield-mark">智</div>
          <div class="sb-brand-meta">
            <div class="sb-brand-name">智标</div>
            <div class="sb-brand-sub">标书合规审查与智能编制 Agent</div>
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
          <span class="nav-item-inner">
            <svg class="nav-svg" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">
              <path :d="item.icon" />
            </svg>
            <span class="nav-txt">{{ item.label }}</span>
          </span>
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
          <span class="nav-item-inner">
            <svg class="nav-svg" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">
              <path :d="item.icon" />
            </svg>
            <span class="nav-txt">{{ item.label }}</span>
          </span>
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
          <span class="nav-item-inner">
            <svg class="nav-svg" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">
              <path :d="item.icon" />
            </svg>
            <span class="nav-txt">{{ item.label }}</span>
          </span>
        </button>
      </nav>

      <!-- 侧栏底栏高对比度工程铭牌 -->
      <div class="sb-telemetry-foot">
        <div class="foot-line"><b>算术/效期沙盒：</b>确定性精算核验</div>
        <div class="foot-line"><b>防幻觉铁闸：</b>全文逐字溯源校验</div>
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
          <!-- E44：顶栏全局任务完成通知中心（跨页保活，点击直达详情） -->
          <el-popover
            v-model:visible="notifPopoverOpen"
            placement="bottom-end"
            :width="370"
            trigger="click"
          >
            <template #reference>
              <el-badge
                :value="notificationStore.unreadCount"
                :hidden="notificationStore.unreadCount === 0"
                :max="99"
                class="tb-notif-badge"
              >
                <button
                  type="button"
                  class="tb-notif-btn"
                  :class="{ 'is-running': notificationStore.runningCount > 0 }"
                  title="查看后台任务进度与完成通知"
                >
                  <span v-if="notificationStore.runningCount > 0" class="notif-pulse-dot"></span>
                  <span>
                    {{
                      notificationStore.runningCount > 0
                        ? `任务运行中 (${notificationStore.runningCount})`
                        : '任务通知'
                    }}
                  </span>
                </button>
              </el-badge>
            </template>

            <div class="notif-pop">
              <div class="notif-pop-hd">
                <span class="notif-pop-title">后台任务与完成通知</span>
                <div class="notif-pop-ops">
                  <button
                    v-if="notificationStore.unreadCount > 0"
                    type="button"
                    class="notif-link-btn"
                    @click="notificationStore.markAllRead()"
                  >
                    全部已读
                  </button>
                  <button
                    v-if="notificationStore.notifications.length > 0"
                    type="button"
                    class="notif-link-btn muted"
                    @click="notificationStore.clearNotifications()"
                  >
                    清空
                  </button>
                </div>
              </div>

              <div v-if="notificationStore.trackedJobs.length > 0" class="notif-sec">
                <div class="notif-sec-lbl">正在运行 ({{ notificationStore.trackedJobs.length }})</div>
                <div
                  v-for="job in notificationStore.trackedJobs"
                  :key="job.key"
                  class="notif-item is-active-job"
                  @click="handleClickTrackedJob(job)"
                >
                  <div class="ni-top">
                    <span class="ni-kind">{{ job.kindLabel }}</span>
                    <span class="ni-time">{{ job.progress ? `${job.progress}%` : '执行中…' }}</span>
                  </div>
                  <div class="ni-title">{{ job.title }}</div>
                  <div class="ni-summary">{{ job.stageDetail || '后台处理中，切页不中断…' }}</div>
                </div>
              </div>

              <div class="notif-sec">
                <div class="notif-sec-lbl">近期完成记录</div>
                <div v-if="notificationStore.notifications.length === 0" class="notif-empty">
                  暂无任务完成通知；发起体检、评分预估、编制、查重或控标扫描后将自动在此提醒。
                </div>
                <div v-else class="notif-list">
                  <div
                    v-for="item in notificationStore.notifications"
                    :key="item.id"
                    class="notif-item"
                    :class="[`tone-${item.tone}`, { 'is-unread': !item.read }]"
                    @click="handleClickNotification(item)"
                  >
                    <div class="ni-top">
                      <span class="ni-kind">{{ item.kindLabel }}</span>
                      <span class="ni-time">{{ formatNotifTime(item.createdAt) }}</span>
                    </div>
                    <div class="ni-title">{{ item.title }}</div>
                    <div class="ni-summary">{{ item.summary }}</div>
                  </div>
                </div>
              </div>
            </div>
          </el-popover>

          <el-popover
            v-model:visible="userMenuOpen"
            placement="bottom-end"
            :width="236"
            trigger="click"
          >
            <template #reference>
              <button type="button" class="tb-user-chip is-interactive" title="账号设置与退出">
                <span class="u-name">{{ userDisplayName }}</span>
                <span
                  v-if="showSeparateRoleBadge"
                  class="u-role"
                  :class="authStore.isAdmin ? 'is-admin' : 'is-member'"
                >
                  {{ roleLabel }}
                </span>
                <span class="u-caret">▾</span>
              </button>
            </template>

            <div class="user-pop">
              <div class="user-pop-hd">
                <div class="up-name-row">
                  <span class="up-display">{{ userDisplayName || roleLabel }}</span>
                  <span class="u-role" :class="authStore.isAdmin ? 'is-admin' : 'is-member'">
                    {{ roleLabel }}
                  </span>
                </div>
                <div class="up-meta">
                  <span v-if="authStore.user?.username">@{{ authStore.user.username }} · </span>
                  <span>{{ authStore.tenantName }}</span>
                </div>
              </div>
              <div class="user-pop-actions">
                <button type="button" class="user-pop-btn" @click="openPwdDialog">
                  <span>修改登录密码</span>
                </button>
                <button type="button" class="user-pop-btn is-danger" @click="handleLogout">
                  <span>退出当前账号</span>
                </button>
              </div>
            </div>
          </el-popover>
        </div>
      </header>

      <main class="bg-content-canvas">
        <router-view v-slot="{ Component }">
          <keep-alive :include="['ExtractView', 'RadarView', 'DeviationView', 'QaChatView', 'BidComposerView', 'SimilarityCheckView']">
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </main>
    </div>
  </div>

    <el-dialog
      v-model="pwdDialogOpen"
      title="修改登录密码"
      width="420px"
      append-to-body
      :close-on-click-modal="false"
    >
      <el-form label-position="top" @submit.prevent="submitPwdChange">
        <el-form-item label="当前原密码" required>
          <el-input
            v-model="pwdForm.old_password"
            type="password"
            show-password
            placeholder="请输入当前登录密码"
          />
        </el-form-item>
        <el-form-item label="新密码（至少 6 位字符）" required>
          <el-input
            v-model="pwdForm.new_password"
            type="password"
            show-password
            placeholder="请输入新密码"
          />
        </el-form-item>
        <el-form-item label="确认新密码" required>
          <el-input
            v-model="pwdForm.confirm_password"
            type="password"
            show-password
            placeholder="请再次输入新密码"
            @keyup.enter="submitPwdChange"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pwdDialogOpen = false">取消</el-button>
        <el-button type="primary" :loading="pwdSubmitting" @click="submitPwdChange">确认修改</el-button>
      </template>
    </el-dialog>
  </el-config-provider>
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
  font-size: 10.5px;
  color: var(--sb-muted);
  margin-top: 2px;
  white-space: nowrap;
  letter-spacing: -0.15px;
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
  height: 36px;
  padding: 0 10px;
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

.nav-item-inner {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.nav-svg {
  width: 15px;
  height: 15px;
  flex-shrink: 0;
  opacity: 0.72;
}

.sb-nav-link.is-active .nav-svg {
  opacity: 1;
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
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 5px 12px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-line);
  border-radius: 6px;
}

.tb-user-chip.is-interactive {
  cursor: pointer;
  transition: all 0.15s ease;
}

.tb-user-chip.is-interactive:hover {
  border-color: var(--brand);
  background: #ffffff;
}

.u-caret {
  font-size: 11px;
  color: var(--txt-muted);
  margin-left: 2px;
}

.user-pop {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.user-pop-hd {
  padding-bottom: 8px;
  border-bottom: 1px solid #e2e8f0;
}

.up-name-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.up-display {
  font-size: 13.5px;
  font-weight: 800;
  color: #0f172a;
}

.up-meta {
  margin-top: 3px;
  font-size: 11.5px;
  color: #64748b;
}

.user-pop-actions {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.user-pop-btn {
  width: 100%;
  text-align: left;
  padding: 7px 10px;
  border-radius: 6px;
  border: none;
  background: transparent;
  color: #1e293b;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.14s ease;
}

.user-pop-btn:hover {
  background: #f1f5f9;
  color: var(--brand);
}

.user-pop-btn.is-danger {
  color: #b91c1c;
}

.user-pop-btn.is-danger:hover {
  background: #fef2f2;
  color: #991b1b;
}

.tb-notif-badge {
  display: inline-flex;
  align-items: center;
}

.tb-notif-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 29px;
  padding: 0 11px;
  border-radius: 6px;
  border: 1px solid var(--border-line);
  background: var(--bg-subtle);
  color: var(--txt-strong);
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
  transition: all 0.15s ease;
}

.tb-notif-btn:hover {
  border-color: var(--brand);
  color: var(--brand);
}

.tb-notif-btn.is-running {
  background: #eff6ff;
  border-color: #93c5fd;
  color: #1d4ed8;
}

.notif-pulse-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #2563eb;
  animation: bgNotifPulse 1.4s ease-in-out infinite;
}

@keyframes bgNotifPulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(0.8); }
}

.notif-pop {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.notif-pop-hd {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 8px;
  border-bottom: 1px solid #e2e8f0;
}

.notif-pop-title {
  font-size: 13.5px;
  font-weight: 800;
  color: #0f172a;
}

.notif-pop-ops {
  display: flex;
  gap: 10px;
}

.notif-link-btn {
  border: none;
  background: transparent;
  color: #2563eb;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  padding: 0;
}

.notif-link-btn.muted {
  color: #64748b;
}

.notif-sec-lbl {
  font-size: 11px;
  font-weight: 800;
  color: #64748b;
  margin-bottom: 6px;
}

.notif-list {
  max-height: 300px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.notif-item {
  padding: 8px 10px;
  border-radius: 7px;
  border: 1px solid #e2e8f0;
  background: #f8fafc;
  cursor: pointer;
  transition: border-color 0.14s ease, background-color 0.14s ease;
}

.notif-item:hover {
  border-color: #93c5fd;
  background: #eff6ff;
}

.notif-item.is-unread {
  background: #f0f9ff;
  border-color: #7dd3fc;
}

.notif-item.is-active-job {
  background: #eff6ff;
  border-color: #bfdbfe;
  margin-bottom: 6px;
}

.notif-item.tone-error {
  border-left: 3px solid #dc2626;
}

.notif-item.tone-warning {
  border-left: 3px solid #d97706;
}

.notif-item.tone-success {
  border-left: 3px solid #16a34a;
}

.ni-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 3px;
}

.ni-kind {
  font-size: 11px;
  font-weight: 800;
  color: #1e40af;
}

.ni-time {
  font-size: 11px;
  color: #64748b;
}

.ni-title {
  font-size: 12.5px;
  font-weight: 700;
  color: #0f172a;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.ni-summary {
  font-size: 11.5px;
  color: #475569;
  margin-top: 2px;
  line-height: 1.45;
}

.notif-empty {
  padding: 16px 8px;
  text-align: center;
  font-size: 12px;
  color: #94a3b8;
  line-height: 1.6;
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