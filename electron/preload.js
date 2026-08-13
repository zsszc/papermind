const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electronAPI', {
  platform: process.platform,
  isElectron: true,
  getRuntimeConfig: () => ipcRenderer.invoke('papermind:get-runtime-config'),
})
