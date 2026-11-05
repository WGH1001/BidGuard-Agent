import { getStoredToken } from './http'
import type { SSEProgressFrame } from './types'

export interface SSEHandlers {
  onProgress?: (frame: SSEProgressFrame) => void
  onDone?: (frame: SSEProgressFrame) => void
  onError?: (frame: SSEProgressFrame | Error) => void
}

/**
 * 基于 fetch + ReadableStream 订阅 GET /api/checks/{taskId}/stream
 * 解决原生 EventSource 无法携带 Authorization: Bearer <token> 请求头的问题。
 * 返回手动中止订阅函数 () => void。
 */
export function subscribeCheckStream(taskId: number, handlers: SSEHandlers): () => void {
  const controller = new AbortController()
  const token = getStoredToken()

  void (async () => {
    try {
      const response = await fetch(`/api/checks/${taskId}/stream`, {
        method: 'GET',
        headers: {
          Accept: 'text/event-stream',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        signal: controller.signal,
      })

      if (!response.ok || !response.body) {
        handlers.onError?.(new Error(`SSE 订阅失败 (HTTP ${response.status})`))
        return
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        const blocks = buffer.split('\n\n')
        buffer = blocks.pop() || ''

        for (const block of blocks) {
          const lines = block.split('\n')
          let eventName = 'progress'
          let dataStr = ''

          for (const line of lines) {
            if (line.startsWith('event:')) {
              eventName = line.slice(6).trim()
            } else if (line.startsWith('data:')) {
              dataStr += line.slice(5).trim()
            }
          }

          if (!dataStr) continue
          try {
            const parsed = JSON.parse(dataStr) as SSEProgressFrame
            if (eventName === 'done' || parsed.status === 'delivered') {
              handlers.onDone?.(parsed)
              controller.abort()
              return
            }
            if (eventName === 'error' || parsed.status === 'failed') {
              handlers.onError?.(parsed)
              controller.abort()
              return
            }
            handlers.onProgress?.(parsed)
          } catch {
            // 忽略非 JSON keep-alive 帧
          }
        }
      }
    } catch (err: unknown) {
      if ((err as { name?: string })?.name === 'AbortError') return
      handlers.onError?.(err instanceof Error ? err : new Error(String(err)))
    }
  })()

  return () => {
    controller.abort()
  }
}