import http from './http'

export function parseContentDisposition(header?: string | null, fallbackName = 'download.docx'): string {
  if (!header) return fallbackName

  const utf8Match = header.match(/filename\*\s*=\s*UTF-8''([^;\r\n]+)/i)
  if (utf8Match && utf8Match[1]) {
    try {
      return decodeURIComponent(utf8Match[1].trim())
    } catch {
      return utf8Match[1].trim()
    }
  }

  const plainMatch = header.match(/filename\s*=\s*"?([^";\r\n]+)"?/i)
  if (plainMatch && plainMatch[1]) {
    try {
      return decodeURIComponent(plainMatch[1].trim())
    } catch {
      return plainMatch[1].trim()
    }
  }

  return fallbackName
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = window.URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.style.display = 'none'
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  window.setTimeout(() => {
    window.URL.revokeObjectURL(url)
  }, 1000)
}

export async function downloadFromApi(apiPath: string, fallbackName: string): Promise<string> {
  const cleanPath = apiPath.startsWith('/api/') ? apiPath.slice(4) : apiPath
  const response = await http.get<Blob>(cleanPath, { responseType: 'blob' })
  const cd = response.headers['content-disposition'] as string | undefined
  const filename = parseContentDisposition(cd, fallbackName)
  downloadBlob(response.data, filename)
  return filename
}