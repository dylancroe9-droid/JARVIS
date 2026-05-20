/* ── JARVIS Renderer — HUD Edition ─────────────────────────────────────────── */

const PORT = 8765
let ws        = null
let micMuted  = false
let currentRow = null   // the .msg row being streamed into
let sessionStart = Date.now()

// AR overlay + gesture state — declared here so hudTick() and initCamera()
// can safely reference them at boot (avoids Temporal Dead Zone).
let arOverlay    = null
let mpReady      = false   // true when MediaPipe Hands is ready

// ─── State map ───────────────────────────────────────────────────────────────

let jarvisState = 'idle'

const STATE_MAP = {
  idle:      { label: 'STANDBY',      strip: 'J.A.R.V.I.S — STANDBY',          hud: '#1e4a66' },
  thinking:  { label: 'PROCESSING',   strip: 'COGNITIVE ENGINE: ACTIVE',         hud: '#cc8800' },
  speaking:  { label: 'TRANSMITTING', strip: 'AUDIO OUTPUT: ACTIVE',             hud: '#00d4ff' },
  listening: { label: 'LISTENING',    strip: 'VOICE RECOGNITION: ACTIVE',        hud: '#00cc70' },
  studying:  { label: 'STUDYING',     strip: 'STUDY MODE: CAPTURING',            hud: '#8b7ae8' },
}

// ─── DOM refs ────────────────────────────────────────────────────────────────

const $app     = document.getElementById('app')
const $dot     = document.getElementById('status-dot')
const $label   = document.getElementById('status-label')
const $strip   = document.getElementById('strip-center')
const $chat    = document.getElementById('chat')
const $empty   = document.getElementById('empty-state')
const $input   = document.getElementById('text-input')
const $send    = document.getElementById('btn-send')
const $stop    = document.getElementById('stop-btn')
const $mic     = document.getElementById('mic-btn')
const $close   = document.getElementById('btn-close')
const $min     = document.getElementById('btn-min')
const $clear   = document.getElementById('btn-clear')
const $confirmOverlay = document.getElementById('confirm-overlay')
const $confirmMsg     = document.getElementById('confirm-msg')
const $confirmAllow   = document.getElementById('confirm-allow')
const $confirmDeny    = document.getElementById('confirm-deny')
const $nowPlaying     = document.getElementById('now-playing')
const $npTitle        = document.getElementById('np-title')
const $studyBtn       = document.getElementById('study-btn')
const $studyBar       = document.getElementById('study-bar')
const $studyCount     = document.getElementById('study-count')
const $studySummarizeBtn = document.getElementById('study-summarize-btn')

// ─── Homework auto-answer loop bar ───────────────────────────────────────────
const $hwBar          = document.getElementById('hw-bar')
const $hwQnum         = document.getElementById('hw-qnum')
const $hwAnswer       = document.getElementById('hw-answer')
const $hwStopBtn      = document.getElementById('hw-stop-btn')
const $clock       = document.getElementById('clock')
const $btnCamMode  = document.getElementById('btn-cam-mode')
const $camModeExit = document.getElementById('cam-mode-exit')

// ─── HUD side panel DOM refs ──────────────────────────────────────────────────
const $hudClockBig  = document.getElementById('hud-clock-big')
const $hudDateSub   = document.getElementById('hud-date-sub')
const $hudUptimeVal = document.getElementById('hud-uptime-val')
const $hudStateVal  = document.getElementById('hud-state-val')
const $hudStateIcon = document.getElementById('hud-state-icon')
const $hudStateSub  = document.getElementById('hud-state-sub')
const $hudLinkVal   = document.getElementById('hud-link-val')

// ─── Right sidebar clock ──────────────────────────────────────────────────────
const $srTime   = document.getElementById('sr-time')
const $srDate   = document.getElementById('sr-date')
function updateSidebarClock() {
  const now  = new Date()
  const h    = now.getHours()
  const m    = String(now.getMinutes()).padStart(2, '0')
  const ampm = h >= 12 ? 'PM' : 'AM'
  const h12  = h % 12 || 12
  if ($srTime) $srTime.textContent = `${h12}:${m} ${ampm}`
  if ($srDate) {
    const days   = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday']
    const months = ['January','February','March','April','May','June','July','August','September','October','November','December']
    $srDate.textContent = `${days[now.getDay()]}, ${months[now.getMonth()]} ${now.getDate()}`
  }
}
setInterval(updateSidebarClock, 1000)
updateSidebarClock()

// ─── Left sidebar state sync ──────────────────────────────────────────────────
const $slStatusDot   = document.getElementById('sl-status-dot')
const $slStatusLabel = document.getElementById('sl-status-label')
const $slStatusSub   = document.getElementById('sl-status-sublabel')
const $slValListen   = document.getElementById('sl-val-listen')

// Session uptime
let _sessionStart = Date.now()
const $slValUptime   = document.getElementById('sl-val-uptime')
const $slSessionTime = document.getElementById('sl-session-time')
setInterval(() => {
  const elapsed = Math.floor((Date.now() - _sessionStart) / 1000)
  const mins = Math.floor(elapsed / 60)
  const secs = elapsed % 60
  const h    = Math.floor(mins / 60)
  const m    = mins % 60
  const disp = h > 0 ? `${h}h ${m}m` : `${m}m`
  if ($slValUptime) $slValUptime.textContent = disp
  if ($slSessionTime) $slSessionTime.textContent = `SESSION: ${mins}m ${secs}s`
}, 1000)

// Sync left sidebar dot with JARVIS state
function updateSidebarState(state) {
  if (!$slStatusDot) return
  $slStatusDot.className = state
  const labels = {
    idle:      ['STANDBY',   'Voice system active'],
    thinking:  ['THINKING',  'Processing request…'],
    speaking:  ['SPEAKING',  'Generating response…'],
    listening: ['LISTENING', 'Capturing audio…'],
    studying:  ['STUDY MODE','Capturing lecture…'],
  }
  const [lbl, sub] = labels[state] || ['STANDBY', 'Voice system active']
  if ($slStatusLabel) $slStatusLabel.textContent = lbl
  if ($slStatusSub)   $slStatusSub.textContent   = sub
}

// ─── Focus mode ───────────────────────────────────────────────────────────────
const $focusBanner  = document.getElementById('focus-mode-banner')
const $slFocusBtn   = document.getElementById('sl-focus-btn')
let _focusModeActive = false

function toggleFocusMode() {
  _focusModeActive = !_focusModeActive
  setFocusMode(_focusModeActive)
  send({ type: 'text', text: _focusModeActive ? 'presentation mode' : 'conversation mode' })
}

function setFocusMode(active) {
  _focusModeActive = active
  if ($focusBanner) $focusBanner.classList.toggle('visible', active)
  if ($slFocusBtn) {
    $slFocusBtn.classList.toggle('active', active)
    $slFocusBtn.innerHTML = `<span id="sl-focus-dot"></span>${active ? 'FOCUS MODE ON' : 'FOCUS MODE OFF'}`
  }
  const $slVal = document.getElementById('sl-val-listen')
  if ($slVal) $slVal.textContent = active ? 'FOCUS' : 'ALWAYS ON'
  document.getElementById('app').classList.toggle('focus-active', active)
}

// ─── Weather widget ───────────────────────────────────────────────────────────
function updateWeatherWidget (dataOrText) {
  const $temp  = document.getElementById('sr-weather-temp')
  const $desc  = document.getElementById('sr-weather-desc')
  const $icon  = document.getElementById('sr-weather-icon')
  const $loc   = document.getElementById('sr-weather-loc')

  if (dataOrText && typeof dataOrText === 'object') {
    // Structured message from server weather fetch
    const { temp_f, desc, icon, city, region, wind, humidity } = dataOrText
    if ($temp) $temp.textContent = temp_f != null ? `${temp_f}°F` : '—'
    if ($desc) {
      let detail = desc || 'Unknown'
      if (wind)     detail += ` · ${wind} mph`
      if (humidity) detail += ` · ${humidity}% humidity`
      $desc.textContent = detail
    }
    if ($icon) $icon.textContent = icon || '🌡️'
    if ($loc)  $loc.textContent  = (city && region) ? `${city}, ${region}` : ''
  } else if (typeof dataOrText === 'string') {
    // Legacy: parse weather from JARVIS text response
    const tempMatch = dataOrText.match(/(\d+)[°℉]/)
    if ($temp && tempMatch) $temp.textContent = `${tempMatch[1]}°F`
    if ($desc) {
      const conditions = ['sunny','cloudy','rainy','partly','clear','overcast','thunderstorm','snow','fog','windy']
      const found = conditions.find(c => dataOrText.toLowerCase().includes(c))
      if (found) $desc.textContent = found.charAt(0).toUpperCase() + found.slice(1)
      const icons = { sunny:'☀️', cloudy:'☁️', rainy:'🌧️', partly:'⛅', clear:'☀️',
                      overcast:'☁️', thunderstorm:'⛈️', snow:'❄️', fog:'🌫️', windy:'💨' }
      if ($icon && found) $icon.textContent = icons[found] || '🌤'
    }
  }
}

// ─── Active files widget ──────────────────────────────────────────────────────
function updateFilesWidget (files) {
  const $list = document.getElementById('sr-files-list')
  if (!$list) return
  if (!files || files.length === 0) {
    $list.innerHTML = '<div class="sr-files-empty">No recent files.</div>'
    return
  }
  $list.innerHTML = files.map(f => `
    <div class="sr-file-item" title="${escHtml(f.path || f.name)}">
      <span class="sr-file-icon">${escHtml(f.icon || '📄')}</span>
      <span class="sr-file-name">${escHtml(f.name)}</span>
    </div>
  `).join('')
}

// ─── Agenda widget ────────────────────────────────────────────────────────────
function updateAgendaWidget (events) {
  const $list = document.getElementById('sr-agenda-list')
  if (!$list) return
  if (!events || events.length === 0) {
    $list.innerHTML = '<div id="sr-agenda-empty">No events today.</div>'
    return
  }
  $list.innerHTML = events.slice(0, 5).map(ev => `
    <div class="sr-agenda-item">
      <div class="sr-agenda-time">${escHtml(ev.time || '')}</div>
      <div class="sr-agenda-title">${escHtml(ev.title || ev.summary || 'Event')}</div>
    </div>
  `).join('')
}

// ─── Right panel agenda (legacy alias) ───────────────────────────────────────
function updateSidebarAgenda (events) { updateAgendaWidget(events) }

// ─── Clock ───────────────────────────────────────────────────────────────────

const DAY_NAMES  = ['SUN','MON','TUE','WED','THU','FRI','SAT']
const MON_NAMES  = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC']

function updateClock () {
  const d  = new Date()
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  $clock.textContent = `${hh}:${mm}:${ss}`
  // Side panel clock (no seconds — bigger font, clean look)
  if ($hudClockBig) $hudClockBig.textContent = `${hh}:${mm}:${ss}`
  if ($hudDateSub)  $hudDateSub.textContent  =
    `${DAY_NAMES[d.getDay()]}, ${MON_NAMES[d.getMonth()]} ${String(d.getDate()).padStart(2,'0')}`
  // Uptime
  if ($hudUptimeVal) $hudUptimeVal.textContent = formatUptime()
}
updateClock()
setInterval(updateClock, 1000)

// ─── Window controls ─────────────────────────────────────────────────────────

$close.addEventListener('click', () => window.jarvis?.close())
$min.addEventListener('click',   () => window.jarvis?.minimize())

$clear.addEventListener('click', () => {
  Array.from($chat.children).forEach(el => {
    if (el.id !== 'empty-state') el.remove()
  })
  $empty.style.display = ''
  currentRow = null
  send({ type: 'reset' })
})

// ─── Camera mode ─────────────────────────────────────────────────────────────

let camModeActive = false

function enterCamMode () {
  camModeActive = true
  $app.classList.add('cam-mode')
  $btnCamMode && ($btnCamMode.title = 'Exit Camera Mode')
  window.jarvis?.enterCamMode()
  // Give the IPC resize a moment then re-measure canvases
  setTimeout(() => { resizeHud(); resizeGestureCanvas() }, 150)
}

function exitCamMode () {
  camModeActive = false
  $app.classList.remove('cam-mode')
  $btnCamMode && ($btnCamMode.title = 'Camera Mode')
  window.jarvis?.exitCamMode()
  setTimeout(() => { resizeHud(); resizeGestureCanvas() }, 150)
}

$btnCamMode?.addEventListener('click', () => camModeActive ? exitCamMode() : enterCamMode())
$camModeExit?.addEventListener('click', exitCamMode)

// ─── Display mode (work ↔ desktop) ───────────────────────────────────────────

const $btnDisplayMode = document.getElementById('btn-display-mode')
let displayMode = localStorage.getItem('jarvis-display-mode') || 'work'

function setDisplayMode (mode) {
  displayMode = mode
  localStorage.setItem('jarvis-display-mode', mode)

  if (mode === 'desktop') {
    $app.classList.add('desktop-mode')
    if ($btnDisplayMode) {
      $btnDisplayMode.textContent = '◧'
      $btnDisplayMode.title       = 'Work Mode'
      $btnDisplayMode.classList.add('active')
    }
  } else {
    $app.classList.remove('desktop-mode')
    if ($btnDisplayMode) {
      $btnDisplayMode.textContent = '◫'
      $btnDisplayMode.title       = 'Desktop Mode'
      $btnDisplayMode.classList.remove('active')
    }
  }

  window.jarvis?.setDisplayMode(mode)
  // Resize canvases after the window settles
  setTimeout(() => { resizeHud?.(); resizeGestureCanvas?.() }, 350)
}

$btnDisplayMode?.addEventListener('click', () =>
  setDisplayMode(displayMode === 'desktop' ? 'work' : 'desktop')
)

