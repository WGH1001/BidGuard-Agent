import axios, { type AxiosInstance, type AxiosRequestConfig, type AxiosResponse } from 'axios'
import { ElMessage } from 'element-plus/es/components/message/index'

export const AUTH_STORAGE_KEY = 'bidguard_auth_v1'

const http: AxiosInstance = axios.create({
  baseURL: '/api',
  timeout: 180000,
})

export function getStoredToken(): string {
  try {
    const raw = localStorage.getItem(AUTH_STORAGE_KEY)
    if (!raw) return ''
    const parsed = JSON.parse(raw) as { token?: string }
    return typeof parsed?.token === 'string' ? parsed.token : ''
  } catch {
    return ''
  }
}

http.interceptors.request.use(
  (config) => {
    const token = getStoredToken()
    if (token && config.headers) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error),
)

http.interceptors.response.use(
  (response: AxiosResponse) => response,
  (error) => {
    const status = error?.response?.status as number | undefined
    const data = error?.response?.data as
      | { detail?: { code?: string; message?: string } | string }
      | undefined

    let errMsg = '请求失败，请稍后重试'
    if (data?.detail) {
      if (typeof data.detail === 'string') {
        errMsg = data.detail
      } else if (typeof data.detail === 'object' && data.detail.message) {
        errMsg = data.detail.message
      }
    } else if (typeof error?.message === 'string' && error.message) {
      errMsg = error.message
    }

    if (status === 401) {
      localStorage.removeItem(AUTH_STORAGE_KEY)
      ElMessage.warning(errMsg || '登录凭据已失效或过期，请重新登录')
      if (window.location.pathname !== '/login') {
        window.location.href = '/login'
      }
    } else if (status === 404) {
      ElMessage.warning(errMsg || '请求的资源不存在或无权访问')
    } else if (status && status >= 400) {
      ElMessage.error(errMsg)
    }

    return Promise.reject(error)
  },
)

export async function get<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
  const res = await http.get<T>(url, config)
  return res.data
}

export async function post<T>(
  url: string,
  data?: unknown,
  config?: AxiosRequestConfig,
): Promise<T> {
  const res = await http.post<T>(url, data, config)
  return res.data
}

export async function put<T>(
  url: string,
  data?: unknown,
  config?: AxiosRequestConfig,
): Promise<T> {
  const res = await http.put<T>(url, data, config)
  return res.data
}

export async function del<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
  const res = await http.delete<T>(url, config)
  return res.data
}

export default http