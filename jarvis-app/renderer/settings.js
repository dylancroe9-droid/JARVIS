/* ── Settings panel ─────────────────────────────────────────────────────────
 * In-app settings. Reuses setup-wizard styles for visual coherence.
 * Calls /setup/all-settings + /setup/save-settings on the Python backend.
 *
 * Sections:
 *   - Identity (name, address)
 *   - Brain (Anthropic + Groq keys, model selection)
 *   - Voice (TTS voice, rate, ElevenLabs key)
 *   - Speech recognition (Whisper model)
 *   - Wake words
 *   - Paths (projects dir)
 *
 * Save writes ~/JARVIS/.env in place; some changes need a relaunch (clearly
 * labeled in the UI).
 */
;(function () {
  const PORT = (window.jarvis && window.jarvis.port) || 8765
  const API  = `http://127.0.0.1:${PORT}/setup`

  const state = { open: false, loaded: null }
  let root = null

  // ── HTML ──────────────────────────────────────────────────────────────────
  function html () {
    return `
      <div class="sp-shell">
        <div class="sp-titlebar">
          <div class="sp-logo">⚙ SETTINGS</div>
          <button class="sp-close" id="sp-close" title="Close">✕</button>
        </div>
        <div class="sp-body">
          <section class="sp-section">
            <h3>Identity</h3>
            <label>Your name</label>
            <input id="f-user_name" type="text" placeholder="What should JARVIS call you?" />
            <label>Address (how JARVIS greets you)</label>
            <input id="f-user_address" type="text" placeholder="e.g. Mr. Roe, Dylan, sir" />
          </section>

          <section class="sp-section">
            <h3>Brain — provider keys</h3>
            <label>Anthropic API key <span id="m-anthropic_key" class="sp-mask"></span></label>
            <input id="f-anthropic_key" type="password" placeholder="sk-ant-…  (leave blank to keep current)" />
            <label>Groq API key <span id="m-groq_key" class="sp-mask"></span></label>
            <input id="f-groq_key" type="password" placeholder="gsk_…  (leave blank to keep current)" />
            <label>Anthropic model</label>
            <select id="f-anthropic_model">
              <option value="claude-opus-4-5">Claude Opus 4.5 (best)</option>
              <option value="claude-sonnet-4-5">Claude Sonnet 4.5 (balanced)</option>
              <option value="claude-haiku-4-5-20251001">Claude Haiku 4.5 (fastest, cheapest)</option>
            </select>
            <label>Groq model</label>
            <select id="f-groq_model">
              <option value="llama-3.3-70b-versatile">Llama 3.3 70B (default)</option>
              <option value="llama-3.1-8b-instant">Llama 3.1 8B (fastest)</option>
              <option value="mixtral-8x7b-32768">Mixtral 8x7B</option>
            </select>
          </section>

          <section class="sp-section">
            <h3>Voice — speech output</h3>
            <label>ElevenLabs API key <small>(optional — best-quality voice)</small> <span id="m-elevenlabs_key" class="sp-mask"></span></label>
            <input id="f-elevenlabs_key" type="password" placeholder="ek_… or 32-hex string  (leave blank to keep current)" />
            <label>macOS fallback voice</label>
            <select id="f-tts_voice">
              <option value="Daniel">Daniel (British, default)</option>
              <option value="Alex">Alex (American)</option>
              <option value="Samantha">Samantha</option>
              <option value="Karen">Karen (Australian)</option>
              <option value="Moira">Moira (Irish)</option>
              <option value="Tessa">Tessa (S. African)</option>
              <option value="Oliver">Oliver</option>
            </select>
            <label>Speaking rate (words / minute)</label>
            <input id="f-tts_rate" type="number" min="120" max="260" step="5" />
          </section>

          <section class="sp-section">
            <h3>Speech recognition</h3>
            <label>Whisper model <small>(smaller = faster, less accurate)</small></label>
            <select id="f-whisper_model">
              <option value="tiny">tiny — fastest (~150ms/sentence)</option>
              <option value="base">base — balanced (default, ~400ms)</option>
              <option value="small">small — most accurate (~900ms)</option>
            </select>
          </section>

          <section class="sp-section">
            <h3>Wake words</h3>
            <label>Comma-separated phrases that wake JARVIS</label>
            <input id="f-wake_words" type="text" placeholder="jarvis, hey jarvis, ok jarvis" />
            <small class="sp-hint">Lowercase, no punctuation. Shorter phrases are more sensitive.</small>
          </section>

          <section class="sp-section">
            <h3>Paths</h3>
            <label>Default projects directory</label>
            <input id="f-projects_dir" type="text" placeholder="~/Projects" />
          </section>
        </div>
        <div class="sp-footer">
          <span class="sp-status" id="sp-status"></span>
          <span style="flex:1"></span>
          <button class="sp-ghost" id="sp-cancel">Cancel</button>
          <button class="sp-primary" id="sp-save">Save</button>
        </div>
      </div>
    `
  }

  // ── Style (single-shot) ──────────────────────────────────────────────────
  function ensureStyle () {
    if (document.getElementById('sp-style')) return
    const s = document.createElement('style')
    s.id = 'sp-style'
    s.textContent = `
      #settings-panel {
        position: fixed; inset: 0; z-index: 99998;
        display: none; align-items: center; justify-content: center;
        background: rgba(0, 4, 14, 0.65);
        backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
        font-family: 'SF Mono', 'Menlo', monospace; font-size: 12px;
        color: #cce8f8;
      }
      #settings-panel.visible { display: flex; }
      .sp-shell {
        width: min(96vw, 480px); max-height: 92vh;
        display: flex; flex-direction: column;
        background: linear-gradient(180deg, #001428 0%, #000810 100%);
        border: 1px solid rgba(0, 212, 255, 0.32);
        border-radius: 12px; overflow: hidden;
        box-shadow: 0 20px 60px rgba(0,0,0,0.60), 0 0 0 1px rgba(0,212,255,0.10);
      }
      .sp-titlebar {
        display: flex; align-items: center; justify-content: space-between;
        padding: 12px 16px; border-bottom: 1px solid rgba(0,212,255,0.15);
      }
      .sp-logo { font-size: 12px; letter-spacing: 0.18em; color: #00d4ff; }
      .sp-close {
        background: none; border: none; color: rgba(160,210,240,0.55);
        font-size: 16px; cursor: pointer; padding: 4px 8px; border-radius: 4px;
      }
      .sp-close:hover { color: #fff; background: rgba(255,255,255,0.06); }
      .sp-body { padding: 4px 0; overflow-y: auto; flex: 1; }
      .sp-section { padding: 14px 18px; border-bottom: 1px solid rgba(0,212,255,0.08); }
      .sp-section:last-child { border-bottom: none; }
      .sp-section h3 {
        font-family: -apple-system, sans-serif; font-size: 11px;
        text-transform: uppercase; letter-spacing: 0.14em;
        color: rgba(0, 212, 255, 0.85); margin-bottom: 10px; font-weight: 600;
      }
      .sp-section label {
        display: block; margin-top: 10px; margin-bottom: 4px;
        color: rgba(160, 210, 240, 0.75); font-size: 11px;
        text-transform: uppercase; letter-spacing: 0.06em;
      }
      .sp-section label small {
        text-transform: none; letter-spacing: 0;
        color: rgba(120, 160, 190, 0.60);
      }
      .sp-section input, .sp-section select {
        width: 100%; padding: 8px 10px; font-family: inherit; font-size: 12px;
        color: #cce8f8;
        background: rgba(0, 12, 30, 0.65);
        border: 1px solid rgba(0, 212, 255, 0.20);
        border-radius: 6px; outline: none;
      }
      .sp-section input:focus, .sp-section select:focus {
        border-color: rgba(0, 212, 255, 0.60);
        background: rgba(0, 16, 40, 0.85);
      }
      .sp-mask {
        font-size: 10px; color: rgba(0, 232, 122, 0.75);
        text-transform: none; letter-spacing: 0; margin-left: 6px;
        font-family: 'SF Mono', monospace;
      }
      .sp-hint { display: block; margin-top: 4px; color: rgba(120, 160, 190, 0.55); font-family: -apple-system, sans-serif; }
      .sp-footer {
        display: flex; align-items: center; gap: 10px;
        padding: 12px 16px;
        border-top: 1px solid rgba(0,212,255,0.15);
        background: rgba(0, 8, 22, 0.65);
      }
      .sp-status { font-size: 11px; color: rgba(160,210,240,0.65); }
      .sp-status.ok  { color: #00e87a; }
      .sp-status.err { color: #ff4060; }
      .sp-ghost, .sp-primary {
        padding: 7px 14px; font-family: inherit; font-size: 11px;
        text-transform: uppercase; letter-spacing: 0.10em;
        border-radius: 6px; cursor: pointer; transition: all 0.15s;
      }
      .sp-ghost {
        background: transparent; color: rgba(160,210,240,0.65);
        border: 1px solid rgba(0, 212, 255, 0.22);
      }
      .sp-ghost:hover { color: #fff; border-color: rgba(0, 212, 255, 0.40); }
      .sp-primary {
        background: rgba(0, 212, 255, 0.18); color: #7feaff;
        border: 1px solid rgba(0, 212, 255, 0.50);
      }
      .sp-primary:hover { background: rgba(0, 212, 255, 0.28); color: #fff; }
      .sp-primary:disabled { opacity: 0.4; cursor: not-allowed; }
    `
    document.head.appendChild(s)
  }

  // ── Data ──────────────────────────────────────────────────────────────────
  async function load () {
    try {
      const r = await fetch(`${API}/all-settings`)
      if (!r.ok) return null
      return await r.json()
    } catch (e) { return null }
  }

  function setField (id, val) {
    const el = root.querySelector(`#f-${id}`)
    if (!el) return
    if (el.tagName === 'SELECT') el.value = val ?? ''
    else el.value = val ?? ''
  }

  function setMask (id, val) {
    const el = root.querySelector(`#m-${id}`)
    if (el) el.textContent = val ? `(saved · ${val})` : ''
  }

  function fill (data) {
    setField('user_name',       data.user_name)
    setField('user_address',    data.user_address)
    setField('anthropic_model', data.anthropic_model)
    setField('groq_model',      data.groq_model)
    setField('whisper_model',   data.whisper_model)
    setField('tts_voice',       data.tts_voice)
    setField('tts_rate',        data.tts_rate)
    setField('wake_words',      data.wake_words)
    setField('projects_dir',    data.projects_dir)
    setMask('anthropic_key',    data.anthropic_key_set ? data.anthropic_key_mask : '')
    setMask('groq_key',         data.groq_key_set      ? data.groq_key_mask      : '')
    setMask('elevenlabs_key',   data.elevenlabs_key_set ? data.elevenlabs_key_mask : '')
  }

  function collect () {
    const v = id => root.querySelector(`#f-${id}`).value.trim()
    const payload = {
      user_name:       v('user_name'),
      user_address:    v('user_address'),
      anthropic_model: v('anthropic_model'),
      groq_model:      v('groq_model'),
      whisper_model:   v('whisper_model'),
      tts_voice:       v('tts_voice'),
      tts_rate:        Number(v('tts_rate')) || null,
      wake_words:      v('wake_words'),
      projects_dir:    v('projects_dir'),
    }
    // Only send key fields if non-empty (blank = keep current)
    const ak = v('anthropic_key');  if (ak) payload.anthropic_key  = ak
    const gk = v('groq_key');       if (gk) payload.groq_key       = gk
    const ek = v('elevenlabs_key'); if (ek) payload.elevenlabs_key = ek
    return payload
  }

  async function save () {
    const status = root.querySelector('#sp-status')
    const btn    = root.querySelector('#sp-save')
    btn.disabled = true
    status.className = 'sp-status'
    status.textContent = 'Saving…'
    try {
      const r = await fetch(`${API}/save-settings`, {
        method:  'POST',
        headers: { 'content-type': 'application/json' },
        body:    JSON.stringify(collect()),
      })
      const j = await r.json()
      if (j.ok) {
        status.className = 'sp-status ok'
        status.textContent = '✓ Saved — relaunch to apply.'
        // Refresh masks so the user can see the new keys are stored.
        const fresh = await load()
        if (fresh) fill(fresh)
      } else {
        status.className = 'sp-status err'
        status.textContent = '✗ ' + (j.message || 'Save failed.')
      }
    } catch (e) {
      status.className = 'sp-status err'
      status.textContent = '✗ Could not reach JARVIS backend.'
    } finally {
      btn.disabled = false
    }
  }

  function close () {
    state.open = false
    root.classList.remove('visible')
  }

  async function open () {
    if (state.open) return
    ensureStyle()
    if (!root) {
      root = document.getElementById('settings-panel')
      root.innerHTML = html()
      root.addEventListener('click', e => {
        if (e.target === root) close()
        if (e.target.id === 'sp-close')  close()
        if (e.target.id === 'sp-cancel') close()
        if (e.target.id === 'sp-save')   save()
      })
      document.addEventListener('keydown', e => {
        if (state.open && e.key === 'Escape') close()
      })
    }
    state.open = true
    root.classList.add('visible')
    const data = await load()
    if (data) {
      state.loaded = data
      fill(data)
    } else {
      const status = root.querySelector('#sp-status')
      status.className = 'sp-status err'
      status.textContent = '✗ Could not reach JARVIS backend.'
    }
  }

  // Wire the gear icon in the HUD header.
  function wireButton () {
    const btn = document.getElementById('btn-settings')
    if (btn) btn.addEventListener('click', open)
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wireButton)
  } else {
    wireButton()
  }

  // Public so other code (e.g. the wizard's "open settings later" button) can trigger it.
  window.jarvisSettings = { open, close }
})()