// Restore saved mode on load
if (displayMode === 'desktop') setDisplayMode('desktop')

// ─── Study mode ──────────────────────────────────────────────────────────────

let studyActive     = false
let studyChunkCount = 0

// Short click = open practice panel; long-press (600ms) = start lecture audio capture
let _studyLongPress = null
$studyBtn.addEventListener('mousedown', () => {
  _studyLongPress = setTimeout(() => {
    _studyLongPress = null
    if (studyActive) return          // already capturing
    studyActive     = true
    studyChunkCount = 0
    $studyBtn.classList.add('active')
    $studyBar.classList.add('visible')
    $studyCount.textContent = '0'
    send({ type: 'study_start' })
  }, 600)
})
$studyBtn.addEventListener('mouseup', () => {
  if (_studyLongPress !== null) {
    clearTimeout(_studyLongPress)
    _studyLongPress = null
    // Short click — open practice panel or stop capture if active
    if (studyActive) {
      studyActive     = false
      studyChunkCount = 0
      $studyBtn.classList.remove('active')
      $studyBar.classList.remove('visible')
      send({ type: 'study_stop' })
    } else {
      openPracticePanel()
    }
  }
})
$studyBtn.addEventListener('mouseleave', () => {
  if (_studyLongPress !== null) {
    clearTimeout(_studyLongPress)
    _studyLongPress = null
  }
})

$studySummarizeBtn.addEventListener('click', () => {
  send({ type: 'study_summarize' })
  addMessage('user', "Summarize what I've captured so far")
})

// ─── Homework loop stop button ────────────────────────────────────────────────
$hwStopBtn.addEventListener('click', () => {
  send({ type: 'text', text: 'stop answering' })
  addMessage('user', 'Stop answering')
  $hwBar.classList.remove('visible')
})

// ─── WebSocket ───────────────────────────────────────────────────────────────

// Backend-down banner — shows a clear message + retry CTA when the
// Python server has been unreachable for several consecutive attempts.
// Without this the HUD spins forever silently if server.py died (audit E1).
let _wsRetries = 0
function showBackendDownBanner () {
  let el = document.getElementById('backend-down-banner')
  if (!el) {
    el = document.createElement('div')
    el.id = 'backend-down-banner'
    el.innerHTML = `
      <div class="bdb-shell">
        <div class="bdb-icon">⚠</div>
        <div class="bdb-body">
          <div class="bdb-title">JARVIS server isn't responding.</div>
          <div class="bdb-sub">The Python backend on port 8765 is down — the voice loop and chat won't work until it's back.</div>
        </div>
        <button class="bdb-btn" id="bdb-retry">Retry now</button>
        <button class="bdb-btn ghost" id="bdb-relaunch">Relaunch app</button>
      </div>
    `
    const s = document.createElement('style')
    s.textContent = `
      #backend-down-banner {
        position: fixed; top: 0; left: 0; right: 0; z-index: 99997;
        display: flex; justify-content: center;
        padding: 10px 14px;
        font-family: 'SF Mono', 'Menlo', monospace; font-size: 12px;
        animation: bdb-in 0.25s ease-out;
        pointer-events: none;
      }
      @keyframes bdb-in { from { transform: translateY(-110%); opacity: 0 } to { transform: translateY(0); opacity: 1 } }
      .bdb-shell {
        display: flex; align-items: center; gap: 12px;
        background: linear-gradient(180deg, rgba(40, 8, 0, 0.92), rgba(20, 4, 0, 0.92));
        border: 1px solid rgba(255, 64, 96, 0.55);
        border-radius: 8px;
        padding: 10px 14px;
        box-shadow: 0 8px 28px rgba(0,0,0,0.55);
        max-width: 520px;
        pointer-events: auto;
      }
      .bdb-icon { font-size: 20px; color: #ff7080; flex-shrink: 0; }
      .bdb-body { flex: 1; }
      .bdb-title { color: #ffd0d8; font-weight: 600; margin-bottom: 2px; font-family: -apple-system, sans-serif; font-size: 13px; }
      .bdb-sub   { color: rgba(255, 200, 210, 0.75); font-size: 11px; line-height: 1.45; font-family: -apple-system, sans-serif; }
      .bdb-btn   {
        padding: 6px 12px; font-family: inherit; font-size: 10px;
        text-transform: uppercase; letter-spacing: 0.10em;
        border-radius: 5px; cursor: pointer; transition: all 0.15s;
        background: rgba(255, 64, 96, 0.22); color: #ffd0d8;
        border: 1px solid rgba(255, 64, 96, 0.55);
        flex-shrink: 0;
      }
      .bdb-btn:hover { background: rgba(255, 64, 96, 0.36); color: #fff; }
      .bdb-btn.ghost { background: transparent; color: rgba(255,200,210,0.65); border-color: rgba(255,255,255,0.18); }
      .bdb-btn.ghost:hover { color: #fff; border-color: rgba(255,255,255,0.40); }
    `
    document.head.appendChild(s)
    document.body.appendChild(el)
    el.querySelector('#bdb-retry').addEventListener('click', () => connect())
    el.querySelector('#bdb-relaunch').addEventListener('click', () => {
      if (window.jarvis && window.jarvis.relaunch) window.jarvis.relaunch()
    })
  }
  el.style.display = 'flex'
}
function hideBackendDownBanner () {
  const el = document.getElementById('backend-down-banner')
  if (el) el.style.display = 'none'
}

function connect () {
  ws = new WebSocket(`ws://127.0.0.1:${PORT}/ws`)

  ws.onopen = () => {
    console.log('[JARVIS] connected')
    _wsRetries = 0
    hideBackendDownBanner()
    if ($hudLinkVal) { $hudLinkVal.textContent = 'ONLINE'; $hudLinkVal.style.color = 'var(--green)' }
    // NOTE: Do NOT push status here — components haven't initialised yet.
    // Camera and gesture each call sendSystemStatus() as they come online.
    // Pushing all-False values now would cause the startup greeting to falsely
    // report issues before any component has had a chance to start.
  }
  ws.onclose = () => {
    if ($hudLinkVal) { $hudLinkVal.textContent = 'RETRY…'; $hudLinkVal.style.color = 'var(--amber)' }
    _wsRetries += 1
    // After 4 failed attempts (~6 seconds), show the user-visible banner.
    if (_wsRetries >= 4) showBackendDownBanner()
    setTimeout(connect, 1500)
  }
  ws.onerror = () => {
    if ($hudLinkVal) { $hudLinkVal.textContent = 'ERROR'; $hudLinkVal.style.color = 'var(--red)' }
  }

  ws.onmessage = ({ data }) => {
    const msg = JSON.parse(data)

    if (msg.type === 'state') {
      applyState(msg.state)
    } else if (msg.type === 'user_message') {
      addMessage('user', msg.text)
      if (window._incSidePanelRequest) window._incSidePanelRequest()
      // Auto-close overlay on voice dismiss commands
      if (arOverlay && /\b(close|dismiss|hide|clear|remove)\b.*(overlay|visual|cards?|diagram|timeline)|^(close|dismiss|hide) it$/i.test(msg.text)) {
        setTimeout(() => clearOverlay(true), 300)
      }
      // Camera mode voice commands
      if (/\b(camera mode|cam mode|full.?screen cam|activate cam(?:era)?)\b/i.test(msg.text)) {
        setTimeout(enterCamMode, 300)
      }
      if (/\b(exit cam(?:era)?|leave cam(?:era)?|normal mode|exit fullscreen)\b/i.test(msg.text)) {
        setTimeout(exitCamMode, 300)
      }
      // Compress / expand all hologram panels
      if (arOverlay && /\b(compress|collapse|minimize)\b.*(all|panel|every)/i.test(msg.text)) {
        setTimeout(() => {
          arPanels.forEach((_, i) => collapsePanel(i))
          selectedIdx = -1
        }, 300)
      }
    } else if (msg.type === 'chunk') {
      appendChunk(msg.text)
    } else if (msg.type === 'done') {
      finishStreaming()
      // Route practice content to the study panel if open
      if (document.getElementById('practice-panel') && msg.full_text) {
        const ft = msg.full_text
        if (ft.includes('FRONT:') || ft.includes('ANSWER:') ||
            ft.match(/^\d+\./m) || ft.includes('Practice') || ft.includes('Flashcard')) {
          renderPracticeContent(ft)
        }
      }
      // Auto-parse weather data and update the right sidebar widget
      if (msg.full_text && /weather|temperature|°F|°C|forecast|sunny|cloudy|rainy|clear/i.test(msg.full_text)) {
        updateWeatherWidget(msg.full_text)
      }
    } else if (msg.type === 'muted') {
      micMuted = true
      $mic.classList.add('muted')
      $mic.classList.remove('listening')
      $mic.textContent = '○'
    } else if (msg.type === 'unmuted') {
      micMuted = false
      $mic.classList.remove('muted')
      $mic.textContent = '⏺'
    } else if (msg.type === 'listening') {
      applyState('listening')
    } else if (msg.type === 'confirm_request') {
      showConfirm(msg.id, msg.message, msg.title)
    } else if (msg.type === 'proactive') {
      showProactiveToast(msg.text)
    } else if (msg.type === 'confirm_expired') {
      hideConfirm()
    } else if (msg.type === 'study_chunk') {
      studyChunkCount++
      $studyCount.textContent = studyChunkCount
    } else if (msg.type === 'study_started') {
      studyActive = true
      $studyBtn.classList.add('active')
      $studyBar.classList.add('visible')
    } else if (msg.type === 'hw_loop_status') {
      // Homework auto-answer loop status
      const hwStatus = msg.status  // running | answering | waiting | done | stopped | error
      if (hwStatus === 'running' || hwStatus === 'answering' || hwStatus === 'waiting') {
        $hwBar.classList.add('visible')
        if (msg.question > 0) $hwQnum.textContent = msg.question
        if (msg.answer) {
          $hwAnswer.textContent = msg.answer.length > 60 ? msg.answer.slice(0, 58) + '…' : msg.answer
        } else if (msg.text) {
          $hwAnswer.textContent = msg.text.length > 60 ? msg.text.slice(0, 58) + '…' : msg.text
        }
      } else if (hwStatus === 'done' || hwStatus === 'stopped' || hwStatus === 'error') {
        $hwBar.classList.remove('visible')
        $hwQnum.textContent = '0'
        $hwAnswer.textContent = 'scanning…'
      }
    } else if (msg.type === 'overlay') {
      console.log('[ws] overlay message received — type:', msg.data?.overlay_type, '| items:', msg.data?.items?.length)
      showOverlay(msg.data)
    } else if (msg.type === 'camera_capture_request') {
      captureAndSendFrame(msg.id)
    } else if (msg.type === 'shutdown') {
      // Give TTS a moment to finish the farewell, then quit entirely
      setTimeout(() => window.jarvis?.quit(), 2200)
    } else if (msg.type === 'coding_agent_start') {
      showCodingPanel(msg.request)
    } else if (msg.type === 'coding_agent_progress') {
      addCodingStep(msg.phase, msg.detail)
    } else if (msg.type === 'coding_agent_done') {
      finishCodingAgent(msg.summary, msg.restart_needed, msg.success)
    } else if (msg.type === 'computer_agent_start') {
      showComputerAgentHUD(msg.request)
    } else if (msg.type === 'computer_agent_action') {
      addComputerAgentAction(msg.action, msg.detail)
    } else if (msg.type === 'computer_agent_done') {
      hideComputerAgentHUD()

    // ── Timer HUD ────────────────────────────────────────────────────────────
    } else if (msg.type === 'timer_tick') {
      showTimerHUD(msg.id, msg.remaining, msg.total, msg.label)
    } else if (msg.type === 'timer_done') {
      timerDone(msg.id, msg.label)
    } else if (msg.type === 'timer_cancelled') {
      hideTimerHUD(msg.id)

    // ── Sidebar live data ─────────────────────────────────────────────────────
    } else if (msg.type === 'weather_update') {
      updateWeatherWidget(msg)
    } else if (msg.type === 'agenda_update') {
      updateAgendaWidget(msg.events)
    } else if (msg.type === 'files_update') {
      updateFilesWidget(msg.files)

    // ── First-run wizard (re-)trigger ─────────────────────────────────────────
    } else if (msg.type === 'show_first_run') {
      if (typeof window.showSetupWizard === 'function') {
        window.showSetupWizard(!!msg.reset)
      }

    // ── Now Playing extended ──────────────────────────────────────────────────
    } else if (msg.type === 'now_playing') {
      updateNowPlaying(msg.title, msg.artist, msg.album)

    // ── Info card ─────────────────────────────────────────────────────────────
    } else if (msg.type === 'info_card') {
      showInfoCard(msg)
    }
  }
}

// ─── Camera frame capture (for read_camera tool) ──────────────────────────────
// When JARVIS calls read_camera(), the backend sends camera_capture_request.
// We capture the current cam-video frame and send it back as base64 JPEG.

function captureAndSendFrame (reqId) {
  try {
    if (!$camVideo || $camVideo.readyState < 2) {
      send({ type: 'camera_frame', id: reqId, image: null })
      return
    }

    const canvas = document.createElement('canvas')
    const vw = $camVideo.videoWidth  || 640
    const vh = $camVideo.videoHeight || 480

    // Downsample slightly for faster transfer
    const scale = Math.min(1, 640 / vw)
    canvas.width  = Math.round(vw * scale)
    canvas.height = Math.round(vh * scale)

    const ctx2 = canvas.getContext('2d')

    // Mirror the image to match the mirrored display (scaleX(-1) in CSS)
    ctx2.save()
    ctx2.translate(canvas.width, 0)
    ctx2.scale(-1, 1)
    ctx2.drawImage($camVideo, 0, 0, canvas.width, canvas.height)
    ctx2.restore()

    // Encode as JPEG — quality 0.88 is a good balance of size vs clarity
    const imageData = canvas.toDataURL('image/jpeg', 0.88)
    send({ type: 'camera_frame', id: reqId, image: imageData })
  } catch (err) {
    console.warn('[cam] captureAndSendFrame failed:', err.message)
    send({ type: 'camera_frame', id: reqId, image: null })
  }
}

