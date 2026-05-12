const { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage, shell, globalShortcut, screen, systemPreferences, session } = require('electron')
const { spawn }  = require('child_process')
const path       = require('path')
const net        = require('net')
const fs         = require('fs')

const PORT       = 8765
const JARVIS_DIR = path.join(__dirname, '..')

let mainWindow   = null
let tray         = null
let pyProcess    = null

// ── Spawn Python server ───────────────────────────────────────────────────────

function startServer () {
  const python = path.join(JARVIS_DIR, '.venv', 'bin', 'python')
  const script = path.join(JARVIS_DIR, 'server.py')

  if (!fs.existsSync(python)) {
    console.error('Python venv not found at', python)
    return
  }

  pyProcess = spawn(python, [script], {
    cwd:   JARVIS_DIR,
    stdio: ['ignore', 'pipe', 'pipe'],
    env:   { ...process.env, TK_SILENCE_DEPRECATION: '1' },
  })

  pyProcess.stdout.on('data', d => { process.stdout.write('[py] ' + d); _appendLog(d.toString()) })
  pyProcess.stderr.on('data', d => { process.stderr.write('[py] ' + d); _appendLog(d.toString()) })
  pyProcess.on('exit', code => { console.log('[py] exited:', code); _appendLog(`\n[py exited: ${code}]\n`) })
}

// ── Wait until Python server accepts connections ──────────────────────────────

function waitForPort (port, attempts = 40) {
  return new Promise((resolve, reject) => {
    const try_ = n => {
      const sock = new net.Socket()
      sock.setTimeout(400)
      const fail = () => { sock.destroy(); n > 0 ? setTimeout(() => try_(n - 1), 400) : reject() }
      sock.on('connect', () => { sock.destroy(); resolve() })
      sock.on('timeout', fail)
      sock.on('error',   fail)
      sock.connect(port, '127.0.0.1')
    }
    try_(attempts)
  })
}

// ── Create main window ────────────────────────────────────────────────────────

function createWindow () {
  const { workAreaSize } = screen.getPrimaryDisplay()

  mainWindow = new BrowserWindow({
    width:           460,
    height:          860,
    minWidth:        380,
    minHeight:       600,
    x:               workAreaSize.width - 478,   // top-right corner
    y:               20,
    frame:           false,
    transparent:     true,
    vibrancy:        'under-window',
    visualEffectState: 'active',
    alwaysOnTop:     true,
    resizable:       true,
    maximizable:     true,
    fullscreenable:  true,
    hasShadow:       true,
    webPreferences: {
      preload:          path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration:  false,
    },
  })

  // Visible on all spaces / Stage Manager groups
  mainWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true })
  mainWindow.setAlwaysOnTop(true, 'floating')

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'))

  // DevTools: only open in dev mode (run with JARVIS_DEV=1 to enable)
  if (process.env.JARVIS_DEV === '1') {
    mainWindow.webContents.openDevTools({ mode: 'detach' })
  }

  mainWindow.on('closed', () => { mainWindow = null })
}

// ── System tray ───────────────────────────────────────────────────────────────

function createTray () {
  // 16×16 template image (white, macOS tints it automatically)
  const icon = nativeImage.createFromDataURL(
    'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAABHNCSVQICAgIfAhkiAAAAAlwSFlzAAAAdgAAAHYBTnsmCAAAABl0RVh0U29mdHdhcmUAd3d3Lmlua3NjYXBlLm9yZ5vuPBoAAAEUSURBVDiNpZIxSgNBFIa/N7tJdrOuQewFtLCwEDyAhZ1H8AZe' +
    'QMRKsLCwEMHCwkKwsBEECwsLC8HGwsLCQrCwsBAsLCwECwsLCwsLCw=='
  )
  tray = new Tray(icon)
  const menu = Menu.buildFromTemplate([
    { label: 'Show JARVIS',  click: () => mainWindow?.show() },
    { label: 'Hide',         click: () => mainWindow?.hide() },
    { type: 'separator' },
    { label: 'Quit JARVIS',  click: () => app.quit() },
  ])
  tray.setToolTip('JARVIS')
  tray.setContextMenu(menu)
  tray.on('click', () => {
    if (mainWindow) {
      mainWindow.isVisible() ? mainWindow.hide() : mainWindow.show()
    }
  })
}

// ── IPC from renderer ─────────────────────────────────────────────────────────

ipcMain.on('window-close',    () => mainWindow?.hide())
ipcMain.on('window-minimize', () => mainWindow?.minimize())
ipcMain.on('quit-app',        () => app.quit())
ipcMain.handle('get-port',    () => PORT)
ipcMain.handle('get-version', () => app.getVersion())

