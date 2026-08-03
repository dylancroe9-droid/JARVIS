const { contextBridge, ipcRenderer } = require('electron')

// Product identity, resolved synchronously at preload so every renderer script
// (branding.js, app.js) can read window.PRODUCT before it runs. Falls back to
// JARVIS if the main process is somehow unavailable.
let _product = { key: 'jarvis', name: 'JARVIS', tagline: "Dylan's assistant.", accent: '#00d4ff' }
try { _product = ipcRenderer.sendSync('get-product-sync') || _product } catch (_) {}
contextBridge.exposeInMainWorld('PRODUCT', _product)

contextBridge.exposeInMainWorld('jarvis', {
  close:           () => ipcRenderer.send('window-close'),
  hide:            () => ipcRenderer.send('window-hide'),
  minimize:        () => ipcRenderer.send('window-minimize'),
  quit:            () => ipcRenderer.send('quit-app'),
  getPort:         () => ipcRenderer.invoke('get-port'),
  openCameraPrefs: () => ipcRenderer.send('open-camera-prefs'),
  enterCamMode:    () => ipcRenderer.send('cam-mode-on'),
  exitCamMode:     () => ipcRenderer.send('cam-mode-off'),
  // First-run wizard:
  relaunch:        () => ipcRenderer.send('app-relaunch'),
  openExternal:    (url) => ipcRenderer.send('open-external', url),
  // Display mode (work / desktop)
  setDisplayMode:  (mode) => ipcRenderer.send('set-display-mode', mode),
  // Support / observability:
  getVersion:      () => ipcRenderer.invoke('get-version'),
  exportLogs:      () => ipcRenderer.invoke('export-logs'),
  // Product identity (JARVIS vs Alex) for renderer branding:
  getProduct:      () => ipcRenderer.invoke('get-product'),
})