// ─── Permission dialog ───────────────────────────────────────────────────────

let _confirmId = null

function showConfirm (id, message, title) {
  _confirmId = id
  $confirmMsg.textContent = message
  const $icon = document.getElementById('confirm-icon')
  if ($icon) $icon.textContent = title || '⚠ ACCESS REQUEST'
  $confirmOverlay.classList.add('visible')
}

function hideConfirm () {
  $confirmOverlay.classList.remove('visible')
  _confirmId = null
}

function respondConfirm (approved) {
  if (_confirmId) send({ type: 'confirm_response', id: _confirmId, approved })
  hideConfirm()
}

$confirmAllow.addEventListener('click', () => respondConfirm(true))
$confirmDeny.addEventListener('click',  () => respondConfirm(false))

// ─── Proactive alert toast ────────────────────────────────────────────────────

const $proactiveToast    = document.getElementById('proactive-toast')
const $proactiveToastMsg = document.getElementById('proactive-toast-msg')
let   _proactiveTimer    = null

function showProactiveToast (text) {
  if (!$proactiveToast || !$proactiveToastMsg) return
  $proactiveToastMsg.textContent = text
  $proactiveToast.classList.add('visible')
  // Also surface in the chat so it's logged
  addMessage('jarvis', text)
  // Auto-dismiss after 8 seconds
  if (_proactiveTimer) clearTimeout(_proactiveTimer)
  _proactiveTimer = setTimeout(() => {
    $proactiveToast.classList.remove('visible')
    _proactiveTimer = null
  }, 8000)
}

function send (obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj))
}

// ─── System status — broadcast component health to the backend ───────────────
// JARVIS reads this to give honest startup greetings and self-diagnose issues.
// Call this whenever a component initialises, fails, or changes state.

function sendSystemStatus (extra = {}) {
  const status = { websocket: !!(ws && ws.readyState === WebSocket.OPEN) }
  if (cameraActive)    status.camera       = true
  if (mpReady)         status.gesture      = true
  // Explicit overrides (can set false, errors, etc.)
  Object.assign(status, extra)
  send({ type: 'system_status', status })
  // Also expose on window for DevTools inspection
  window.__jarvisStatus = status
}

// ─── State ───────────────────────────────────────────────────────────────────

const STATE_ICONS_MAP = {
  idle:      '◈',
  thinking:  '◎',
  speaking:  '◀',
  listening: '●',
  studying:  '◉',
}
const STATE_SUBS = {
  idle:      'AWAITING INPUT',
  thinking:  'PROCESSING...',
  speaking:  'TRANSMITTING',
  listening: 'VOICE SCAN ON',
  studying:  'CAPTURING DATA',
}

function applyState (state) {
  jarvisState = state
  const s = STATE_MAP[state] || STATE_MAP.idle

  $dot.className   = state
  $label.className = state
  $label.textContent = s.label
  $strip.textContent = s.strip
  $app.className   = state
  hudTargetColor   = s.hud

  // Sync left sidebar status indicator
  updateSidebarState(state)

  // Update side panel state
  if ($hudStateVal) {
    $hudStateVal.textContent = s.label
    $hudStateVal.setAttribute('data-state', state)
  }
  if ($hudStateIcon) $hudStateIcon.textContent = STATE_ICONS_MAP[state] || '◈'
  if ($hudStateSub)  $hudStateSub.textContent  = STATE_SUBS[state]  || ''

  // Mic button listening glow
  if (state === 'listening') {
    $mic.classList.add('listening')
  } else {
    $mic.classList.remove('listening')
  }

  // Stop button only while speaking
  if (state === 'speaking') {
    $stop.classList.add('active')
  } else {
    $stop.classList.remove('active')
  }

  // Drive side panel activity bars — active while JARVIS is doing work
  if (window._setSidePanelActive) {
    window._setSidePanelActive(state === 'thinking' || state === 'speaking' || state === 'studying')
  }
}

// ─── Markdown renderer ───────────────────────────────────────────────────────

function escHtml (s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')
}

function renderMarkdown (raw) {
  let s = escHtml(raw)
  s = s.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, l, code) =>
    `<pre><code>${code.trim()}</code></pre>`)
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>')
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
  s = s.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, '<em>$1</em>')
  const parts = s.split(/(<pre>[\s\S]*?<\/pre>)/g)
  s = parts.map((p, i) => i % 2 === 0 ? p.replace(/\n/g, '<br>') : p).join('')
  return s
}

// ─── Messages ────────────────────────────────────────────────────────────────

function nowTs () {
  const d = new Date()
  return String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0')
}

function buildRow (role) {
  const row    = document.createElement('div')
  row.className = `msg ${role}`

  const ts   = document.createElement('span')
  ts.className = 'msg-ts'
  ts.textContent = nowTs()

  const who  = document.createElement('span')
  who.className = 'msg-who'
  who.textContent = role === 'user' ? 'YOU' : 'J.A.R.V.I.S'

  const arrow = document.createElement('span')
  arrow.className = 'msg-arrow'
  arrow.textContent = '▸'

  const body = document.createElement('span')
  body.className = 'msg-body'

  row.appendChild(ts)
  row.appendChild(who)
  row.appendChild(arrow)
  row.appendChild(body)
  return { row, body }
}

function addMessage (role, text) {
  // If a stream is still open, close it cleanly before adding a new message
  if (currentRow) finishStreaming()

  $empty.style.display = 'none'

  const { row, body } = buildRow(role)
  if (role === 'jarvis') {
    body.innerHTML = renderMarkdown(text)
    currentRow = null
  } else {
    body.textContent = text
  }

  $chat.appendChild(row)
  $chat.scrollTop = $chat.scrollHeight
  return body
}

function appendChunk (chunk) {
  if (!currentRow) {
    $empty.style.display = 'none'
    const { row, body } = buildRow('jarvis')
    row.classList.add('streaming')
    body._raw = ''
    // Use a <span> child for the text so we can append nodes instead of
    // resetting textContent each chunk (which caused flicker / visual stacking)
    const textNode = document.createTextNode('')
    body.appendChild(textNode)
    body._textNode = textNode
    $chat.appendChild(row)
    currentRow = row
  }
  const body = currentRow.querySelector('.msg-body')
  body._raw = (body._raw || '') + chunk
  // Append to the existing text node — no DOM reset, no flicker
  if (body._textNode) {
    body._textNode.nodeValue = body._raw
  } else {
    body.textContent = body._raw
  }
  $chat.scrollTop = $chat.scrollHeight
}

function updateNowPlaying (text) {
  const m = text.match(/[Nn]ow playing[:\s]+(.+?)(?:\.|$)/m)
           || text.match(/[Pp]laying '(.+?)'/m)
           || text.match(/[Ss]huffling .+ for[:\s]+(.+?)(?:\.|$)/m)
  if (m) {
    $npTitle.textContent = m[1].trim()
    $nowPlaying.classList.add('visible')
  }
}

function finishStreaming () {
  if (currentRow) {
    currentRow.classList.remove('streaming')
    const body = currentRow.querySelector('.msg-body')
    const raw  = body._raw || (body._textNode ? body._textNode.nodeValue : body.textContent) || ''
    body._textNode = null
    body.innerHTML = renderMarkdown(raw)
    updateNowPlaying(raw)
    currentRow = null
  }
}

// ─── Input ───────────────────────────────────────────────────────────────────

function submitText () {
  const text = $input.value.trim()
  if (!text) return
  $input.value = ''
  hideInfoCard()   // dismiss info card when user asks something new
  send({ type: 'message', text })
  if (window._incSidePanelRequest) window._incSidePanelRequest()
}

$send.addEventListener('click', submitText)
$input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitText() }
})

$mic.addEventListener('click', () => {
  if (micMuted) send({ type: 'unmute' })
  else          send({ type: 'mute' })
})

$stop.addEventListener('click', () => {
  send({ type: 'interrupt' })
  applyState('idle')
})

// ─── Quick action chips ──────────────────────────────────────────────────────

document.querySelectorAll('.chip').forEach(chip => {
  chip.addEventListener('click', () => {
    const msg = chip.dataset.msg
    if (!msg) return
    send({ type: 'message', text: msg })
    addMessage('user', msg)
  })
})

// ─── Placeholder rotation ────────────────────────────────────────────────────

const HINTS = [
  'INPUT COMMAND...',
  'SAY "HEY JARVIS" TO ACTIVATE...',
  '"PLAY MY GYM PLAYLIST"',
  '"WHAT\'S THE WEATHER TODAY"',
  '"OPEN YOUTUBE"',
  '"START A TIMER"',
]
let _hintIdx = 0
setInterval(() => {
  if (document.activeElement !== $input && !$input.value) {
    _hintIdx = (_hintIdx + 1) % HINTS.length
    $input.placeholder = HINTS[_hintIdx]
  }
}, 4500)

// ─── Camera ──────────────────────────────────────────────────────────────────

const $camVideo   = document.getElementById('cam-video')
const $camSection = document.getElementById('cam-section')

let cameraActive = false

async function initCamera () {
  try {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error('getUserMedia not supported')
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: 'user',
        width:  { ideal: 640 },
        height: { ideal: 480 },
      },
      audio: false,
    })
    $camVideo.srcObject = stream
    cameraActive = true
    $camSection.classList.remove('no-cam')
    console.log('[cam] live — stream active')
    sendSystemStatus({ camera: true })

    // Camera is now live — (re)start MediaPipe Hands if not already running.
    if (!mpReady) setTimeout(() => initMediaPipe(), 800)
  } catch (err) {
    cameraActive = false
    const denied = err.name === 'NotAllowedError'
    const reason = denied ? 'camera_denied' : 'camera_error'
    console.warn('[cam] unavailable:', denied
      ? 'macOS camera denied — System Settings → Privacy → Camera'
      : err.message)
    $camSection.classList.add('no-cam')
    sendSystemStatus({ camera: false, camera_error: reason })

    const fixEl = document.getElementById('no-cam-fix')
    if (fixEl && !fixEl._bound) {
      fixEl._bound = true
      fixEl.addEventListener('click', () => {
        window.jarvis?.openCameraPrefs()
        // Poll every 3s to auto-retry once permission is granted
        const poll = setInterval(async () => {
          try {
            const s = await navigator.mediaDevices.getUserMedia({ video: true, audio: false })
            s.getTracks().forEach(t => t.stop())   // just checking
            clearInterval(poll)
            initCamera()                            // retry for real
          } catch (_) {}
        }, 3000)
        setTimeout(() => clearInterval(poll), 60000) // stop polling after 60s
      })
    }
  }
}

// ─── Waveform bar ────────────────────────────────────────────────────────────

const $waveformCanvas = document.getElementById('waveform-canvas')
const $waveformLevel  = document.getElementById('waveform-level')
let wCtx         = $waveformCanvas ? $waveformCanvas.getContext('2d') : null
let audioCtxNode = null
let audioAnalyser = null
let audioData     = null

async function initAudioWaveform () {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false })
    audioCtxNode = new AudioContext()
    const src    = audioCtxNode.createMediaStreamSource(stream)
    audioAnalyser = audioCtxNode.createAnalyser()
    audioAnalyser.fftSize         = 128
    audioAnalyser.smoothingTimeConstant = 0.78
    audioData    = new Uint8Array(audioAnalyser.frequencyBinCount)
    src.connect(audioAnalyser)
    console.log('[waveform] audio analyser running')
  } catch (e) {
    console.warn('[waveform] audio init failed:', e.message)
  }
}

function resizeWaveform () {
  if (!$waveformCanvas) return
  $waveformCanvas.width  = $waveformCanvas.clientWidth  || 460
  $waveformCanvas.height = $waveformCanvas.clientHeight || 30
}

function drawWaveform () {
  if (!wCtx || !$waveformCanvas) return
  const W = $waveformCanvas.width  || $waveformCanvas.clientWidth  || 460
  const H = $waveformCanvas.height || $waveformCanvas.clientHeight || 30
  wCtx.clearRect(0, 0, W, H)

  // State colors
  const stateColor =
    jarvisState === 'listening' ? [0,255,140] :
    jarvisState === 'speaking'  ? [0,212,255] :
    jarvisState === 'thinking'  ? [255,170,0] :
    jarvisState === 'studying'  ? [167,139,250] :
    [0,212,255]

  if (!audioAnalyser) {
    // Flat idle line
    wCtx.strokeStyle = `rgba(${stateColor.join(',')},0.12)`
    wCtx.lineWidth   = 1
    wCtx.beginPath()
    wCtx.moveTo(0, H / 2)
    wCtx.lineTo(W, H / 2)
    wCtx.stroke()
    return
  }

  audioAnalyser.getByteFrequencyData(audioData)
  const bins = audioData.length
  const bW   = W / bins

  let sumPower = 0
  for (let i = 0; i < bins; i++) {
    const norm  = audioData[i] / 255
    sumPower   += norm
    const bH    = norm * (H * 0.85)
    const x     = i * bW
    const alpha = jarvisState === 'idle' ? 0.12 + norm * 0.25 : 0.22 + norm * 0.55
    wCtx.fillStyle = `rgba(${stateColor.join(',')},${alpha.toFixed(2)})`
    wCtx.fillRect(x + 0.5, (H - bH) / 2, Math.max(bW - 1, 1), bH)
  }

  // dB readout
  if ($waveformLevel) {
    const avg = sumPower / bins
    const db  = avg > 0.001 ? Math.round(20 * Math.log10(avg)) : -999
    $waveformLevel.textContent = db === -999 ? '— dB' : `${db} dB`
  }
}

