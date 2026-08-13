function filenameFromUrl(rawUrl) {
  try {
    const pathname = new URL(rawUrl, window.location.href).pathname
    return decodeURIComponent(pathname.split('/').pop()) || 'document.pdf'
  } catch {
    return 'document.pdf'
  }
}


export async function downloadUrl(rawUrl, {
  fetchImpl = fetch,
  documentRef = document,
  urlApi = URL,
  headers,
} = {}) {
  const response = await fetchImpl(rawUrl, { credentials: 'omit', headers })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)

  const objectUrl = urlApi.createObjectURL(await response.blob())
  const link = documentRef.createElement('a')
  link.href = objectUrl
  link.download = filenameFromUrl(rawUrl)
  link.style = 'display: none'
  documentRef.body.appendChild(link)
  try {
    link.click()
  } finally {
    link.remove()
    urlApi.revokeObjectURL(objectUrl)
  }
}