// Last N kB of stdout/stderr from the Python child — kept in a ring buffer
// so the user can hit "Send Logs" and we have something to give support.
const LOG_BUFFER_BYTES = 64 * 1024
let _logBuffer = ''
function _appendLog (chunk) {
  _logBuffer += chunk
  if (_logBuffer.length > LOG_BUFFER_BYTES) {
    _logBuffer = _logBuffer.slice(-LOG_BUFFER_BYTES)
  }
}

ipcMain.handle('export-logs', async () => {
  const dest = path.join(require('os').homedir(), 'Desktop', `jarvis-logs-${Date.now()}.txt`)
  try {
    const header = `JARVIS ${app.getVersion()} log export\nGenerated: ${new Date().toISOString()}\n${'─'.repeat(60)}\n`
    fs.writeFileSync(dest, header + (_logBuffer || '(no log output captured yet)\n'))
    shell.showItemInFolder(dest)
    return { ok: true, path: dest }
  } catch (e) {
    return { ok: false, error: String(e) }
  }
})
ipcMain.on('open-camera-prefs', () => {
  shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_Camera')
})
ipcMain.on('open-external', (_e, url) => {
  if (typeof url === 'string' && /^https?:\/\//i.test(url)) shell.openExternal(url)
})
ipcMain.on('app-relaunch', () => {
  // Kill the Python server before relaunch so the new process can bind 8765.
  if (pyProcess) { try { pyProcess.kill('SIGTERM') } catch (_) {} }
  app.relaunch()
  app.exit(0)
})

// ── Camera mode — resize window to fill screen / restore ──────────────────────
let _normalBounds = null

ipcMain.on('cam-mode-on', () => {
  if (!mainWindow) return
  _normalBounds = mainWindow.getBounds()
  const { workArea } = screen.getPrimaryDisplay()
  mainWindow.setBounds({
    x:      workArea.x,
    y:      workArea.y,
    width:  workArea.width,
    height: workArea.height,
  }, true)  // animate
})

ipcMain.on('cam-mode-off', () => {
  if (!mainWindow) return
  if (_normalBounds) {
    mainWindow.setBounds(_normalBounds, true)
    _normalBounds = null
  }
})

// ── App lifecycle ─────────────────────────────────────────────────────────────

app.whenReady().then(async () => {
  // Request microphone access before spawning Python — without this macOS
  // silently blocks PortAudio/sounddevice in the child process on first run
  if (process.platform === 'darwin') {
    // Microphone
    const micStatus = systemPreferences.getMediaAccessStatus('microphone')
    console.log('[mic] status:', micStatus)
    if (micStatus !== 'granted') {
      const granted = await systemPreferences.askForMediaAccess('microphone')
      console.log('[mic] permission granted:', granted)
    }

    // Camera (needed for HUD live feed + camera vision tool)
    const camStatus = systemPreferences.getMediaAccessStatus('camera')
    console.log('[cam] status:', camStatus)
    if (camStatus === 'not-determined') {
      const granted = await systemPreferences.askForMediaAccess('camera')
      console.log('[cam] permission granted:', granted)
      if (!granted) {
        console.warn('[cam] DENIED — open System Settings → Privacy & Security → Camera → enable Electron/JARVIS')
      }
    } else if (camStatus === 'denied') {
      // macOS won't re-prompt for denied apps — user must re-enable in System Settings
      console.warn('[cam] DENIED — open System Settings → Privacy & Security → Camera → enable Electron/JARVIS')
    } else if (camStatus === 'granted') {
      console.log('[cam] already granted ✓')
    }
  }

  // Allow camera + mic access from the renderer process (getUserMedia)
  session.defaultSession.setPermissionRequestHandler((webContents, permission, callback) => {
    const allowed = ['media', 'camera', 'microphone'].includes(permission)
    callback(allowed)
  })

  // Only start the Python server if nothing is already listening on the port.
  // (start.sh starts it first; we just adopt that process instead of spawning a second.)
  const alreadyUp = await waitForPort(PORT, 1).then(() => true).catch(() => false)
  if (alreadyUp) {
    console.log('Python server already running on port', PORT, '— skipping spawn')
  } else {
    startServer()
    try {
      await waitForPort(PORT)
      console.log('Python server ready on port', PORT)
    } catch {
      console.error('Python server failed to start — loading anyway')
    }
  }

  createWindow()
  createTray()

  // ⌘⇧J toggles the window from anywhere
  globalShortcut.register('CommandOrControl+Shift+J', () => {
    if (!mainWindow) return createWindow()
    if (mainWindow.isVisible() && mainWindow.isFocused()) {
      mainWindow.hide()
    } else {
      mainWindow.show()
      mainWindow.focus()
    }
  })

  app.on('activate', () => {
    if (!mainWindow) createWindow()
    else mainWindow.show()
  })
})

app.on('window-all-closed', () => {
  // Keep running in tray on macOS
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', () => {
  globalShortcut.unregisterAll()
  if (pyProcess) pyProcess.kill('SIGTERM')
})
