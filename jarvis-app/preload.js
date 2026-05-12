const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('jarvis', {
  close:           () => ipcRenderer.send('window-close'),
  minimize:        () => ipcRenderer.send('window-minimize'),
  quit:            () => ipcRenderer.send('quit-app'),
  getPort:         () => ipcRenderer.invoke('get-port'),
  openCameraPrefs: () => ipcRenderer.send('open-camera-prefs'),
  enterCamMode:    () => ipcRenderer.send('cam-mode-on'),
  exitCamMode:     () => ipcRenderer.send('cam-mode-off'),
  // First-run wizard:
  relaunch:        () => ipcRenderer.send('app-relaunch'),
  openExternal:    (url) => ipcRenderer.send('open-external', url),
  // Support / observability:
  getVersion:      () => ipcRenderer.invoke('get-version'),
  exportLogs:      () => ipcRenderer.invoke('export-logs'),
})
