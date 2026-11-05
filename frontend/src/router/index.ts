import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

import LoginView from '@/views/LoginView.vue'
import DashboardView from '@/views/DashboardView.vue'
import ExtractView from '@/views/ExtractView.vue'
import CheckDetailView from '@/views/CheckDetailView.vue'
import DeviationView from '@/views/DeviationView.vue'
import RadarView from '@/views/RadarView.vue'
import QaChatView from '@/views/QaChatView.vue'
import RulesAndCertsView from '@/views/RulesAndCertsView.vue'
import ReportsAndAuditView from '@/views/ReportsAndAuditView.vue'
import ChangePasswordView from '@/views/ChangePasswordView.vue'
import ModelSettingsView from '@/views/ModelSettingsView.vue'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: LoginView,
    meta: { public: true, title: '投标防废标工作台登录' },
  },
  {
    path: '/',
    redirect: '/dashboard',
  },
  {
    path: '/dashboard',
    name: 'dashboard',
    component: DashboardView,
    meta: { title: '防废标体检总览', group: '封标核验闭环' },
  },
  {
    path: '/extract',
    name: 'extract',
    component: ExtractView,
    meta: {
      title: 'F1 招标红线与★条款提取',
      moduleCode: 'F1',
    },
  },
  {
    path: '/checks',
    name: 'checks',
    component: CheckDetailView,
    meta: {
      title: '封标前防废标体检台',
      moduleCode: 'F2-F4',
    },
  },
  {
    path: '/deviation',
    name: 'deviation',
    component: DeviationView,
    meta: {
      title: 'F5 偏离表一键生成',
      moduleCode: 'F5',
    },
  },
  {
    path: '/radar',
    name: 'radar',
    component: RadarView,
    meta: {
      title: 'F6 投前控标雷达',
      moduleCode: 'F6',
    },
  },
  {
    path: '/qa',
    name: 'qa',
    component: QaChatView,
    meta: {
      title: 'F7 标书专家追问',
      moduleCode: 'F7',
    },
  },
  {
    path: '/rules',
    name: 'rules',
    component: RulesAndCertsView,
    meta: {
      title: '废标规则与证照底表',
      moduleCode: 'RULES',
    },
  },
  {
    path: '/reports',
    name: 'reports',
    component: ReportsAndAuditView,
    meta: {
      title: '封标交付中心与审计',
      moduleCode: 'DELIVER',
    },
  },
  {
    path: '/model-settings',
    name: 'model-settings',
    component: ModelSettingsView,
    meta: { title: '私有化与个人模型配置', group: '系统与安全' },
  },
  {
    path: '/change-password',
    name: 'change-password',
    component: ChangePasswordView,
    meta: { title: '修改账户密码', group: '系统与安全' },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  const authStore = useAuthStore()

  if (to.path === '/login') {
    if (authStore.isAuthenticated) {
      return { path: '/dashboard' }
    }
    return true
  }

  if (!authStore.isAuthenticated) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }

  return true
})

export default router