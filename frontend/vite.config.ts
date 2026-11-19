import { fileURLToPath, URL } from 'node:url'
import { existsSync } from 'node:fs'
import { defineConfig, type Plugin } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'

function elPlusKebab(name: string): string | null {
  if (!/^El[A-Z]/.test(name)) return null
  if (name === 'ElLoadingDirective') return null
  return name
    .slice(2)
    .replace(/([A-Z])/g, '-$1')
    .toLowerCase()
    .replace(/^-/, '')
}

const EL_PLUS_PARENT_FALLBACK: Record<string, string> = {
  'dropdown-item': 'dropdown',
  'dropdown-menu': 'dropdown',
  'menu-item': 'menu',
  'menu-group': 'menu',
  option: 'select',
  'option-group': 'select',
  'radio-button': 'radio',
  'radio-group': 'radio',
  'checkbox-button': 'checkbox',
  'checkbox-group': 'checkbox',
  'skeleton-item': 'skeleton',
  'table-column': 'table',
  'timeline-item': 'timeline',
  'carousel-item': 'carousel',
  'tab-pane': 'tabs',
  'breadcrumb-item': 'breadcrumb',
  'form-item': 'form',
  'collapse-item': 'collapse',
  'descriptions-item': 'descriptions',
}

const COMPONENTS_DIR = fileURLToPath(
  new URL('./node_modules/element-plus/es/components', import.meta.url),
)

function elPlusComponentInfo(
  name: string,
): { name: string; from: string; sideEffects: string } | undefined {
  const kebab = elPlusKebab(name)
  if (!kebab) return undefined
  let target = kebab
  if (!existsSync(`${COMPONENTS_DIR}/${kebab}/index.mjs`)) {
    const parent = EL_PLUS_PARENT_FALLBACK[kebab]
    if (!parent) {
      throw new Error(`[elPlusResolver] 未登记的嵌套组件：${name}（components/${kebab}/index.mjs 不存在）`)
    }
    target = parent
  }
  return {
    name,
    from: `element-plus/es/components/${target}/index`,
    sideEffects: `element-plus/es/components/${kebab}/style/css`,
  }
}

export default defineConfig({
  plugins: [
    vue(),
    AutoImport({
      resolvers: [
        (name: string) => {
          if (name === 'ElMessage') {
            return { name: 'ElMessage', from: 'element-plus/es/components/message/index' }
          }
          if (name === 'ElMessageBox') {
            return { name: 'ElMessageBox', from: 'element-plus/es/components/message-box/index' }
          }
          if (name === 'ElNotification') {
            return { name: 'ElNotification', from: 'element-plus/es/components/notification/index' }
          }
          return undefined
        },
      ],
      dts: 'src/auto-imports.d.ts',
    }),
    Components({
      resolvers: [(name: string) => elPlusComponentInfo(name)],
      dts: 'src/components.d.ts',
    }) as Plugin,
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('element-plus')) return 'vendor-element-plus'
            if (id.includes('echarts')) return 'vendor-echarts'
            if (id.includes('vue') || id.includes('pinia') || id.includes('vue-router')) {
              return 'vendor-vue-core'
            }
            if (id.includes('axios')) return 'vendor-axios'
            return 'vendor-libs'
          }
        },
      },
    },
  },
})