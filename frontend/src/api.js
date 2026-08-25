import axios from 'axios'
import { message } from 'antd'
import { apiFetch, applyApiRequestConfig } from './utils/apiUrl'

const api = axios.create({
  timeout: 30000,
})

api.interceptors.request.use(applyApiRequestConfig)

// 统一错误处理：后端返回的 detail 优先展示
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.config?.skipGlobalError) {
      return Promise.reject(error)
    }
    const detail = error.response?.data?.detail || error.message || '请求失败'
    if (detail && detail !== 'canceled') {
      message.error(String(detail))
    }
    return Promise.reject(error)
  }
)

export default api

// Papers
export const importPapers = (files) => {
  const form = new FormData()
  files.forEach((file) => form.append('files', file))
  return api.post('/papers/import', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}

export const listPapers = (params) => api.get('/papers', { params })
export const getPaperStats = () => api.get('/papers/stats/overview')
export const getBenchmarkV2Readiness = () =>
  api.get('/readiness/benchmark-v2', { skipGlobalError: true })
export const getPaper = (id) => api.get(`/papers/${id}`)
export const updatePaper = (id, data) => api.put(`/papers/${id}`, data)
export const deletePaper = (id) => api.delete(`/papers/${id}`)
export const batchDeletePapers = (ids) => api.post('/papers/batch/delete', { ids })
export const batchUpdateStatus = (ids, status) => api.post('/papers/batch/status', { ids, status })
export const batchUpdateTags = (ids, tagNames, action = 'add') =>
  api.post('/papers/batch/tags', { ids, tag_names: tagNames, action })
export const processPaper = (id) => api.post(`/papers/${id}/process`)
export const summarizePaper = (id, config) =>
  api.post(`/papers/${id}/summarize`, null, { ...config, timeout: 180000 })
export const getPaperSummary = (id) => api.get(`/papers/${id}/summary`)
export const getReadProgress = (id) => api.get(`/papers/${id}/read-progress`)
export const updateReadProgress = (id, page) =>
  api.put(`/papers/${id}/read-progress?page=${page}`)
export const listAnnotations = (id) => api.get(`/papers/${id}/annotations`)
export const createAnnotation = (id, data) => api.post(`/papers/${id}/annotations`, data)
export const deleteAnnotation = (id, annotationId) =>
  api.delete(`/papers/${id}/annotations/${annotationId}`)
export const extractMetadata = (id) => api.post(`/papers/${id}/extract-metadata`)
export const getPaperNote = (id) => api.get(`/papers/${id}/note`)
export const savePaperNote = (id, content) =>
  api.post(`/papers/${id}/note`, new URLSearchParams({ content }))

// Tags
export const listTags = () => api.get('/papers/tags/all')
export const addTag = (paperId, tagName) =>
  api.post(`/papers/${paperId}/tags`, new URLSearchParams({ tag_name: tagName }))
export const removeTag = (paperId, tagId) => api.delete(`/papers/${paperId}/tags/${tagId}`)
export const updateTag = (tagId, data) => api.put(`/papers/tags/${tagId}`, data)

// Search
export const searchPapers = (data) => api.post('/search', data)

// Chat
export const listConversations = () => api.get('/chat/conversations')
export const createConversation = () => api.post('/chat/conversations')
export const deleteConversation = (id) => api.delete(`/chat/conversations/${id}`)
export const getHistory = (id) => api.get(`/chat/conversations/${id}/history`)
export const deleteMessagesFrom = (conversationId, messageId) =>
  api.delete(`/chat/conversations/${conversationId}/messages/${messageId}`)
export const regenerateMessage = (conversationId, messageId, { signal } = {}) =>
  apiFetch(`/api/chat/conversations/${conversationId}/messages/${messageId}/regenerate`, {
    method: 'POST',
    signal,
  })
export const sendChatMessage = (data) =>
  apiFetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
export const listSkills = () => api.get('/chat/skills')
export const analyzeImage = (file, question, { signal } = {}) => {
  const form = new FormData()
  form.append('file', file)
  form.append('question', question || '')
  return apiFetch('/api/chat/analyze-image', {
    method: 'POST',
    body: form,
    signal,
  })
}

// Thesis
export const uploadThesis = (file) => {
  const form = new FormData()
  form.append('file', file)
  return api.post('/thesis/upload', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}
export const listThesis = () => api.get('/thesis')
export const getThesis = (id) => api.get(`/thesis/${id}`)
export const deleteThesis = (id) => api.delete(`/thesis/${id}`)
export const getThesisCitations = (id) => api.get(`/thesis/${id}/citations`)
export const getThesisCitationMap = (id) => api.get(`/thesis/${id}/citation-map`)
export const updateThesisCitation = (thesisId, citationId, data) =>
  api.put(`/thesis/${thesisId}/citations/${citationId}`, data)
export const getChapterText = (id, chapterIndex) =>
  api.get(`/thesis/${id}/chapters/${chapterIndex}/text`)
export const analyzeThesis = (id, data) => api.post(`/thesis/${id}/analyze`, data)
export const suggestCitations = (id, paragraph, config = {}) =>
  api.post(`/thesis/${id}/suggest-citations`, { paragraph }, config)

// Export
export const exportPapersCSV = () => api.get('/export/papers/csv', { responseType: 'blob' })
export const exportPapersExcel = () => api.get('/export/papers/excel', { responseType: 'blob' })
export const exportPapersBib = (format = 'GB/T 7714') =>
  api.get(`/export/papers/bib?format=${encodeURIComponent(format)}`, { responseType: 'blob' })
export const exportBackup = () =>
  api.post('/export/backup', {}, { responseType: 'blob', timeout: 120000 })
export const triggerAutoBackup = () => api.post('/export/backup/auto')

// Memory
export const listMemories = (type) => api.get('/memory/memories', { params: { memory_type: type } })
export const addMemory = (content, type = 'fact', importance = 5) =>
  api.post('/memory/memories', null, { params: { content, memory_type: type, importance } })
export const deleteMemory = (id) => api.delete(`/memory/memories/${id}`)

// Settings
export const getSettings = () => api.get('/settings')
export const updateSettings = (data) => api.put('/settings', data)