initAudioWaveform()

// ─── HUD Canvas ──────────────────────────────────────────────────────────────

const $hud = document.getElementById('hud-canvas')
const hCtx = $hud.getContext('2d')

let hudW = 460
let hudH = 355
let hudT = 0

// Current and target color for smooth lerp
let hudCurrentR = 30, hudCurrentG = 74, hudCurrentB = 102
let hudTargetColor = '#1e4a66'

function hexToRgbObj (hex) {
  hex = hex.replace('#', '')
  return {
    r: parseInt(hex.slice(0, 2), 16),
    g: parseInt(hex.slice(2, 4), 16),
    b: parseInt(hex.slice(4, 6), 16),
  }
}

function lerpColor () {
  const target = hexToRgbObj(hudTargetColor)
  const speed  = 0.06
  hudCurrentR += (target.r - hudCurrentR) * speed
  hudCurrentG += (target.g - hudCurrentG) * speed
  hudCurrentB += (target.b - hudCurrentB) * speed
}

function rgba (a) {
  return `rgba(${hudCurrentR|0},${hudCurrentG|0},${hudCurrentB|0},${a})`
}

function resizeHud () {
  hudW = $camSection.clientWidth  || 460
  hudH = $camSection.clientHeight || 355
  $hud.width  = hudW
  $hud.height = hudH
}

function formatUptime () {
  const s = Math.floor((Date.now() - sessionStart) / 1000)
  return String(Math.floor(s / 60)).padStart(2,'0') + ':' + String(s % 60).padStart(2,'0')
}

// ─── Draw helper: corner bracket ─────────────────────────────────────────────
function bracket (x, y, dx, dy, size, alpha, thick) {
  hCtx.strokeStyle = rgba(alpha)
  hCtx.lineWidth   = thick
  hCtx.lineCap     = 'square'
  hCtx.beginPath()
  hCtx.moveTo(x + dx * size, y)
  hCtx.lineTo(x, y)
  hCtx.lineTo(x, y + dy * size)
  hCtx.stroke()
}

// ─── Main HUD draw ────────────────────────────────────────────────────────────
function drawHUD () {
  const W  = hudW, H = hudH
  const CX = W / 2, CY = H / 2

  hCtx.clearRect(0, 0, W, H)

  const pulse      = Math.sin(hudT * (jarvisState === 'speaking' ? 5.5 : jarvisState === 'listening' ? 4 : 2.2)) * 0.5 + 0.5
  const pulseSharp = Math.sin(hudT * 3.5) * 0.5 + 0.5
  const isActive   = jarvisState !== 'idle'

  // ── Outer frame corner brackets ──
  const bSz = 22
  bracket(  2,   2,  1,  1, bSz, 0.7, 2)
  bracket(W-2,   2, -1,  1, bSz, 0.7, 2)
  bracket(  2, H-2,  1, -1, bSz, 0.7, 2)
  bracket(W-2, H-2, -1, -1, bSz, 0.7, 2)

  // Center area — leave space for side panels (100px each side)
  const cx2 = CX   // ring center still at window center
  const cy2 = CY

  // Available radius (must not overlap the 100px side panels)
  const maxR = Math.min(CX - 108, CY - 14)

  // ── Ring 0 — faint outermost atmosphere ──
  const atmoR = maxR * 1.08
  const atmoGrad = hCtx.createRadialGradient(cx2, cy2, atmoR * 0.9, cx2, cy2, atmoR)
  atmoGrad.addColorStop(0, rgba(0.0))
  atmoGrad.addColorStop(1, rgba(isActive ? 0.06 + pulse * 0.06 : 0.03))
  hCtx.strokeStyle = rgba(isActive ? 0.07 + pulse * 0.04 : 0.03)
  hCtx.lineWidth   = 1
  hCtx.beginPath()
  hCtx.arc(cx2, cy2, atmoR, 0, 2 * Math.PI)
  hCtx.stroke()

  // ── Ring 1 — outer ring with tick marks (slow CW rotation) ──
  const outerR = maxR * 0.97
  hCtx.save()
  hCtx.translate(cx2, cy2)
  hCtx.rotate(hudT * 0.18)

  hCtx.strokeStyle = rgba(isActive ? 0.30 : 0.18)
  hCtx.lineWidth   = 1
  hCtx.beginPath()
  hCtx.arc(0, 0, outerR, 0, 2 * Math.PI)
  hCtx.stroke()

  // Tick marks every 10°
  for (let i = 0; i < 36; i++) {
    const a      = (i / 36) * 2 * Math.PI
    const isMain = i % 9 === 0
    const tLen   = isMain ? 14 : (i % 3 === 0 ? 8 : 4)
    hCtx.strokeStyle = rgba(isMain ? 0.70 : 0.22)
    hCtx.lineWidth   = isMain ? 2 : 0.8
    const r0 = outerR - tLen
    hCtx.beginPath()
    hCtx.moveTo(Math.cos(a) * r0, Math.sin(a) * r0)
    hCtx.lineTo(Math.cos(a) * outerR, Math.sin(a) * outerR)
    hCtx.stroke()
  }
  hCtx.restore()

  // ── Ring 2 — secondary ring (slow CCW) ──
  const ring2R = outerR * 0.85
  hCtx.save()
  hCtx.translate(cx2, cy2)
  hCtx.rotate(-hudT * 0.12)
  hCtx.strokeStyle = rgba(isActive ? 0.16 + pulse * 0.08 : 0.08)
  hCtx.lineWidth   = 1
  hCtx.setLineDash([3, 9])
  hCtx.beginPath()
  hCtx.arc(0, 0, ring2R, 0, 2 * Math.PI)
  hCtx.stroke()
  hCtx.setLineDash([])
  hCtx.restore()

  // ── Ring 3 — mid dual arcs (faster counter-rotation) ──
  const midR    = outerR * 0.72
  const arcSpan = (118 / 180) * Math.PI
  hCtx.save()
  hCtx.translate(cx2, cy2)
  hCtx.rotate(-hudT * 0.38)
  hCtx.strokeStyle = rgba(isActive ? 0.55 + pulse * 0.15 : 0.35)
  hCtx.lineWidth   = isActive ? 2.5 : 1.5
  hCtx.lineCap     = 'round'
  if (isActive) {
    hCtx.shadowColor = rgba(0.8)
    hCtx.shadowBlur  = 8
  }
  for (let i = 0; i < 2; i++) {
    const startA = i * Math.PI + 0.28
    hCtx.beginPath()
    hCtx.arc(0, 0, midR, startA, startA + arcSpan)
    hCtx.stroke()
  }
  hCtx.shadowBlur = 0
  hCtx.restore()

  // ── Ring 3b — thin accent arcs (co-rotating with outer) ──
  const midR2 = outerR * 0.72
  hCtx.save()
  hCtx.translate(cx2, cy2)
  hCtx.rotate(hudT * 0.38 + Math.PI / 4)
  hCtx.strokeStyle = rgba(isActive ? 0.22 : 0.10)
  hCtx.lineWidth   = 1
  hCtx.lineCap     = 'round'
  const arcSpan2 = (60 / 180) * Math.PI
  for (let i = 0; i < 4; i++) {
    const startA = (i / 4) * 2 * Math.PI
    hCtx.beginPath()
    hCtx.arc(0, 0, midR2 - 6, startA, startA + arcSpan2)
    hCtx.stroke()
  }
  hCtx.restore()

  // ── Ring 4 — inner glow ring (state-driven pulse) ──
  const innerR  = outerR * 0.40
  const iAlpha  = jarvisState === 'idle' ? 0.14 + pulse * 0.07 : 0.30 + pulse * 0.40
  const iR      = innerR + pulse * (jarvisState === 'speaking' ? 9 : jarvisState === 'listening' ? 7 : 4)
  hCtx.save()
  hCtx.translate(cx2, cy2)
  if (isActive) {
    hCtx.shadowColor = rgba(0.9)
    hCtx.shadowBlur  = jarvisState === 'speaking' ? 18 : 10
  }
  hCtx.strokeStyle = rgba(iAlpha)
  hCtx.lineWidth   = jarvisState === 'speaking' ? 3 : 2
  hCtx.beginPath()
  hCtx.arc(0, 0, iR, 0, 2 * Math.PI)
  hCtx.stroke()
  hCtx.shadowBlur = 0
  hCtx.restore()

  // ── Ring 5 — tiny core dot ──
  const coreR = outerR * 0.14
  const coreAlpha = jarvisState === 'idle' ? 0.08 + pulseSharp * 0.06 : 0.18 + pulseSharp * 0.25
  hCtx.strokeStyle = rgba(coreAlpha)
  hCtx.lineWidth   = 1
  hCtx.beginPath()
  hCtx.arc(cx2, cy2, coreR, 0, 2 * Math.PI)
  hCtx.stroke()

  // ── Crosshair center ──
  const cLen = 8
  hCtx.strokeStyle = rgba(isActive ? 0.55 + pulse * 0.2 : 0.28)
  hCtx.lineWidth   = 1
  hCtx.beginPath()
  hCtx.moveTo(cx2 - cLen, cy2)
  hCtx.lineTo(cx2 + cLen, cy2)
  hCtx.moveTo(cx2, cy2 - cLen)
  hCtx.lineTo(cx2, cy2 + cLen)
  hCtx.stroke()

  // ── Face-detection box ──
  const bxW   = Math.min(W * 0.28, 120)
  const bxH   = H * 0.44
  const bxX   = cx2 - bxW / 2
  const bxY   = cy2 - bxH / 2
  const bcSz  = 14
  const bcAlpha = 0.38 + pulse * 0.18
  ;[
    [bxX,       bxY,        1,  1],
    [bxX + bxW, bxY,       -1,  1],
    [bxX,       bxY + bxH,  1, -1],
    [bxX + bxW, bxY + bxH, -1, -1],
  ].forEach(([x, y, dx, dy]) => bracket(x, y, dx, dy, bcSz, bcAlpha, 1.5))

  hCtx.font      = '7.5px Menlo, Courier New, monospace'
  hCtx.fillStyle = rgba(0.35)
  hCtx.textAlign = 'center'
  hCtx.fillText('◈ DYLAN ROE', cx2, bxY - 5)

  // ── Horizontal scan line ──
  const scanPeriod = 5.0
  const scanY      = ((hudT % scanPeriod) / scanPeriod) * H
  const scanGrad   = hCtx.createLinearGradient(0, scanY - 12, 0, scanY + 12)
  scanGrad.addColorStop(0,   'transparent')
  scanGrad.addColorStop(0.4, rgba(0.10))
  scanGrad.addColorStop(0.5, rgba(0.28))
  scanGrad.addColorStop(0.6, rgba(0.10))
  scanGrad.addColorStop(1,   'transparent')
  hCtx.fillStyle = scanGrad
  hCtx.fillRect(0, scanY - 12, W, 24)

  // ── Bottom status text ──
  hCtx.textAlign = 'center'
  hCtx.font      = '8px Menlo, Courier New, monospace'

  let bottomText
  if (jarvisState === 'thinking') {
    hCtx.fillStyle = `rgba(255,170,0,${0.55 + pulse * 0.3})`
    bottomText = '■  COGNITIVE ENGINE: ACTIVE  ■'
  } else if (jarvisState === 'speaking') {
    hCtx.fillStyle = rgba(0.65 + pulse * 0.25)
    bottomText = '◀  AUDIO TRANSMISSION: ACTIVE  ▶'
  } else if (jarvisState === 'listening') {
    hCtx.fillStyle = `rgba(0,255,140,${0.55 + pulse * 0.35})`
    bottomText = '●  VOICE RECOGNITION: ACTIVE  ●'
  } else if (jarvisState === 'studying') {
    hCtx.fillStyle = `rgba(167,139,250,${0.45 + pulse * 0.2})`
    bottomText = '◉  STUDY MODE: CAPTURING'
  } else {
    hCtx.fillStyle = rgba(0.28)
    bottomText = 'IDENTITY CONFIRMED — MR. ROE'
  }
  hCtx.fillText(bottomText, cx2, H - 8)
}

// ─── Animation loop ──────────────────────────────────────────────────────────

function hudTick () {
  hudT += 1 / 60
  lerpColor()
  drawHUD()
  drawWaveform()
  if (arOverlay) {
    updateArFloat()
  }
  requestAnimationFrame(hudTick)
}

// ─── Boot ────────────────────────────────────────────────────────────────────

window.addEventListener('resize', () => { resizeHud(); resizeGestureCanvas(); resizeWaveform() })
resizeHud()
resizeWaveform()
initCamera()   // camera on for video feed
connect()
hudTick()
$input.focus()

// ─── HUD support footer (version + Send logs) ────────────────────────────────
;(async () => {
  const $v = document.getElementById('hud-version')
  const $s = document.getElementById('hud-send-logs')
  if (!$v || !$s) return
  try {
    const v = window.jarvis && window.jarvis.getVersion ? await window.jarvis.getVersion() : null
    if (v) $v.textContent = 'v' + v
  } catch (e) { /* ignore */ }
  $s.addEventListener('click', async (e) => {
    e.preventDefault()
    if (!(window.jarvis && window.jarvis.exportLogs)) return
    $s.textContent = 'Saving…'
    const r = await window.jarvis.exportLogs()
    if (r && r.ok) {
      $s.textContent = '✓ Saved to Desktop'
      setTimeout(() => { $s.textContent = 'Send logs' }, 4000)
    } else {
      $s.textContent = '✗ Failed'
      setTimeout(() => { $s.textContent = 'Send logs' }, 4000)
    }
  })
})()

// ──────────────────────────────────────────────────────────────────────────────
// AR OVERLAY + GESTURE SYSTEM
// Minimalist timeline / chip overlay — click to expand draggable floating card
// Pinch detection: MediaPipe Hands thumb/index distance
// ──────────────────────────────────────────────────────────────────────────────

