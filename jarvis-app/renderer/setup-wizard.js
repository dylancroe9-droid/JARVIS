/* ── JARVIS First-Run Setup Wizard ─────────────────────────────────────────────
 * Loaded before app.js. Calls /setup/status; if a key is already saved (and
 * profile marks setup_complete), hides itself and lets the app boot normally.
 *
 * Steps:
 *   0  Welcome
 *   1  Pick AI provider
 *   2  Paste + live-validate API key
 *   3  Tell JARVIS about yourself (name + city)
 *   4  macOS permissions
 *   5  Done / Relaunch
 *
 * After completing, the wizard calls window.jarvis.relaunch() so the server
 * restarts with the new .env values loaded.
 *
 * window.showSetupWizard() is exposed so app.js can re-trigger the wizard
 * when the server broadcasts { type: "show_first_run" } (e.g. "go back to
 * setup" voice command).
 */
(() => {
  const PORT = 8765
  const API  = `http://127.0.0.1:${PORT}/setup`

  const PROVIDERS = {
    anthropic: {
      name:        'Claude (Anthropic)',
      tagline:     'Smartest reasoning, best tool use. Pay-as-you-go.',
      console:     'https://console.anthropic.com/settings/keys',
      prefix:      'sk-ant-',
      placeholder: 'sk-ant-…',
      payloadKey:  'anthropic_key',
      badge:       'BEST',
      badgeColor:  '#7feaff',
    },
    groq: {
      name:        'Llama 3.3 (Groq)',
      tagline:     'Free tier available. Fast — great for getting started.',
      console:     'https://console.groq.com/keys',
      prefix:      'gsk_',
      placeholder: 'gsk_…',
      payloadKey:  'groq_key',
      badge:       'FREE',
      badgeColor:  '#7fffb0',
    },
  }

  // ── CSS ─────────────────────────────────────────────────────────────────────
  function injectStyles () {
    const s = document.createElement('style')
    s.textContent = `
      #setup-wizard {
        display: none;
        position: fixed; inset: 0; z-index: 99999;
        background: #000;
        font-family: -apple-system, 'SF Pro Display', sans-serif;
        color: #e0e6f0;
        overflow: hidden;
      }
      #setup-wizard.visible { display: flex; align-items: center; justify-content: center; }

      /* Animated scan-line / pulse background */
      #setup-wizard::before {
        content: '';
        position: absolute; inset: 0;
        background:
          repeating-linear-gradient(
            0deg,
            transparent,
            transparent 3px,
            rgba(0,212,255,0.018) 3px,
            rgba(0,212,255,0.018) 4px
          );
        pointer-events: none;
        animation: sw-scan 8s linear infinite;
      }
      @keyframes sw-scan {
        from { background-position: 0 0; }
        to   { background-position: 0 100vh; }
      }

      .sw-shell {
        position: relative; z-index: 1;
        width: min(460px, 92vw);
        padding: 40px 44px 36px;
        background: rgba(6,16,28,0.96);
        border: 1px solid rgba(0,212,255,0.20);
        border-radius: 14px;
        box-shadow: 0 0 60px rgba(0,212,255,0.08), 0 24px 60px rgba(0,0,0,0.7);
      }

      .sw-logo {
        font-size: 11px;
        letter-spacing: 0.22em;
        color: rgba(0,212,255,0.65);
        text-transform: uppercase;
        margin-bottom: 28px;
      }

      .sw-progress {
        display: flex; gap: 6px; margin-bottom: 32px;
      }
      .sw-pip {
        height: 3px; flex: 1;
        background: rgba(255,255,255,0.08);
        border-radius: 2px;
        transition: background 0.3s;
      }
      .sw-pip.active { background: rgba(0,212,255,0.55); }
      .sw-pip.done   { background: rgba(0,212,255,0.90); }

      .sw-step { display: none; }
      .sw-step.active { display: block; }

      .sw-step h1 {
        font-size: 26px; font-weight: 600;
        color: #fff; margin: 0 0 6px;
        letter-spacing: -0.3px;
      }
      .sw-step h2 {
        font-size: 14px; font-weight: 400;
        color: rgba(200,220,255,0.55);
        margin: 0 0 22px;
      }
      .sw-step p {
        font-size: 14px; line-height: 1.6;
        color: rgba(200,220,255,0.70);
        margin: 0 0 16px;
      }

      .sw-card {
        border: 1px solid rgba(255,255,255,0.09);
        border-radius: 10px;
        padding: 14px 16px;
        margin-bottom: 10px;
        cursor: pointer;
        transition: border-color 0.18s, background 0.18s;
        background: rgba(255,255,255,0.03);
      }
      .sw-card:hover { border-color: rgba(0,212,255,0.35); background: rgba(0,212,255,0.05); }
      .sw-card.selected { border-color: rgba(0,212,255,0.70); background: rgba(0,212,255,0.08); }
      .sw-card .title { font-size: 14px; font-weight: 600; color: #cce8ff; margin-bottom: 4px; display: flex; align-items: center; gap: 8px; }
      .sw-card .desc  { font-size: 12px; color: rgba(180,210,255,0.55); line-height: 1.5; }

      .badge {
        font-size: 9px; font-weight: 700;
        letter-spacing: 0.12em; text-transform: uppercase;
        padding: 2px 6px; border-radius: 3px;
        background: rgba(127,255,176,0.18); color: #7fffb0;
      }

      .sw-label {
        display: block; font-size: 10px; font-weight: 600;
        letter-spacing: 0.14em; text-transform: uppercase;
        color: rgba(0,212,255,0.7);
        margin-bottom: 7px; margin-top: 18px;
      }
      .sw-label:first-of-type { margin-top: 0; }
      .sw-hint {
        font-size: 11px; color: rgba(160,200,255,0.45);
        margin-top: 5px; line-height: 1.5;
      }

      input[type=text], input[type=password] {
        width: 100%; box-sizing: border-box;
        padding: 10px 13px;
        background: rgba(255,255,255,0.05);
        border: 1px solid rgba(255,255,255,0.12);
        border-radius: 8px;
        color: #d8eaff; font-size: 14px; font-family: inherit;
        outline: none; transition: border-color 0.18s;
      }
      input:focus { border-color: rgba(0,212,255,0.55); background: rgba(0,212,255,0.05); }

      .sw-row { display: flex; align-items: center; gap: 10px; }

      button {
        padding: 9px 18px;
        font-family: inherit; font-size: 12px; font-weight: 600;
        letter-spacing: 0.08em; text-transform: uppercase;
        border-radius: 7px; cursor: pointer;
        transition: all 0.16s; border: 1px solid;
        background: rgba(0,212,255,0.14);
        color: #7feaff; border-color: rgba(0,212,255,0.35);
      }
      button:hover:not(:disabled) { background: rgba(0,212,255,0.26); color: #fff; border-color: rgba(0,212,255,0.65); }
      button:disabled { opacity: 0.32; cursor: not-allowed; }
      button.primary { background: rgba(0,212,255,0.22); color: #c0f0ff; border-color: rgba(0,212,255,0.55); }
      button.primary:hover:not(:disabled) { background: rgba(0,212,255,0.38); color: #fff; }
      button.ghost { background: transparent; color: rgba(200,220,255,0.45); border-color: rgba(255,255,255,0.12); }
      button.ghost:hover:not(:disabled) { color: rgba(200,220,255,0.80); border-color: rgba(255,255,255,0.28); }
      button.spacer { margin-left: auto; }

      .sw-actions { display: flex; align-items: center; margin-top: 26px; gap: 10px; flex-wrap: wrap; }

      .sw-result {
        font-size: 12px; font-weight: 500;
        transition: color 0.2s;
      }
      .sw-result.busy { color: rgba(200,220,255,0.50); }
      .sw-result.ok   { color: #7fffb0; }
      .sw-result.err  { color: #ff8090; }

      .sw-link {
        color: rgba(0,212,255,0.80); cursor: pointer;
        text-decoration: none; border-bottom: 1px solid rgba(0,212,255,0.30);
        transition: color 0.15s;
      }
      .sw-link:hover { color: #fff; border-color: rgba(0,212,255,0.70); }

      .sw-perm {
        display: flex; align-items: center; justify-content: space-between;
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 8px; padding: 12px 14px; margin-bottom: 8px;
        background: rgba(255,255,255,0.02);
      }
      .sw-perm .info .name { font-size: 13px; font-weight: 600; color: #c8dfff; }
      .sw-perm .info .why  { font-size: 11px; color: rgba(180,210,255,0.45); margin-top: 2px; }
      .sw-perm button { padding: 6px 14px; font-size: 10px; }

      /* Done / check step */
      .sw-check { font-size: 54px; text-align: center; margin: 10px 0 18px; }
      #sw-done-name { color: rgba(0,212,255,0.90); }
    `
    document.head.appendChild(s)
  }

  // ── HTML ────────────────────────────────────────────────────────────────────
  function html () {
    return `
      <div class="sw-logo">◈ J.A.R.V.I.S — INITIAL CONFIGURATION</div>
      <div class="sw-progress">
        <div class="sw-pip" data-pip="0"></div>
        <div class="sw-pip" data-pip="1"></div>
        <div class="sw-pip" data-pip="2"></div>
        <div class="sw-pip" data-pip="3"></div>
        <div class="sw-pip" data-pip="4"></div>
      </div>

      <!-- Step -1 — invite gate ──────────────────────────── -->
      <div class="sw-step" data-step="-1">
        <h1>Access required.</h1>
        <h2>Enter the code Dylan gave you.</h2>
        <label class="sw-label">Access code</label>
        <input type="password" id="sw-invite-input" autocomplete="off" spellcheck="false" placeholder="••••••••••••••" maxlength="40" />
        <div id="sw-invite-error" style="color:#ff8090;font-size:12px;margin-top:10px;min-height:18px"></div>
        <div class="sw-actions">
          <button class="primary spacer" id="sw-invite-submit">Continue →</button>
        </div>
      </div>

      <!-- Step 0 — welcome ───────────────────────────────── -->
      <div class="sw-step" data-step="0">
        <h1>Welcome.</h1>
        <h2>Five minutes to your first conversation.</h2>
        <p>JARVIS needs an API key from an AI provider so it can think and speak.
           We'll open the provider's console for you — paste the key back here
           and we'll validate it before saving anything.</p>
        <p>Your key is stored locally in <code>~/JARVIS/.env</code> and never
           sent anywhere except directly to the provider.</p>
        <div class="sw-actions">
          <button class="primary spacer" data-go="1">Get started →</button>
        </div>
      </div>

      <!-- Step 1 — pick provider ─────────────────────────── -->
      <div class="sw-step" data-step="1">
        <h1>Pick a brain.</h1>
        <h2>You can change this later from Settings.</h2>
        <div class="sw-card" data-provider="groq">
          <div class="title">Llama 3.3 (Groq) <span class="badge">FREE</span></div>
          <div class="desc">Free tier, very fast. Perfect for getting started — no credit card required.</div>
        </div>
        <div class="sw-card" data-provider="anthropic">
          <div class="title">Claude (Anthropic) <span class="badge" style="background:rgba(0,212,255,0.18);color:#7feaff">BEST</span></div>
          <div class="desc">Most capable reasoning and tool use. Pay-as-you-go after a small free trial.</div>
        </div>
        <div class="sw-card" data-provider="demo">
          <div class="title">Try without a key <span class="badge" style="background:rgba(160,100,255,0.18);color:#c0a0ff">DEMO</span></div>
          <div class="desc">Hear JARVIS speak and explore the interface. Canned responses only — no real conversation, no tools.</div>
        </div>
        <div class="sw-actions">
          <button class="ghost" data-go="0">← Back</button>
          <button class="primary spacer" id="sw-pick-next" disabled>Next →</button>
        </div>
      </div>

      <!-- Step 2 — paste + validate key ──────────────────── -->
      <div class="sw-step" data-step="2">
        <h1 id="sw-key-title">Add your key.</h1>
        <h2 id="sw-key-sub">We'll validate it live before saving.</h2>
        <p>
          1. <a class="sw-link" id="sw-open-console">Open the provider's console</a>
             and create a new API key.<br/>
          2. Paste it below, then hit Validate.
        </p>
        <label class="sw-label">API key</label>
        <input type="password" id="sw-key-input" autocomplete="off" spellcheck="false" placeholder="" />
        <div class="sw-row" style="margin-top:11px">
          <button id="sw-validate" disabled>Validate</button>
          <div id="sw-key-result" class="sw-result"></div>
        </div>
        <div class="sw-actions">
          <button class="ghost" data-go="1">← Back</button>
          <button class="primary spacer" id="sw-key-next" disabled>Next →</button>
        </div>
      </div>

      <!-- Step 3 — about you ─────────────────────────────── -->
      <div class="sw-step" data-step="3">
        <h1>Tell me about you.</h1>
        <h2>JARVIS uses this to personalize responses.</h2>
        <label class="sw-label">Your first name</label>
        <input type="text" id="sw-name-input" autocomplete="given-name" placeholder="e.g. Alex" maxlength="40" />
        <label class="sw-label">Your city</label>
        <input type="text" id="sw-city-input" autocomplete="address-level2" placeholder="e.g. Atlanta" maxlength="60" />
        <p class="sw-hint">Used for local weather and time-aware greetings. Both fields are optional — skip if you prefer.</p>
        <div class="sw-actions">
          <button class="ghost" data-go="2">← Back</button>
          <button class="primary spacer" data-go="4">Next →</button>
        </div>
      </div>

      <!-- Step 4 — macOS permissions ─────────────────────── -->
      <div class="sw-step" data-step="4">
        <h1>Grant macOS permissions.</h1>
        <h2>JARVIS needs these to listen, see, and run hotkeys.</h2>
        <div class="sw-perm">
          <div class="info"><div class="name">Microphone</div><div class="why">For voice commands — the main way to talk to JARVIS.</div></div>
          <button data-pref="microphone">Open</button>
        </div>
        <div class="sw-perm">
          <div class="info"><div class="name">Accessibility</div><div class="why">For the ⌘⇧J global hotkey.</div></div>
          <button data-pref="accessibility">Open</button>
        </div>
        <div class="sw-perm">
          <div class="info"><div class="name">Screen Recording</div><div class="why">So JARVIS can read your screen when you ask.</div></div>
          <button data-pref="screen">Open</button>
        </div>
        <div class="sw-perm">
          <div class="info"><div class="name">Calendar</div><div class="why">So JARVIS can read your daily schedule.</div></div>
          <button data-pref="calendars">Open</button>
        </div>
        <div class="sw-perm">
          <div class="info"><div class="name">Camera</div><div class="why">For the live HUD feed and gesture controls.</div></div>
          <button data-pref="camera">Open</button>
        </div>
        <div class="sw-actions">
          <button class="ghost" data-go="3">← Back</button>
          <button class="primary spacer" id="sw-finish">Finish →</button>
        </div>
      </div>

      <!-- Final — success ────────────────────────────────── -->
      <div class="sw-step" data-step="final">
        <div class="sw-check">✓</div>
        <h1>You're set, <span id="sw-done-name">friend</span>.</h1>
        <p style="text-align:center;color:rgba(180,220,255,0.60)">
          JARVIS will relaunch now to load your settings.<br/>
          Say "go back to setup" any time to reconfigure.
        </p>
        <div class="sw-actions" style="justify-content:center">
          <button class="primary" id="sw-relaunch">Launch JARVIS →</button>
        </div>
      </div>
    `
  }

  const INVITE_CODE = 'DylanTheGoat'
  const INVITE_KEY  = 'jarvis_invite_verified'

  // ── State ───────────────────────────────────────────────────────────────────
  const state = {
    step:          -1,   // -1 = invite gate
    inviteOk:      false,
    provider:      null,
    keyValid:      false,
    keyValue:      '',
    nameValue:     '',
    cityValue:     '',
  }

  let root = null

  function esc (str) {
    return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')
  }

  function showStep (step) {
    state.step = step
    root.querySelectorAll('.sw-step').forEach(el => {
      el.classList.toggle('active', el.dataset.step === String(step))
    })
    // Hide progress pips on invite gate
    const pipsEl = root.querySelector('.sw-progress')
    if (pipsEl) pipsEl.style.visibility = (step === -1) ? 'hidden' : 'visible'
    root.querySelectorAll('.sw-pip').forEach(pip => {
      const i = Number(pip.dataset.pip)
      pip.classList.remove('done', 'active')
      if (step === 'final' || i < step) pip.classList.add('done')
      else if (i === step)              pip.classList.add('active')
    })
    // Focus first input if present
    const firstInput = root.querySelector(`.sw-step[data-step="${step}"] input`)
    if (firstInput) setTimeout(() => firstInput.focus(), 80)
  }

  function selectProvider (key) {
    state.provider = key
    state.keyValid  = false
    root.querySelectorAll('.sw-card').forEach(c =>
      c.classList.toggle('selected', c.dataset.provider === key)
    )
    root.querySelector('#sw-pick-next').disabled = !key

    if (key === 'demo') return
    const prov = PROVIDERS[key]
    if (!prov) return
    root.querySelector('#sw-key-title').textContent = `Add your ${prov.name} key.`
    root.querySelector('#sw-key-input').placeholder = prov.placeholder
    const link = root.querySelector('#sw-open-console')
    link.textContent = prov.console
    link.dataset.url = prov.console
  }

  async function enableDemoMode () {
    try {
      const r = await fetch(`${API}/enable-demo`, { method: 'POST' })
      const j = await r.json()
      return !!j.ok
    } catch (e) { return false }
  }

  async function validateKey () {
    const input  = root.querySelector('#sw-key-input')
    const result = root.querySelector('#sw-key-result')
    const next   = root.querySelector('#sw-key-next')
    const btn    = root.querySelector('#sw-validate')
    const key    = input.value.trim()
    if (!state.provider || !key) return

    btn.disabled = true
    result.className = 'sw-result busy'
    result.textContent = 'Checking…'

    try {
      const r = await fetch(`${API}/validate-key`, {
        method:  'POST',
        headers: { 'content-type': 'application/json' },
        body:    JSON.stringify({ provider: state.provider, key }),
      })
      const j = await r.json()
      if (j.ok) {
        state.keyValid = true
        state.keyValue = key
        result.className = 'sw-result ok'
        result.textContent = '✓ ' + (j.message || 'Key works.')
        next.disabled = false
      } else {
        state.keyValid = false
        result.className = 'sw-result err'
        result.textContent = '✗ ' + (j.message || 'Could not validate key.')
        next.disabled = true
      }
    } catch (e) {
      state.keyValid = false
      result.className = 'sw-result err'
      result.textContent = '✗ Could not reach the JARVIS backend.'
      next.disabled = true
    } finally {
      btn.disabled = false
    }
  }

  async function openPref (pane) {
    try {
      await fetch(`${API}/open-pref`, {
        method:  'POST',
        headers: { 'content-type': 'application/json' },
        body:    JSON.stringify({ pane }),
      })
    } catch (e) { /* best-effort */ }
  }

  async function saveAndFinish () {
    const finishBtn = root.querySelector('#sw-finish')
    finishBtn.disabled = true

    if (state.provider === 'demo') {
      const ok = await enableDemoMode()
      if (!ok) {
        finishBtn.disabled = false
        alert('Could not enable demo mode — check that the JARVIS server is running.')
        return
      }
      // For demo mode, save name/city to profile but no key needed
      await saveProfile()
      showFinal()
      return
    }

    const payload = {}
    payload[PROVIDERS[state.provider].payloadKey] = state.keyValue
    if (state.nameValue) payload.user_name  = state.nameValue
    if (state.cityValue) payload.user_city  = state.cityValue

    try {
      const r = await fetch(`${API}/save`, {
        method:  'POST',
        headers: { 'content-type': 'application/json' },
        body:    JSON.stringify(payload),
      })
      const j = await r.json()
      if (!j.ok) {
        alert('Could not save: ' + (j.message || 'unknown error'))
        finishBtn.disabled = false
        return
      }
      showFinal()
    } catch (e) {
      alert('Could not reach the JARVIS backend to save.')
      finishBtn.disabled = false
    }
  }

  async function saveProfile () {
    // Called when demo mode wants to save name/city without a key
    try {
      await fetch(`${API}/save`, {
        method:  'POST',
        headers: { 'content-type': 'application/json' },
        body:    JSON.stringify({
          user_name: state.nameValue || undefined,
          user_city: state.cityValue || undefined,
        }),
      })
    } catch (e) { /* best-effort */ }
  }

  function showFinal () {
    const nameEl = root.querySelector('#sw-done-name')
    if (nameEl) nameEl.textContent = state.nameValue ? esc(state.nameValue) : 'friend'
    showStep('final')
  }

  function relaunch () {
    if (window.jarvis && window.jarvis.relaunch) {
      window.jarvis.relaunch()
    } else {
      alert('Please quit and reopen JARVIS to load your new settings.')
    }
  }

  // ── Invite gate ─────────────────────────────────────────────────────────────
  function checkInvite () {
    const input = root.querySelector('#sw-invite-input')
    const err   = root.querySelector('#sw-invite-error')
    const val   = (input ? input.value : '').trim()
    if (val === INVITE_CODE) {
      try { localStorage.setItem(INVITE_KEY, '1') } catch (e) {}
      state.inviteOk = true
      err.textContent = ''
      showStep(0)
    } else {
      err.textContent = 'Incorrect code. Ask Dylan for the access code.'
      if (input) { input.value = ''; input.focus() }
    }
  }

  function isInviteVerified () {
    try { return localStorage.getItem(INVITE_KEY) === '1' } catch (e) { return false }
  }

  // ── Click / input wiring ────────────────────────────────────────────────────
  function wire () {
    root.addEventListener('click', e => {
      const t = e.target.closest('[data-go], [data-provider], [data-pref], #sw-validate, #sw-open-console, #sw-key-next, #sw-finish, #sw-relaunch, #sw-invite-submit')
      if (!t) return

      if (t.matches('#sw-invite-submit')) { checkInvite(); return }

      if (t.matches('[data-go]')) {
        const next = Number(t.dataset.go)
        // Going to key step — guard: must have a provider
        if (next === 2 && !state.provider) return
        // Demo: skip key step, go straight to "about you"
        if (next === 2 && state.provider === 'demo') {
          enableDemoMode().then(ok => {
            if (ok) showStep(3)
            else alert('Could not enable demo mode — is the JARVIS server running?')
          })
          return
        }
        showStep(next)
        return
      }

      if (t.matches('[data-provider]')) {
        selectProvider(t.closest('.sw-card').dataset.provider)
        return
      }

      if (t.matches('[data-pref]')) { openPref(t.dataset.pref); return }

      if (t.matches('#sw-validate'))  { validateKey(); return }

      if (t.matches('#sw-open-console')) {
        e.preventDefault()
        const url = t.dataset.url
        if (url) {
          if (window.jarvis && window.jarvis.openExternal) window.jarvis.openExternal(url)
          else window.open(url, '_blank')
        }
        return
      }

      if (t.matches('#sw-key-next')) {
        if (!state.keyValid) return
        showStep(3)
        return
      }

      if (t.matches('#sw-finish')) { saveAndFinish(); return }
      if (t.matches('#sw-relaunch')) { relaunch(); return }
    })

    // Invite gate Enter key
    const inviteIn = root.querySelector('#sw-invite-input')
    if (inviteIn) {
      inviteIn.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); checkInvite() }
      })
      setTimeout(() => inviteIn.focus(), 80)
    }

    // Key input → enable Validate button when format looks right
    const keyIn = root.querySelector('#sw-key-input')
    keyIn.addEventListener('input', () => {
      const v    = keyIn.value.trim()
      const prov = state.provider && PROVIDERS[state.provider]
      const ok   = prov && v.startsWith(prov.prefix) && v.length > prov.prefix.length + 4
      root.querySelector('#sw-validate').disabled = !ok
      root.querySelector('#sw-key-result').textContent = ''
      root.querySelector('#sw-key-result').className = 'sw-result'
      root.querySelector('#sw-key-next').disabled = true
      state.keyValid = false
    })
    keyIn.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); validateKey() }
    })

    // Name / city fields
    root.querySelector('#sw-name-input').addEventListener('input', e => {
      state.nameValue = e.target.value
    })
    root.querySelector('#sw-city-input').addEventListener('input', e => {
      state.cityValue = e.target.value
    })
  }

  // ── Show / hide wizard ──────────────────────────────────────────────────────
  function show (resetSteps = false) {
    if (!root) {
      root = document.getElementById('setup-wizard')
      if (!root) return
    }
    root.innerHTML = html()
    wire()
    // Hide the main app while wizard is up
    const appEl = document.getElementById('app')
    if (appEl) appEl.style.display = 'none'
    root.classList.add('visible')

    // Wrap content in the shell div if not already
    if (!root.querySelector('.sw-shell')) {
      const inner = root.innerHTML
      root.innerHTML = `<div class="sw-shell">${inner}</div>`
      wire() // re-wire after DOM replace
    }

    if (resetSteps) {
      state.step     = 0
      state.provider = null
      state.keyValid = false
      state.keyValue = ''
    }
    showStep(state.step)
  }

  function hide () {
    if (root) root.classList.remove('visible')
    const appEl = document.getElementById('app')
    if (appEl) appEl.style.display = ''
  }

  // ── Public API (called by app.js WS handler) ────────────────────────────────
  window.showSetupWizard  = (reset = false) => show(reset)
  window.hideSetupWizard  = hide

  // ── Boot ────────────────────────────────────────────────────────────────────
  async function checkStatus () {
    for (let i = 0; i < 30; i++) {
      try {
        const r = await fetch(`${API}/status`)
        if (r.ok) return await r.json()
      } catch (e) { /* server still starting */ }
      await new Promise(res => setTimeout(res, 350))
    }
    return null
  }

  async function boot () {
    const status = await checkStatus()
    if (!status) {
      console.warn('[setup] Could not reach setup API — skipping wizard.')
      return
    }
    if (status.configured) {
      // .env already has a key — skip wizard entirely
      return
    }
    // No key yet → show wizard (invite gate first if not already verified)
    root = document.getElementById('setup-wizard')
    if (!root) return
    injectStyles()
    root.innerHTML = `<div class="sw-shell">${html()}</div>`
    root.classList.add('visible')
    const appEl = document.getElementById('app')
    if (appEl) appEl.style.display = 'none'
    wire()
    // If invite already verified (localStorage), skip straight to step 0
    if (isInviteVerified()) {
      showStep(0)
    } else {
      showStep(-1)
    }
  }

  // Inject styles once at startup (also called inside show() for re-triggers)
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => { injectStyles(); boot() })
  } else {
    injectStyles()
    boot()
  }
})()
