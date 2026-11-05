import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

import LoginView from '@/views/LoginView.vue'
import DashboardView from '@/views/DashboardView.vue'
import ChangePasswordView from '@/views/ChangePasswordView.vue'
import ModelSettingsView from '@/views/ModelSettingsView.vue'
import PlaceholderView from '@/views/PlaceholderView.vue'

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
    component: PlaceholderView,
    meta: {
      title: 'F1 招标红线与★条款提取',
      moduleCode: 'F1',
      desc: '上传招标文件结构化拆解资格项、符合项、★/▲强制参数与评分细则表（C2 卡接入）',
    },
  },
  {
    path: '/checks',
    name: 'checks',
    component: PlaceholderView,
    meta: {
      title: '封标前防废标体检台',
      moduleCode: 'F2/F3/F4',
      desc: '双书上传、6 步流水线 SSE 实时核验、报价人工回填与风险项分级处置（C2 卡接入）',
    },
  },
  {
    path: '/deviation',
    name: 'deviation',
    component: PlaceholderView,
    meta: {
      title: 'F5 偏离表一键生成',
      moduleCode: 'F5',
      desc: '招标要求与我方投标响应逐条对齐预览，一键导出标准六列 Word 偏离表（C2 卡接入）',
    },
  },
  {
    path: '/radar',
    name: 'radar',
    component: PlaceholderView,
    meta: {
      title: 'F6 投前控标雷达',
      moduleCode: 'F6',
      desc: '立项前扫描招标文件疑似品牌绑定、原厂授权与排他性控标参数（C2 卡接入）',
    },
  },
  {
    path: '/qa',
    name: 'qa',
    component: PlaceholderView,
    meta: {
      title: 'F7 标书专家追问',
      moduleCode: 'F7',
      desc: '跨招投标双书多轮 ReAct 检索问答，强制附带原文摘录与页码/字符偏移（C3 卡接入）',
    },
  },
  {
    path: '/rules',
    name: 'rules',
    component: PlaceholderView,
    meta: {
      title: '废标规则与证照底表',
      moduleCode: 'RULES',
      desc: '配置开标日、最高限价、社保月份、自定义禁用词与企业投标证照到期预警台账（C3 卡接入）',
    },
  },
  {
    path: '/reports',
    name: 'reports',
    component: PlaceholderView,
    meta: {
      title: '封标交付中心与审计',
      moduleCode: 'DELIVER',
      desc: '一键打包下载投标书原位标红件、偏离表与人工终核签字单 ZIP，及管理员安全审计日志（C3 卡接入）',
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