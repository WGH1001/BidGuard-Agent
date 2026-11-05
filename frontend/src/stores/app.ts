import { ref, watch } from 'vue'
import { defineStore } from 'pinia'
import type { SidebarThemeMode } from '@/api/types'

const THEME_STORAGE_KEY = 'bidguard_sidebar_theme'

export interface SidebarThemeOption {
  key: SidebarThemeMode
  label: string
  swatch: string
}

export const SIDEBAR_THEMES: SidebarThemeOption[] = [
  { key: 'slate', label: '工业浅岩灰', swatch: '#e2e8f0' },
  { key: 'carbon', label: '曜石暖炭灰', swatch: '#23201e' },
  { key: 'fjord', label: '深海峡湾青', swatch: '#18282b' },
]

function loadSavedTheme(): SidebarThemeMode {
  try {
    const saved = localStorage.getItem(THEME_STORAGE_KEY)
    if (saved === 'slate' || saved === 'carbon' || saved === 'fjord') {
      return saved
    }
  } catch {
    // ignore
  }
  return 'slate'
}

export const useAppStore = defineStore('app', () => {
  const sidebarTheme = ref<SidebarThemeMode>(loadSavedTheme())
  const sidebarCollapsed = ref<boolean>(false)
  const activeTaskId = ref<number | null>(null)

  function applyDomTheme(mode: SidebarThemeMode): void {
    if (typeof document !== 'undefined') {
      document.documentElement.setAttribute('data-sidebar-theme', mode)
    }
  }

  function setSidebarTheme(mode: SidebarThemeMode): void {
    sidebarTheme.value = mode
    try {
      localStorage.setItem(THEME_STORAGE_KEY, mode)
    } catch {
      // ignore
    }
    applyDomTheme(mode)
  }

  function setActiveTaskId(id: number | null): void {
    activeTaskId.value = id
  }

  applyDomTheme(sidebarTheme.value)

  watch(sidebarTheme, (val) => {
    applyDomTheme(val)
  })

  return {
    sidebarTheme,
    sidebarCollapsed,
    activeTaskId,
    setSidebarTheme,
    setActiveTaskId,
  }
})