// ─── AR Gesture canvas ───────────────────────────────────────────────────────

const $gestureCanvas  = document.getElementById('gesture-canvas')
const gCtx            = $gestureCanvas.getContext('2d')
const $arLayer        = document.getElementById('ar-overlay-layer')
const $gestureBadge   = document.getElementById('gesture-status')

// Floating expanded card refs
const $arCard      = document.getElementById('ar-expanded-card')
const $arCardLabel = document.getElementById('ar-card-label-el')
const $arCardTitle = document.getElementById('ar-card-title-el')
const $arCardBody  = document.getElementById('ar-card-body-el')
const $arCardFoot  = document.getElementById('ar-card-footer')
const $arCardClose = document.getElementById('ar-card-close')
const $arDragHdl   = document.getElementById('ar-card-drag-handle')
const $arCardSweep = document.getElementById('ar-card-sweep')

function resizeGestureCanvas () {
  $gestureCanvas.width  = window.innerWidth  || 460
  $gestureCanvas.height = window.innerHeight || 860
}
resizeGestureCanvas()

// ─── AR state ────────────────────────────────────────────────────────────────

// arOverlay is declared at top of file (needed by hudTick before AR section loads)
let arPanels      = []     // DOM elements for each panel
let arPanelData   = []     // {x, y, w, h, idx, item, baseRotY} per panel
let selectedIdx   = -1     // which panel is selected (-1 = none)

// Gesture state
let pinchActive    = false
let mpHands        = null
let mpProcessing   = false
let mpFrame        = 0

// Theme color tables
const OVERLAY_THEMES = {
  cyan:   { border: 'rgba(0,212,255,0.55)',    glow: 'rgba(0,212,255,0.14)',    text: '#00d4ff',  barBg: 'rgba(0,212,255,0.08)'  },
  amber:  { border: 'rgba(255,170,0,0.55)',    glow: 'rgba(255,170,0,0.14)',    text: '#ffaa00',  barBg: 'rgba(255,170,0,0.08)'  },
  green:  { border: 'rgba(0,255,140,0.55)',    glow: 'rgba(0,255,140,0.14)',    text: '#00ff8c',  barBg: 'rgba(0,255,140,0.08)'  },
  purple: { border: 'rgba(167,139,250,0.55)',  glow: 'rgba(167,139,250,0.14)', text: '#a78bfa',  barBg: 'rgba(167,139,250,0.08)' },
}
const ITEM_COLORS = {
  cyan:   '#00d4ff',
  amber:  '#ffaa00',
  green:  '#00ff8c',
  purple: '#a78bfa',
  red:    '#ff4060',
}


// ─── Panel layout helpers ─────────────────────────────────────────────────────

function getPanelRotY (idx, total) {
  if (total <= 1) return 0
  const norm = (idx / (total - 1)) - 0.5   // -0.5 … +0.5
  return norm * 26                           // max ±13°
}

// ─── Timeline layout — horizontal line with alternating labels above/below ───

function _buildTimelineLayout (items, theme, N) {
  const track = document.createElement('div')
  track.className = 'ar-timeline-track'
  track.style.pointerEvents = 'none'

  // Horizontal line
  const line = document.createElement('div')
  line.className = 'ar-timeline-line'
  line.style.background = `linear-gradient(90deg,transparent,${theme.text}66,${theme.text}88,${theme.text}66,transparent)`
  track.appendChild(line)

  items.forEach((item, i) => {
    const accentHex = ITEM_COLORS[item.color] || theme.text
    const pct       = N > 1 ? (i / (N - 1)) * 90 + 5 : 50  // 5%…95%
    const isAbove   = i % 2 === 0
    const tickH     = 20  // px tick line connecting label to the line

    const node = document.createElement('div')
    node.className = 'ar-node'
    node.style.cssText = `
      left: ${pct}%;
      ${isAbove ? 'bottom: calc(50% + 4px)' : 'top: calc(50% + 4px)'};
      transform: translateX(-50%);
      flex-direction: column${isAbove ? '-reverse' : ''};
      color: ${accentHex};
      animation: ar-assemble 0.4s ease-out ${(i * 0.07).toFixed(2)}s both;
    `

    // Year / label
    const year = document.createElement('div')
    year.className = 'ar-node-year'
    year.textContent = item.label

    // Short event name
    const name = document.createElement('div')
    name.className = 'ar-node-name'
    name.textContent = item.title

    // Dot that sits ON the line
    const dot = document.createElement('div')
    dot.className = 'ar-node-dot'
    dot.style.cssText = `
      background: ${accentHex};
      box-shadow: 0 0 8px ${accentHex};
      position: absolute;
      ${isAbove ? 'bottom' : 'top'}: -${4}px;
      left: 50%; transform: translateX(-50%);
      width: 8px; height: 8px;
    `

    // Vertical tick connecting node to line
    const tick = document.createElement('div')
    tick.className = 'ar-node-tick'
    tick.style.cssText = `
      height: ${tickH}px;
      background: ${accentHex}88;
    `

    if (isAbove) {
      node.append(year, name, tick)
    } else {
      node.append(tick, name, year)
    }
    node.appendChild(dot)

    node.addEventListener('click', () => selectPanel(i))

    track.appendChild(node)
    arPanels.push(node)
    arPanelData.push({ x: 0, y: 0, w: 0, h: 0, item, idx: i, accentHex, norm: 0 })
  })

  $arLayer.appendChild(track)
}

// ─── Chip layout — floating labeled chips in a loose grid ────────────────────

function _buildChipLayout (items, theme, N) {
  const camW = $camSection.clientWidth  || 460
  const camH = $camSection.clientHeight || 355
  const cols   = N <= 3 ? N : N <= 6 ? 3 : 4
  const rows   = Math.ceil(N / cols)
  const chipW  = Math.min(120, Math.floor((camW * 0.82) / cols) - 10)
  const gapX   = 12
  const gapY   = 12
  const totalW = cols * chipW + (cols - 1) * gapX
  const startX = (camW - totalW) / 2
  const startY = camH * 0.28

  items.forEach((item, i) => {
    const accentHex = ITEM_COLORS[item.color] || theme.text
    const col = i % cols
    const row = Math.floor(i / cols)
    const x = startX + col * (chipW + gapX)
    const y = startY + row * (34 + gapY)

    const chip = document.createElement('div')
    chip.className = 'ar-chip'
    chip.style.cssText = `
      left: ${x}px; top: ${y}px;
      width: ${chipW}px;
      color: ${accentHex};
      border-color: ${accentHex}88;
      animation-delay: ${(i * 0.06).toFixed(2)}s;
    `
    chip.innerHTML = `
      <span style="font-size:7px;opacity:0.5;margin-right:5px;letter-spacing:0.05em">${escHtml(item.label)}</span>
      <span style="font-size:8.5px;">${escHtml(item.title)}</span>
    `

    chip.addEventListener('click', () => selectPanel(i))

    $arLayer.appendChild(chip)
    arPanels.push(chip)
    arPanelData.push({ x, y, w: chipW, h: 34, item, idx: i, accentHex, norm: 0 })
  })
}

// ─── Clear overlay ───────────────────────────────────────────────────────────

function clearOverlay (animated = true) {
  hideExpandedCard()
  if (!arOverlay && !$arLayer.classList.contains('active')) return

  if (animated) {
    $arLayer.classList.remove('active')
    setTimeout(() => {
      $arLayer.innerHTML = ''
      gCtx.clearRect(0, 0, $gestureCanvas.width, $gestureCanvas.height)
    }, 420)
  } else {
    $arLayer.innerHTML = ''
    gCtx.clearRect(0, 0, $gestureCanvas.width, $gestureCanvas.height)
  }

  arOverlay     = null
  arPanels      = []
  arPanelData   = []
  selectedIdx   = -1
  pinchActive   = false
}

// ─── Expanded floating card — shown when a label is clicked ──────────────────

function showExpandedCard (item, accentHex, refEl) {
  if (!$arCard) return

  // Position: above refEl, clamped to cam-section
  const sRect = $camSection.getBoundingClientRect()
  const eRect = refEl ? refEl.getBoundingClientRect() : sRect
  const cardW = 240
  const cardH = 160  // rough estimate

  let cx = eRect.left - sRect.left + eRect.width / 2 - cardW / 2
  let cy = eRect.top  - sRect.top  - cardH - 12

  // If above goes off the top, show below instead
  if (cy < 5) cy = eRect.bottom - sRect.top + 12

  cx = Math.max(5, Math.min(sRect.width  - cardW - 5, cx))
  cy = Math.max(5, Math.min(sRect.height - cardH - 5, cy))

  $arCardLabel.textContent = item.label
  $arCardLabel.style.color = accentHex
  $arCardTitle.textContent = item.title
  $arCardTitle.style.color = accentHex + 'ee'
  $arCardBody.textContent  = item.detail
  $arCardSweep.style.background = `linear-gradient(90deg,transparent,${accentHex}bb,transparent)`

  $arCard.style.borderTopColor   = accentHex
  $arCard.style.borderColor      = accentHex + '50'
  $arCard.style.boxShadow        = `0 0 30px ${accentHex}18, inset 0 0 20px rgba(0,4,18,0.5)`
  $arCard.style.left   = cx + 'px'
  $arCard.style.top    = cy + 'px'
  $arCard.style.display = 'block'
}

function hideExpandedCard () {
  if ($arCard) $arCard.style.display = 'none'
}

// ─── Panel selection — shows floating card, talks to JARVIS ──────────────────

function selectPanel (idx) {
  if (!arOverlay || idx < 0 || idx >= arPanelData.length) return

  // Toggle: click selected again → close card
  if (selectedIdx === idx) {
    hideExpandedCard()
    arPanels[idx]?.classList.remove('selected')
    selectedIdx = -1
    return
  }

  // Deselect previous
  if (selectedIdx >= 0 && arPanels[selectedIdx]) {
    arPanels[selectedIdx].classList.remove('selected')
  }

  selectedIdx = idx
  const pd = arPanelData[idx]

  arPanels[idx]?.classList.add('selected')
  showExpandedCard(pd.item, pd.accentHex, arPanels[idx])

  // Ask JARVIS to speak the detail
  send({ type: 'message', text: `[OVERLAY SELECT] ${pd.item.title}: ${pd.item.detail}` })
  addMessage('user', `▸ ${pd.item.label}: ${pd.item.title}`)

}

function collapsePanel (idx) {
  if (idx < 0 || idx >= arPanels.length) return
  arPanels[idx]?.classList.remove('selected')
  if (selectedIdx === idx) {
    hideExpandedCard()
    selectedIdx = -1
  }
}

// ─── Drag the floating card ───────────────────────────────────────────────────

let _dragActive = false
let _dragOffX   = 0
let _dragOffY   = 0

if ($arDragHdl) {
  $arDragHdl.addEventListener('mousedown', (e) => {
    _dragActive = true
    const r  = $arCard.getBoundingClientRect()
    _dragOffX = e.clientX - r.left
    _dragOffY = e.clientY - r.top
    e.preventDefault()
  })
}

document.addEventListener('mousemove', (e) => {
  if (!_dragActive || !$arCard) return
  const sr = $camSection.getBoundingClientRect()
  let nx = e.clientX - sr.left - _dragOffX
  let ny = e.clientY - sr.top  - _dragOffY
  nx = Math.max(0, Math.min(sr.width  - $arCard.offsetWidth,  nx))
  ny = Math.max(0, Math.min(sr.height - $arCard.offsetHeight, ny))
  $arCard.style.left = nx + 'px'
  $arCard.style.top  = ny + 'px'
})

document.addEventListener('mouseup', () => { _dragActive = false })

if ($arCardClose) {
  $arCardClose.addEventListener('click', () => {
    hideExpandedCard()
    if (selectedIdx >= 0 && arPanels[selectedIdx]) {
      arPanels[selectedIdx].classList.remove('selected')
    }
    selectedIdx = -1
  })
}

// ─── Floating animation — simple parallax layer shift ────────────────────────

function updateArFloat () {
  if (!arOverlay) return

  const pxOff = 0
  const pyOff = 0
  $arLayer.style.transform = `translate(${pxOff}px, ${pyOff}px)`

  arPanels.forEach((panel, i) => {
    if (!panel) return
    const pd    = arPanelData[i]
    const isSel = (i === selectedIdx)
    if (isSel) return   // selected panel is frozen in place by selectPanel()

    // Each panel floats with its own phase — organic hologram drift
    const floatY = Math.sin(hudT * 0.85 + i * 1.1) * 4.5      // ±4.5px vertical
    const floatZ = Math.cos(hudT * 0.55 + i * 0.9) * 6        // ±6px depth pulse

    const gazeNudge = 0
    const rotY = pd.baseRotY + gazeNudge

    const scale = 1.0
    const tz    = floatZ

    panel.style.transform = `perspective(700px) rotateY(${rotY}deg) translateY(${floatY}px) translateZ(${tz}px) scale(${scale})`
    panel.style.filter = ''
  })
}



// ─── MediaPipe: hand results → pinch detection ────────────────────────────────

function onHandResults (results) {
  if (!results.multiHandLandmarks || results.multiHandLandmarks.length === 0) {
    if (pinchActive) pinchActive = false
    return
  }

  const lm = results.multiHandLandmarks[0]
  // Thumb tip = 4, index finger tip = 8
  const thumb = lm[4]
  const index = lm[8]
  const dist  = Math.hypot(thumb.x - index.x, thumb.y - index.y)

  const wasPinching = pinchActive
  pinchActive       = dist < 0.075

  if (pinchActive && !wasPinching) {
    // Fresh pinch — trigger selection
    if (arOverlay && selectedIdx >= 0) {
      // Pinch on empty space when something is selected → deselect
      collapsePanel(selectedIdx)
      selectedIdx = -1
    }
  }
}

