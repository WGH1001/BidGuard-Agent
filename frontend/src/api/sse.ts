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
 * 内置指数退避重连（1s -> 2s -> 4s -> 8s，上限 4 次），防止长耗时任务连接断开触发 0ms 死循环重连。
 * 返回手动中止订阅函数 () => void。
 */
export function subscribeCheckStream(taskId: number, handlers: SSEHandlers): () => void {
  const controller = new AbortController()
  let stopped = false

  const sleep = (ms: number) =>
    new Promise<void>((resolve) => {
      const timer = window.setTimeout(resolve, ms)
      controller.signal.addEventListener('abort', () => window.clearTimeout(timer), { once: true })
    })

  void (async () => {
    const maxRetries = 4
    let attempt = 0

    while (!stopped && !controller.signal.aborted && attempt <= maxRetries) {
      if (attempt > 0) {
        const delayMs = Math.min(1000 * Math.pow(2, attempt - 1), 8000)
        await sleep(delayMs)
        if (stopped || controller.signal.aborted) return
      }

      try {
        const token = getStoredToken()
        const response = await fetch(`/api/checks/${taskId}/stream`, {
          method: 'GET',
          headers: {
            Accept: 'text/event-stream',
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          signal: controller.signal,
        })

        if (!response.ok || !response.body) {
          if (response.status === 401 || response.status === 403 || response.status === 404) {
            stopped = true
            handlers.onError?.(new Error(`SSE 订阅失败 (HTTP ${response.status})`))
            return
          }
          attempt += 1
          if (attempt > maxRetries) {
            handlers.onError?.(new Error(`SSE 订阅失败 (HTTP ${response.status})`))
            return
          }
          continue
        }

        const reader = response.body.getReader()
        const decoder = new TextDecoder('utf-8')
        let buffer = ''

        while (!stopped && !controller.signal.aborted) {
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
                stopped = true
                handlers.onDone?.(parsed)
                controller.abort()
                return
              }
              if (eventName === 'error' || parsed.status === 'failed') {
                stopped = true
                handlers.onError?.(parsed)
                controller.abort()
                return
              }
              attempt = 0
              handlers.onProgress?.(parsed)
            } catch {
              // 忽略非 JSON keep-alive 帧
            }
          }
        }

        // 流提前结束但尚未收到终态帧，退避重连
        attempt += 1
        if (attempt > maxRetries && !stopped && !controller.signal.aborted) {
          handlers.onError?.(new Error('SSE 进度流连接已断开，请刷新任务查看最新状态'))
          return
        }
      } catch (err: unknown) {
        if (stopped || (err as { name?: string })?.name === 'AbortError') return
        attempt += 1
        if (attempt > maxRetries) {
          handlers.onError?.(err instanceof Error ? err : new Error(String(err)))
          return
        }
      }
    }
  })()

  return () => {
    stopped = true
    controller.abort()
  }
}