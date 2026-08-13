const STORAGE_KEY = 'writing-desk-state'

export function readWritingDeskState(storage = localStorage) {
  try {
    const saved = JSON.parse(storage.getItem(STORAGE_KEY) || '{}')
    const selectedThesis = saved.selectedThesis ?? null
    const drafts = saved.drafts && typeof saved.drafts === 'object'
      ? saved.drafts
      : selectedThesis && typeof saved.paragraph === 'string'
        ? { [selectedThesis]: saved.paragraph }
        : {}
    return { selectedThesis, drafts }
  } catch {
    return { selectedThesis: null, drafts: {} }
  }
}

export function writeWritingDeskState(storage, state) {
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(state))
    return true
  } catch {
    return false
  }
}