// ─── MediaPipe frame processing ───────────────────────────────────────────────

async function runMediaPipe () {
  if (!mpReady || mpProcessing) return
  if (!$camVideo || $camVideo.readyState < 2 || !$camVideo.srcObject) return
  mpProcessing = true
  try {
    if (mpHands) await mpHands.send({ image: $camVideo })
    mpFrame++
  } catch (_) {}
  mpProcessing = false
}

// ─── MediaPipe initialisation ─────────────────────────────────────────────────

async function initMediaPipe () {
  if (mpReady) return  // already initialised — don't double-init
  try {
    if (typeof Hands === 'undefined') {
      console.warn('[pinch] MediaPipe Hands not loaded')
      return
    }
    mpHands = new Hands({
      locateFile: f => `../node_modules/@mediapipe/hands/${f}`
    })
    mpHands.setOptions({
      maxNumHands:            1,
      modelComplexity:        0,
      minDetectionConfidence: 0.72,
      minTrackingConfidence:  0.50,
    })
    mpHands.onResults(onHandResults)
    await mpHands.initialize()
    mpReady = true
    setInterval(runMediaPipe, 67)
    console.log('[pinch] MediaPipe Hands ready')
    sendSystemStatus({ gesture: true })
  } catch (err) {
    console.warn('[pinch] MediaPipe failed:', err.message || err)
    sendSystemStatus({ gesture: false })
  }
}


// ─── WebSocket overlay handler ───────────────────────────────────────────────
// Injected into ws.onmessage — hooked via the overlay check at the bottom of connect()

function handleOverlayMessage (msg) {
  if (msg.type === 'overlay') {
    showOverlay(msg.data)
  }
}

// ─── Dev helpers (callable from DevTools console) ────────────────────────────

window.testOverlay = function () {
  showOverlay({
    overlay_type: 'concepts',
    title:        'Test Overlay',
    theme:        'cyan',
    items: [
      { label: '①', title: 'One',   detail: 'First item', color: 'cyan' },
      { label: '②', title: 'Two',   detail: 'Second item', color: 'amber' },
      { label: '③', title: 'Three', detail: 'Third item', color: 'green' },
    ],
  })
}

// ─── Coding Agent Panel ──────────────────────────────────────────────────────

const $codingPanel      = document.getElementById('coding-panel')
const $codingPanelIcon  = document.getElementById('coding-panel-icon')
const $codingPanelStatus = document.getElementById('coding-panel-status')
const $codingPanelReq   = document.getElementById('coding-panel-request')
const $codingPanelLog   = document.getElementById('coding-panel-log')
const $codingDoneSummary = document.getElementById('coding-done-summary')
const $codingRestartBtns = document.getElementById('coding-restart-btns')
const $codingPanelClose  = document.getElementById('coding-panel-close')

let _codingIteration = 0

if ($codingPanelClose) {
  $codingPanelClose.addEventListener('click', () => {
    if ($codingPanel) $codingPanel.classList.remove('visible')
  })
}

const _CODING_ICONS = {
  thinking:       '◌',
  read_file:      '◎',
  edit_file:      '✎',
  write_file:     '✚',
  grep_code:      '⌕',
  run_command:    '$',
  list_directory: '⊟',
  done:           '✔',
  error:          '✖',
}

function showCodingPanel (request) {
  if (!$codingPanel) return
  // Reset state
  _codingIteration = 0
  if ($codingPanelLog)     $codingPanelLog.innerHTML = ''
  if ($codingDoneSummary)  { $codingDoneSummary.textContent = ''; $codingDoneSummary.classList.remove('visible') }
  if ($codingRestartBtns)  $codingRestartBtns.innerHTML = ''
  if ($codingPanelIcon)    { $codingPanelIcon.textContent = '⚙'; $codingPanelIcon.className = ''; }
  if ($codingPanelStatus)  $codingPanelStatus.textContent = 'RUNNING'
  if ($codingPanelReq)     $codingPanelReq.textContent = 'REQUEST: ' + (request || '').toUpperCase().slice(0, 80)
  $codingPanel.classList.add('visible')
}

function addCodingStep (phase, detail) {
  if (!$codingPanelLog) return

  // Track iterations from "thinking" steps
  if (phase === 'thinking') {
    _codingIteration++
    const badge = document.createElement('div')
    badge.className = 'coding-iter-badge'
    badge.textContent = `─── ITERATION ${_codingIteration} ───────────────────`
    $codingPanelLog.appendChild(badge)
    if ($codingPanelStatus) $codingPanelStatus.textContent = `ITER ${_codingIteration}`
  }

  const step = document.createElement('div')
  const iconClass = phase in _CODING_ICONS ? phase : 'thinking'
  step.className = 'coding-step' + (phase === 'thinking' ? ' is-thinking' : '')

  const icon = document.createElement('span')
  icon.className = `coding-step-icon ${iconClass}`
  icon.textContent = _CODING_ICONS[iconClass] || '·'

  const txt = document.createElement('span')
  txt.className = 'coding-step-text'
  txt.textContent = detail || phase

  step.appendChild(icon)
  step.appendChild(txt)
  $codingPanelLog.appendChild(step)

  // Auto-scroll to bottom
  $codingPanelLog.scrollTop = $codingPanelLog.scrollHeight
}

function finishCodingAgent (summary, restartNeeded, success) {
  if (!$codingPanel) return

  // Final step indicator
  addCodingStep('done', success ? '✔ COMPLETE' : '✖ FAILED')

  if ($codingPanelIcon) {
    $codingPanelIcon.textContent = success ? '✔' : '✖'
    $codingPanelIcon.className   = 'done'
  }
  if ($codingPanelStatus) $codingPanelStatus.textContent = success ? 'DONE' : 'FAILED'

  // Show summary
  if ($codingDoneSummary && summary) {
    $codingDoneSummary.textContent = summary
    $codingDoneSummary.classList.add('visible')
  }

  // Restart buttons
  if ($codingRestartBtns && Array.isArray(restartNeeded)) {
    restartNeeded.forEach(svc => {
      const btn = document.createElement('button')
      btn.className = 'coding-restart-btn' + (svc === 'server' ? ' server' : '')
      btn.textContent = svc === 'server' ? '↺ RESTART SERVER' : '↺ RESTART ELECTRON'
      btn.title = svc === 'server'
        ? 'cd ~/JARVIS && source .venv/bin/activate && python server.py'
        : 'cd ~/JARVIS/jarvis-app && npm start'
      btn.addEventListener('click', () => {
        // Copy restart command to clipboard as a convenience
        const cmd = svc === 'server'
          ? 'cd ~/JARVIS && source .venv/bin/activate && python server.py'
          : 'cd ~/JARVIS/jarvis-app && npm start'
        if (navigator.clipboard) navigator.clipboard.writeText(cmd).catch(() => {})
        btn.textContent = '✔ COPIED'
        setTimeout(() => {
          btn.textContent = svc === 'server' ? '↺ RESTART SERVER' : '↺ RESTART ELECTRON'
        }, 2000)
      })
      $codingRestartBtns.appendChild(btn)
    })
  }
}

// ─── Info Card ────────────────────────────────────────────────────────────────
// Slides in from the right whenever JARVIS gives a factual response.
// Server extracts title + category + facts → broadcasts info_card event.

let _infoCardTimer = null

const _IC_CAT_COLORS = {
  person:     '#4a7a9b',
  company:    '#7a5818',
  product:    '#2a6040',
  place:      '#5a3a9b',
  science:    '#1a6878',
  medical:    '#7a2a2a',
  history:    '#5a4a1a',
  technology: '#1a4a7a',
  concept:    '#3a3a5a',
  other:      '#2a3a4a',
}

function showInfoCard (data) {
  clearTimeout(_infoCardTimer)
  // Dismiss any existing card instantly before showing new one
  const old = document.getElementById('info-card')
  if (old) old.remove()

  const card    = document.createElement('div')
  card.id       = 'info-card'
  const color   = _IC_CAT_COLORS[data.category] || _IC_CAT_COLORS.other
  const catText = (data.category || 'info').toUpperCase()

  card.innerHTML = `
    <div class="ic-header">
      <span class="ic-label">◈ INFORMATION</span>
      <button class="ic-close" onclick="hideInfoCard()">✕</button>
    </div>
    <div class="ic-title">${escHtml(data.title || '')}</div>
    <span class="ic-category" style="background:${color}22;border-color:${color}55;color:${color}">${escHtml(catText)}</span>
    <ul class="ic-facts">
      ${(data.facts || []).map(f => `<li>${escHtml(f)}</li>`).join('')}
    </ul>
    <div class="ic-progress-wrap">
      <div class="ic-progress-bar" id="ic-bar"></div>
    </div>
  `

  document.body.appendChild(card)

  // Animate in on next frame
  requestAnimationFrame(() => {
    card.classList.add('visible')
    const bar = document.getElementById('ic-bar')
    if (bar) {
      bar.style.transition = 'none'
      bar.style.width = '100%'
      requestAnimationFrame(() => {
        bar.style.transition = 'width 45s linear'
        bar.style.width = '0%'
      })
    }
  })

  _infoCardTimer = setTimeout(hideInfoCard, 45000)
}

function hideInfoCard () {
  clearTimeout(_infoCardTimer)
  const card = document.getElementById('info-card')
  if (!card) return
  card.classList.remove('visible')
  setTimeout(() => { if (card.parentNode) card.remove() }, 380)
}

// ─── Timer HUD ────────────────────────────────────────────────────────────────

const _activeTimers = new Map()  // id → {el, animId}

function showTimerHUD (id, remaining, total, label) {
  let timerEl = _activeTimers.get(id)?.el
  if (!timerEl) {
    timerEl = document.createElement('div')
    timerEl.className = 'timer-hud'
    timerEl.id = 'timer-' + id
    timerEl.innerHTML = `
      <div class="timer-hud-label">${escHtml(label)}</div>
      <div class="timer-hud-ring">
        <svg viewBox="0 0 60 60"><circle class="timer-track" cx="30" cy="30" r="26"/><circle class="timer-fill" cx="30" cy="30" r="26"/></svg>
        <div class="timer-hud-time"></div>
      </div>
      <button class="timer-cancel-btn" onclick="cancelTimer('${id}')">✕</button>
    `
    document.body.appendChild(timerEl)
    setTimeout(() => timerEl.classList.add('visible'), 10)
    _activeTimers.set(id, { el: timerEl })
  }

  const pct = total > 0 ? remaining / total : 0
  const circ = 2 * Math.PI * 26
  const fill = timerEl.querySelector('.timer-fill')
  if (fill) {
    fill.style.strokeDasharray = circ
    fill.style.strokeDashoffset = circ * (1 - pct)
  }

  const timeEl = timerEl.querySelector('.timer-hud-time')
  if (timeEl) {
    const m = Math.floor(remaining / 60)
    const s = remaining % 60
    timeEl.textContent = `${m}:${String(s).padStart(2, '0')}`
  }

  // Color shift as time runs out
  if (fill) {
    if (pct > 0.4)      fill.style.stroke = '#00d4ff'
    else if (pct > 0.15) fill.style.stroke = '#ffaa00'
    else                 fill.style.stroke = '#ff4444'
  }
}

function timerDone (id, label) {
  const entry = _activeTimers.get(id)
  if (entry?.el) {
    entry.el.classList.add('done')
    const timeEl = entry.el.querySelector('.timer-hud-time')
    if (timeEl) timeEl.textContent = '0:00'
    setTimeout(() => hideTimerHUD(id), 5000)
  }
  // Flash notification
  const note = document.createElement('div')
  note.className = 'timer-notification'
  note.textContent = `⏰  ${label}`
  document.body.appendChild(note)
  setTimeout(() => note.classList.add('visible'), 10)
  setTimeout(() => { note.classList.remove('visible'); setTimeout(() => note.remove(), 400) }, 4000)
}

function hideTimerHUD (id) {
  const entry = _activeTimers.get(id)
  if (entry?.el) {
    entry.el.classList.remove('visible')
    setTimeout(() => { entry.el.remove(); _activeTimers.delete(id) }, 400)
  }
}

function cancelTimer (id) {
  send({ type: 'timer_cancel', id })
  hideTimerHUD(id)
}


// ─── Study Mode: Practice Problems & Flashcards ────────────────────────────────

let _studyClasses = JSON.parse(localStorage.getItem('jarvis_classes') || '[]')
let _flashcardData = []
let _flashcardIndex = 0
let _flashcardFlipped = false

function openPracticePanel () {
  let panel = document.getElementById('practice-panel')
  if (panel) { panel.classList.add('visible'); return }

  panel = document.createElement('div')
  panel.id = 'practice-panel'
  panel.innerHTML = `
    <div class="practice-header">
      <span>◈ STUDY MODE</span>
      <button onclick="closePracticePanel()" class="practice-close">✕</button>
    </div>
    <div class="practice-classes" id="practice-classes">
      ${_studyClasses.map((c, i) => `<button class="class-chip" onclick="selectClass('${escHtml(c)}')">${escHtml(c)}</button>`).join('')}
      <button class="class-chip add-class" onclick="addClassPrompt()">＋ Add Class</button>
    </div>
    <div class="practice-mode-row">
      <button class="mode-btn active" id="mode-problems" onclick="setPracticeMode('problems')">Practice Problems</button>
      <button class="mode-btn" id="mode-flashcards" onclick="setPracticeMode('flashcards')">Flashcards</button>
    </div>
    <div id="practice-content" class="practice-content">
      <div class="practice-empty">Select a class above to generate practice content</div>
    </div>
    <div class="practice-footer">
      <span id="selected-class" class="selected-class-label">No class selected</span>
      <button class="generate-btn" id="generate-btn" onclick="generatePractice()">Generate</button>
    </div>
  `
  document.body.appendChild(panel)
  setTimeout(() => panel.classList.add('visible'), 10)
}

