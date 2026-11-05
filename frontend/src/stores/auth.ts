import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { authApi } from '@/api'
import { AUTH_STORAGE_KEY } from '@/api/http'
import type { LoginRequest, UserProfileOut } from '@/api/types'

interface PersistedAuthData {
  token: string
  user: UserProfileOut | null
}

function readStoredAuth(): PersistedAuthData {
  try {
    const raw = localStorage.getItem(AUTH_STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<PersistedAuthData>
      if (parsed && typeof parsed.token === 'string' && parsed.token) {
        return {
          token: parsed.token,
          user: parsed.user || null,
        }
      }
    }
  } catch {
    // 忽略损坏缓存
  }
  return { token: '', user: null }
}

export const useAuthStore = defineStore('auth', () => {
  const initial = readStoredAuth()
  const token = ref<string>(initial.token)
  const user = ref<UserProfileOut | null>(initial.user)

  const isAuthenticated = computed(() => Boolean(token.value))
  const isAdmin = computed(() => user.value?.role === 'admin')
  const tenantName = computed(() => {
    const tid = user.value?.tenant_id
    if (tid === 2) return '弱电智能化工程部'
    if (tid === 3) return '智能装备销售中心'
    return '政企系统集成投标部'
  })

  function persist(): void {
    localStorage.setItem(
      AUTH_STORAGE_KEY,
      JSON.stringify({
        token: token.value,
        user: user.value,
      }),
    )
  }

  async function login(payload: LoginRequest): Promise<UserProfileOut> {
    const res = await authApi.login(payload)
    token.value = res.token
    user.value = res.user
    persist()
    return res.user
  }

  async function refreshMe(): Promise<UserProfileOut | null> {
    if (!token.value) return null
    try {
      const profile = await authApi.me()
      user.value = profile
      persist()
      return profile
    } catch {
      return null
    }
  }

  function logout(): void {
    token.value = ''
    user.value = null
    localStorage.removeItem(AUTH_STORAGE_KEY)
  }

  return {
    token,
    user,
    isAuthenticated,
    isAdmin,
    tenantName,
    login,
    refreshMe,
    logout,
  }
})