let _selectedClass = ''
let _practiceMode = 'problems'

function closePracticePanel () {
  const p = document.getElementById('practice-panel')
  if (p) { p.classList.remove('visible'); setTimeout(() => p.remove(), 300) }
}

function selectClass (name) {
  _selectedClass = name
  const label = document.getElementById('selected-class')
  if (label) label.textContent = name
  document.querySelectorAll('.class-chip').forEach(b => b.classList.remove('active'))
  document.querySelectorAll('.class-chip').forEach(b => {
    if (b.textContent === name) b.classList.add('active')
  })
}

function setPracticeMode (mode) {
  _practiceMode = mode
  document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'))
  const btn = document.getElementById('mode-' + mode)
  if (btn) btn.classList.add('active')
}

function _refreshClassRow () {
  const panel = document.getElementById('practice-panel')
  if (!panel) return
  const classRow = panel.querySelector('.practice-classes')
  if (!classRow) return
  classRow.innerHTML = _studyClasses.map(c => `<button class="class-chip" onclick="selectClass('${escHtml(c)}')">${escHtml(c)}</button>`).join('')
    + `<button class="class-chip add-class" onclick="addClassPrompt()">＋ Add Class</button>`
}

function _commitNewClass (name) {
  if (!name?.trim()) return
  _studyClasses.push(name.trim())
  localStorage.setItem('jarvis_classes', JSON.stringify(_studyClasses))
  _refreshClassRow()
}

function addClassPrompt () {
  const panel = document.getElementById('practice-panel')
  if (!panel) return
  const classRow = panel.querySelector('.practice-classes')
  if (!classRow) return

  // Replace the + Add Class button with an inline input
  const addBtn = classRow.querySelector('.add-class')
  if (!addBtn) return

  const wrapper = document.createElement('span')
  wrapper.className = 'class-chip add-class-input-wrap'
  wrapper.innerHTML = `<input id="new-class-input" type="text" placeholder="e.g. AP Chemistry" autocomplete="off"
    style="background:transparent;border:none;outline:none;color:var(--cyan,#67e8f9);font-size:11px;width:130px;font-family:inherit;"
  /><button onclick="
    var v=document.getElementById('new-class-input').value;
    _commitNewClass(v);
  " style="margin-left:4px;padding:0 6px;background:rgba(103,232,249,0.15);border:1px solid rgba(103,232,249,0.35);color:var(--cyan,#67e8f9);border-radius:4px;cursor:pointer;font-size:11px;">✓</button>`
  classRow.replaceChild(wrapper, addBtn)

  const inp = document.getElementById('new-class-input')
  if (inp) {
    inp.focus()
    inp.addEventListener('keydown', e => {
      if (e.key === 'Enter') { _commitNewClass(inp.value) }
      if (e.key === 'Escape') { _refreshClassRow() }
    })
  }
}

function generatePractice () {
  if (!_selectedClass) {
    const content = document.getElementById('practice-content')
    if (content) content.innerHTML = `<div class="practice-empty" style="color:rgba(239,68,68,0.8);">⚠ Select a class first</div>`
    return
  }
  const content = document.getElementById('practice-content')
  if (content) content.innerHTML = `<div class="practice-loading"><div class="practice-spinner"></div><span>Generating ${_practiceMode}…</span></div>`
  send({ type: 'study_practice', subject: _selectedClass, mode: _practiceMode, count: 5 })
}

// Called from WS handler when JARVIS returns practice content
function renderPracticeContent (text) {
  const content = document.getElementById('practice-content')
  if (!content) return

  if (_practiceMode === 'flashcards') {
    // Parse "FRONT: X | BACK: Y" pairs
    _flashcardData = []
    text.split('\n').forEach(line => {
      const m = line.match(/FRONT:\s*(.+?)\s*\|\s*BACK:\s*(.+)/i)
      if (m) _flashcardData.push({ front: m[1].trim(), back: m[2].trim() })
    })
    if (_flashcardData.length === 0) {
      content.innerHTML = `<div class="practice-result">${renderMarkdown(text)}</div>`
      return
    }
    _flashcardIndex = 0
    _flashcardFlipped = false
    renderFlashcard(content)
  } else {
    // Practice problems — render as styled list
    content.innerHTML = `<div class="practice-result">${renderMarkdown(text)}</div>`
  }
}

function renderFlashcard (container) {
  const card = _flashcardData[_flashcardIndex]
  if (!card) return
  container.innerHTML = `
    <div class="flashcard-wrapper">
      <div class="flashcard ${_flashcardFlipped ? 'flipped' : ''}" id="flashcard" onclick="flipCard()">
        <div class="flashcard-front"><div class="fc-label">QUESTION</div><div class="fc-text">${escHtml(card.front)}</div></div>
        <div class="flashcard-back"><div class="fc-label">ANSWER</div><div class="fc-text">${escHtml(card.back)}</div></div>
      </div>
      <div class="flashcard-nav">
        <button onclick="prevCard()" ${_flashcardIndex === 0 ? 'disabled' : ''}>‹ Prev</button>
        <span>${_flashcardIndex + 1} / ${_flashcardData.length}</span>
        <button onclick="nextCard()" ${_flashcardIndex >= _flashcardData.length - 1 ? 'disabled' : ''}>Next ›</button>
      </div>
    </div>
  `
}

function flipCard () {
  _flashcardFlipped = !_flashcardFlipped
  const fc = document.getElementById('flashcard')
  if (fc) fc.classList.toggle('flipped', _flashcardFlipped)
}
function nextCard () {
  if (_flashcardIndex < _flashcardData.length - 1) {
    _flashcardIndex++; _flashcardFlipped = false
    renderFlashcard(document.getElementById('practice-content'))
  }
}
function prevCard () {
  if (_flashcardIndex > 0) {
    _flashcardIndex--; _flashcardFlipped = false
    renderFlashcard(document.getElementById('practice-content'))
  }
}

// ─── Computer Agent HUD ──────────────────────────────────────────────────────

const $caHud        = document.getElementById('ca-hud')
const $caHudStatus  = document.getElementById('ca-hud-status')
const $caHudTask    = document.getElementById('ca-hud-task')
const $caHudLog     = document.getElementById('ca-hud-log')
const _CA_ICONS     = { screenshot: '◉', click: '◎', type: '▣', key: '⌨', scroll: '↕', move: '→', default: '◈' }
const _CA_MAX_LOG   = 6   // keep last N actions visible

function showComputerAgentHUD (request) {
  if (!$caHud) return
  $caHud.classList.remove('done')
  $caHudTask.textContent = request ? ('◈ ' + request.slice(0, 80)) : ''
  if ($caHudStatus) $caHudStatus.textContent = 'INITIALIZING'
  if ($caHudLog) $caHudLog.innerHTML = ''
  $caHud.classList.add('visible')
}

function addComputerAgentAction (action, detail) {
  if (!$caHud || !$caHud.classList.contains('visible')) return
  if ($caHudStatus) $caHudStatus.textContent = detail.slice(0, 30).toUpperCase()

  const row = document.createElement('div')
  row.className = `ca-action ${action}`
  const icon = _CA_ICONS[action] || _CA_ICONS.default
  row.innerHTML = `<span class="ca-action-icon">${icon}</span><span>${escHtml(detail)}</span>`
  $caHudLog.appendChild(row)

  // Keep only last _CA_MAX_LOG rows
  while ($caHudLog.children.length > _CA_MAX_LOG) {
    $caHudLog.removeChild($caHudLog.firstChild)
  }
  $caHudLog.scrollTop = $caHudLog.scrollHeight
}

function hideComputerAgentHUD () {
  if (!$caHud) return
  $caHud.classList.add('done')
  if ($caHudStatus) $caHudStatus.textContent = 'COMPLETE'
  // Slide out after 2.5s
  setTimeout(() => {
    $caHud.classList.remove('visible', 'done')
  }, 2500)
}

// ─── Arc Reactor HUD — Iron Man JARVIS central animation ─────────────────────
// Fullscreen canvas animation: concentric rings, compass markers, scan sweep,
// data readouts, inner hex grid, glowing centre — all running at 60fps.
;(function initArcReactor () {
  const canvas = document.getElementById('arc-reactor-canvas')
  if (!canvas) return
  const ctx = canvas.getContext('2d', { alpha: true })
  const T0  = Date.now()

  let W, H, CX, CY, R

  function resize () {
    const p = canvas.parentElement
    const dpr = window.devicePixelRatio || 1
    const cw = p.clientWidth, ch = p.clientHeight
    canvas.width  = cw * dpr
    canvas.height = ch * dpr
    canvas.style.width  = cw + 'px'
    canvas.style.height = ch + 'px'
    ctx.scale(dpr, dpr)
    W = cw; H = ch
    CX = W / 2; CY = H / 2
    R  = Math.min(W, H) * 0.37
  }
  const ro = new ResizeObserver(() => { resize() })
  ro.observe(canvas.parentElement)
  resize()

  // ── Ring config — refined speeds, better depth ordering ─────────────────
  // Outer rings: slow, majestic  |  Inner rings: faster, more energetic
  // segs=0 → solid ring  |  sp in rad/s
  const RINGS = [
    { fr:1.00, segs:64,  fill:0.65, sp: 0.12, w:10, col:'#00d4ff', gl:0.9, al:0.88 },
    { fr:0.91, segs:0,   fill:1.00, sp:-0.14, w: 1, col:'#00d4ff', gl:0.1, al:0.22 },
    { fr:0.85, segs:40,  fill:0.58, sp:-0.19, w: 8, col:'#00d4ff', gl:0.6, al:0.78 },
    { fr:0.78, segs:80,  fill:0.28, sp: 0.32, w: 2, col:'#00d4ff', gl:0.2, al:0.38 },
    { fr:0.72, segs:0,   fill:1.00, sp:-0.10, w: 1, col:'#00d4ff', gl:0.1, al:0.18 },
    { fr:0.66, segs:32,  fill:0.75, sp:-0.28, w: 7, col:'#00d4ff', gl:0.5, al:0.70 },
    { fr:0.58, segs:0,   fill:1.00, sp: 0.40, w: 2, col:'#ff9500', gl:0.7, al:0.48 },
    { fr:0.52, segs:48,  fill:0.42, sp: 0.55, w: 4, col:'#00d4ff', gl:0.3, al:0.50 },
    { fr:0.44, segs:0,   fill:1.00, sp:-0.24, w: 1, col:'#00d4ff', gl:0.1, al:0.25 },
    { fr:0.38, segs:20,  fill:0.78, sp:-0.80, w: 6, col:'#ff9500', gl:0.8, al:0.84 },
    { fr:0.30, segs:96,  fill:0.32, sp: 1.20, w: 3, col:'#00d4ff', gl:0.2, al:0.42 },
    { fr:0.22, segs:0,   fill:1.00, sp: 0.60, w: 8, col:'#00d4ff', gl:1.0, al:0.35 },
    { fr:0.14, segs:14,  fill:0.82, sp:-1.50, w: 5, col:'#00d4ff', gl:0.9, al:0.82 },
  ]

  // Pre-compute segment brightness — adds subtle worn/varied look
  const SEG_PAT = RINGS.map(r =>
    r.segs ? Array.from({ length: r.segs }, () =>
      Math.random() > 0.08 ? 1 : 0.12 + Math.random() * 0.28
    ) : []
  )

  // Compass: orange markers at cardinal directions
  const COMPASS = [
    { a: -Math.PI / 2, tag: 'TH:443' },
    { a:  0,           tag: 'SY:219' },
    { a:  Math.PI / 2, tag: 'NX:107' },
    { a:  Math.PI,     tag: 'AI:991' },
  ]

  // ── Draw helpers ─────────────────────────────────────────────────────────
  function ring (cfg, t) {
    const r   = cfg.fr * R
    const rot = t * cfg.sp
    ctx.save()
    ctx.lineWidth   = cfg.w
    ctx.shadowBlur  = cfg.gl * 12
    ctx.shadowColor = cfg.col

    if (cfg.segs === 0) {
      ctx.beginPath()
      ctx.arc(CX, CY, r, 0, Math.PI * 2)
      ctx.strokeStyle = cfg.col
      ctx.globalAlpha = cfg.al
      ctx.stroke()
    } else {
      const dA   = (Math.PI * 2) / cfg.segs
      const arcA = dA * cfg.fill
      const pat  = SEG_PAT[RINGS.indexOf(cfg)]
      for (let i = 0; i < cfg.segs; i++) {
        const a0 = rot + i * dA
        ctx.beginPath()
        ctx.arc(CX, CY, r, a0, a0 + arcA)
        ctx.strokeStyle = cfg.col
        ctx.globalAlpha = cfg.al * (pat[i] || 1)
        ctx.stroke()
      }
    }
    ctx.restore()
  }

  function compass (t) {
    const outerR = RINGS[0].fr * R
    COMPASS.forEach(({ a, tag }) => {
      const pulse = 0.70 + 0.30 * Math.sin(t * 1.8 + a)
      const dx    = Math.cos(a), dy = Math.sin(a)
      const dotR  = outerR + 22

      ctx.save()
      // Connector tick
      ctx.beginPath()
      ctx.moveTo(CX + dx * (outerR + 5), CY + dy * (outerR + 5))
      ctx.lineTo(CX + dx * (outerR + 14), CY + dy * (outerR + 14))
      ctx.strokeStyle = '#ff9500'
      ctx.globalAlpha = 0.55
      ctx.lineWidth   = 1.2
      ctx.shadowBlur  = 5
      ctx.shadowColor = '#ff9500'
      ctx.stroke()

      // Dot
      ctx.beginPath()
      ctx.arc(CX + dx * dotR, CY + dy * dotR, 4.5, 0, Math.PI * 2)
      ctx.fillStyle   = '#ff9500'
      ctx.globalAlpha = pulse * 0.90
      ctx.shadowBlur  = 16
      ctx.shadowColor = '#ff9500'
      ctx.fill()

      // Data label
      const tagDist = outerR + 42
      ctx.font         = '7px SF Mono,Menlo,monospace'
      ctx.textAlign    = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillStyle    = '#00d4ff'
      ctx.globalAlpha  = 0.45
      ctx.shadowBlur   = 3
      ctx.shadowColor  = '#00d4ff'
      ctx.fillText(tag, CX + dx * tagDist, CY + dy * tagDist)
      ctx.restore()
    })
  }

  function scanLine (t) {
    // Two scan lines at slightly different speeds — more dynamic
    [{ speed: 0.55, alpha: 0.65 }, { speed: -0.30, alpha: 0.30 }].forEach(s => {
      const r     = RINGS[0].fr * R * 0.96
      const ang   = t * s.speed
      const sweep = Math.PI / 6
      ctx.save()
      ctx.translate(CX, CY)
      ctx.rotate(ang)
      // Sweep wedge
      const grad = ctx.createConicalGradient
        ? ctx.createConicalGradient(0, 0, -sweep)
        : ctx.createLinearGradient(0, 0, r, 0)
      const g = ctx.createLinearGradient(0, 0, r * Math.cos(-sweep * 0.5), r * Math.sin(-sweep * 0.5))
      g.addColorStop(0,   `rgba(0,212,255,${0.20 * s.alpha})`)
      g.addColorStop(0.5, `rgba(0,212,255,${0.08 * s.alpha})`)
      g.addColorStop(1,   'rgba(0,212,255,0)')
      ctx.beginPath()
      ctx.moveTo(0, 0)
      ctx.arc(0, 0, r, -sweep, 0)
      ctx.closePath()
      ctx.fillStyle   = g
      ctx.globalAlpha = 1
      ctx.fill()
      // Leading edge line
      ctx.beginPath()
      ctx.moveTo(0, 0)
      ctx.lineTo(r, 0)
      ctx.strokeStyle = `rgba(0,220,255,${0.80 * s.alpha})`
      ctx.globalAlpha = 1
      ctx.lineWidth   = 1.2
      ctx.shadowBlur  = 8
      ctx.shadowColor = '#00d4ff'
      ctx.stroke()
      ctx.restore()
    })
  }

  function innerGrid (t) {
    const clipR   = RINGS[RINGS.length - 1].fr * R * 0.85
    const spacing = 12
    ctx.save()
    ctx.beginPath()
    ctx.arc(CX, CY, clipR, 0, Math.PI * 2)
    ctx.clip()
    const wave = t * 0.35
    for (let dx = -clipR; dx <= clipR; dx += spacing) {
      for (let dy = -clipR; dy <= clipR; dy += spacing) {
        const d2 = dx * dx + dy * dy
        if (d2 >= clipR * clipR) continue
        const d  = Math.sqrt(d2)
        const al = (0.04 + 0.028 * Math.sin(wave + d * 0.045)) * Math.pow(1 - d / clipR, 0.8)
        if (al <= 0) continue
        ctx.beginPath()
        ctx.arc(CX + dx, CY + dy, 1.0, 0, Math.PI * 2)
        ctx.fillStyle   = '#00d4ff'
        ctx.globalAlpha = al
        ctx.fill()
      }
    }
    ctx.restore()
  }

  function bgGlow (t) {
    const pulse = 0.85 + 0.15 * Math.sin(t * 0.6)
    const g = ctx.createRadialGradient(CX, CY, 0, CX, CY, R * 1.45)
    g.addColorStop(0,   `rgba(0, 45, 72, ${0.32 * pulse})`)
    g.addColorStop(0.4, `rgba(0, 18, 30, ${0.18 * pulse})`)
    g.addColorStop(1,   'rgba(0,  0,  0, 0)')
    ctx.fillStyle   = g
    ctx.globalAlpha = 1
    ctx.shadowBlur  = 0
    ctx.fillRect(0, 0, W, H)
  }

  function centre (t) {
    const pulse = 0.5 + 0.5 * Math.sin(t * 1.5)
    const ir    = RINGS[RINGS.length - 1].fr * R

    // Layered halo — smooth fade out from centre
    for (let i = 5; i >= 0; i--) {
      const lr = ir * (2.2 - i * 0.32)
      const a  = 0.055 * (i + 1) * (0.65 + 0.35 * pulse)
      const g  = ctx.createRadialGradient(CX, CY, 0, CX, CY, lr)
      g.addColorStop(0,   `rgba(0,210,255,${a})`)
      g.addColorStop(0.6, `rgba(0,100,160,${a * 0.3})`)
      g.addColorStop(1,   'rgba(0,0,0,0)')
      ctx.beginPath()
      ctx.arc(CX, CY, lr, 0, Math.PI * 2)
      ctx.fillStyle   = g
      ctx.globalAlpha = 1
      ctx.fill()
    }

    // Core fill
    const cr = ir * 0.62
    ctx.beginPath()
    ctx.arc(CX, CY, cr, 0, Math.PI * 2)
    ctx.fillStyle   = `rgba(0,175,225,${0.08 + 0.05 * pulse})`
    ctx.globalAlpha = 1
    ctx.fill()

    // Core ring
    ctx.beginPath()
    ctx.arc(CX, CY, cr, 0, Math.PI * 2)
    ctx.strokeStyle = '#00d4ff'
    ctx.lineWidth   = 1.8
    ctx.globalAlpha = 0.50 + 0.28 * pulse
    ctx.shadowBlur  = 22
    ctx.shadowColor = '#00d4ff'
    ctx.stroke()

    // Centre glyph
    const fs = Math.max(11, Math.floor(ir * 0.50))
    ctx.font         = `700 ${fs}px SF Mono,Menlo,monospace`
    ctx.textAlign    = 'center'
    ctx.textBaseline = 'middle'
    ctx.fillStyle    = '#00d4ff'
    ctx.globalAlpha  = 0.58 + 0.28 * pulse
    ctx.shadowBlur   = 18
    ctx.shadowColor  = '#00d4ff'
    ctx.fillText('◈', CX, CY)
  }

  // Live readouts — orbit slowly outside compass ring
  const _readouts = [
    { a:-1.10, base: 440, amp: 7,  sp:0.22, fmt: v => `TH:${v}` },
    { a: 0.50, base: 218, amp: 4,  sp:0.38, fmt: v => `SY:${v}` },
    { a: 2.10, base: 107, amp: 3,  sp:0.52, fmt: v => `NX:${v}` },
    { a:-2.60, base: 991, amp: 3,  sp:0.30, fmt: v => `AI:${v}` },
    { a:-0.30, base:  72, amp: 9,  sp:0.90, fmt: v => `HZ:${v}` },
    { a: 1.28, base: 336, amp: 5,  sp:0.44, fmt: v => `MX:${v}` },
  ]
  function dataReadouts (t) {
    const dist = RINGS[0].fr * R + 60
    ctx.font         = '7px SF Mono,Menlo,monospace'
    ctx.textAlign    = 'center'
    ctx.textBaseline = 'middle'
    _readouts.forEach(rd => {
      const v = Math.floor(rd.base + rd.amp * Math.sin(t * rd.sp))
      ctx.fillStyle   = '#00d4ff'
      ctx.globalAlpha = 0.38
      ctx.shadowBlur  = 2
      ctx.shadowColor = '#00d4ff'
      ctx.fillText(rd.fmt(String(v).padStart(3, '0')),
        CX + Math.cos(rd.a) * dist,
        CY + Math.sin(rd.a) * dist)
    })
  }

  // ── Main loop — 60fps, DPR-aware ─────────────────────────────────────────
  function frame () {
    const t = (Date.now() - T0) / 1000
    ctx.save()
    ctx.clearRect(0, 0, W, H)
    bgGlow(t)
    innerGrid(t)
    scanLine(t)
    RINGS.forEach(r => ring(r, t))
    compass(t)
    dataReadouts(t)
    centre(t)
    ctx.restore()
    requestAnimationFrame(frame)
  }
  frame()
})()

// ─── Side panel live data animation ──────────────────────────────────────────
// Simulates realistic fluctuating system metrics in all the HUD bars/histograms
;(function initSidePanelData () {
  const T0 = Date.now()

  // Bar elements
  const bars = {
    cpu:  { fill: document.getElementById('bar-cpu'),  val: document.getElementById('val-cpu'),  base: 42, amp: 18, sp: 0.31 },
    mem:  { fill: document.getElementById('bar-mem'),  val: document.getElementById('val-mem'),  base: 61, amp:  6, sp: 0.13 },
    gpu:  { fill: document.getElementById('bar-gpu'),  val: document.getElementById('val-gpu'),  base: 28, amp: 14, sp: 0.44 },
    tx:   { fill: document.getElementById('bar-tx'),   val: document.getElementById('val-tx'),   base: 18, amp: 15, sp: 0.72 },
    rx:   { fill: document.getElementById('bar-rx'),   val: document.getElementById('val-rx'),   base: 55, amp: 22, sp: 0.58 },
    ctx:  { fill: document.getElementById('bar-ctx'),  val: document.getElementById('val-ctx'),  base:  8, amp:  5, sp: 0.08 },
    conf: { fill: document.getElementById('bar-conf'), val: document.getElementById('val-conf'), base: 94, amp:  4, sp: 0.22 },
    mem2: { fill: document.getElementById('bar-mem2'), val: document.getElementById('val-mem2'), base: 61, amp:  6, sp: 0.13 },
    swap: { fill: document.getElementById('bar-swap'), val: document.getElementById('val-swap'), base: 12, amp:  5, sp: 0.27 },
    disk: { fill: document.getElementById('bar-disk'), val: document.getElementById('val-disk'), base: 34, amp:  3, sp: 0.09 },
  }

  // Histogram history buffers
  const histCpuData  = Array.from({ length: 10 }, () => 30 + Math.random() * 60)
  const histAudData  = Array.from({ length: 10 }, () => 10 + Math.random() * 80)

  // Counters
  let _requestCount = 0
  let _tokenCount   = 0

  // Track when JARVIS is thinking/speaking → spike CPU bars
  let _jarvisActive = false
  window._setSidePanelActive = v => { _jarvisActive = v }

  function updateBars (t) {
    Object.entries(bars).forEach(([k, b]) => {
      if (!b.fill) return
      let pct = b.base + b.amp * Math.sin(t * b.sp + k.charCodeAt(0))
      if (_jarvisActive && (k === 'cpu' || k === 'conf')) pct = Math.min(99, pct + 25)
      pct = Math.max(2, Math.min(99, Math.round(pct)))
      b.fill.style.width = pct + '%'
      // Format val display
      if (k === 'tx')  { if (b.val) b.val.textContent = (pct * 0.08).toFixed(1) + 'M' }
      else if (k === 'rx') { if (b.val) b.val.textContent = (pct * 0.12).toFixed(1) + 'M' }
      else             { if (b.val) b.val.textContent = pct + '%' }
    })
    // CPU percent badge
    const cpuPct = document.getElementById('hud-cpu-pct')
    if (cpuPct && bars.cpu.fill) cpuPct.textContent = bars.cpu.fill.style.width
    const memPct = document.getElementById('val-mem-pct')
    if (memPct && bars.mem.fill) memPct.textContent = bars.mem.fill.style.width
  }

  function updateHistograms (t) {
    // Shift CPU histogram and push new sample
    if (Math.floor(t * 2) !== Math.floor((t - 0.05) * 2)) {
      histCpuData.shift()
      histCpuData.push(20 + 60 * (0.5 + 0.5 * Math.sin(t * 0.31)) + (Math.random() - 0.5) * 20)
    }
    const cpuBars = document.querySelectorAll('#hist-cpu .hud-hist-bar')
    const maxC = Math.max(...histCpuData)
    cpuBars.forEach((el, i) => { el.style.height = ((histCpuData[i] / maxC) * 100).toFixed(0) + '%' })

    // Audio histogram — mirrors waveform activity
    if (Math.floor(t * 4) !== Math.floor((t - 0.05) * 4)) {
      histAudData.shift()
      const isActive = document.body.classList.contains('state-speaking') || document.body.classList.contains('state-thinking')
      histAudData.push(isActive ? 40 + Math.random() * 55 : 5 + Math.random() * 30)
    }
    const audBars = document.querySelectorAll('#hist-audio .hud-hist-bar')
    const maxA = Math.max(...histAudData, 1)
    audBars.forEach((el, i) => { el.style.height = ((histAudData[i] / maxA) * 100).toFixed(0) + '%' })
  }

  function updateNetStats (t) {
    const latEl  = document.getElementById('val-latency')
    const pktEl  = document.getElementById('val-packets')
    if (latEl) latEl.textContent = Math.floor(8 + 10 * Math.abs(Math.sin(t * 0.4))) + 'ms'
    if (pktEl) {
      _tokenCount += Math.floor(Math.random() * 3)
      pktEl.textContent = _tokenCount.toLocaleString()
    }
    const reqEl = document.getElementById('val-requests')
    if (reqEl) reqEl.textContent = _requestCount
    const tokEl = document.getElementById('val-tokens')
    if (tokEl) tokEl.textContent = Math.floor(_tokenCount / 10).toLocaleString() + 'k'
  }

  // Hook into message count from WebSocket
  window._incSidePanelRequest = () => { _requestCount++ }

  let lastT = 0
  function tick () {
    const t = (Date.now() - T0) / 1000
    if (t - lastT >= 0.05) {   // 20fps for data is plenty
      lastT = t
      updateBars(t)
      updateHistograms(t)
      updateNetStats(t)
    }
    requestAnimationFrame(tick)
  }
  tick()
})()

