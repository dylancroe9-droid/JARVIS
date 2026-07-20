/*
 * widgets.js — JARVIS widget dashboard
 * ------------------------------------------------------------------
 * A slot-based board of draggable widgets that sits over the HUD (the sphere
 * stays). Widgets have a clean default layout, can be dragged anywhere, hidden,
 * or restored — by hand OR by voice ("pull up the calendar", "get rid of this
 * one", "put everything back"). Layout persists in localStorage.
 *
 * Voice bridge: app.js routes {type:"widget_control", action, widget} here via
 * window._widgetControl(). Calendar data arrives via window._widgetsAgenda().
 *
 * Kept deliberately self-contained (injects its own CSS, builds its own DOM) so
 * it doesn't tangle with the 135 KB index.html.
 */
(function () {
  'use strict';

  const LS_KEY = 'jarvis_widgets_v2';

  // ── Registry: default position/size/visibility + how to render each widget ──
  // x/y are top/left in px; null x means "anchor to the right edge" at load.
  const REGISTRY = {
    time: {
      title: 'TIME', icon: '◷',
      def: { right: 28, y: 96, w: 250, h: 150, visible: true },
      render: renderTime,
    },
    calendar: {
      title: 'CALENDAR', icon: '▦',
      def: { right: 28, y: 270, w: 300, h: 360, visible: true },
      render: renderCalendar,
    },
    browser: {
      title: 'BROWSER', icon: '◱',
      def: { x: 140, y: 110, w: 760, h: 540, visible: false },
      render: renderBrowser,
    },
  };

  // Spoken aliases → widget id
  const ALIASES = {
    time: 'time', clock: 'time',
    calendar: 'calendar', agenda: 'calendar', schedule: 'calendar', events: 'calendar',
    browser: 'browser', web: 'browser', internet: 'browser',
    netflix: 'browser', peacock: 'browser', disney: 'browser', prime: 'browser',
  };

  let state = load();          // { [id]: {x,y,w,h,visible} }
  let lastFocused = null;      // for "this one"
  let agendaCache = [];        // last calendar events pushed from the server

  // ── CSS ────────────────────────────────────────────────────────────────────
  function injectCSS() {
    if (document.getElementById('jwidget-css')) return;
    const s = document.createElement('style');
    s.id = 'jwidget-css';
    s.textContent = `
    #widget-board { position:fixed; inset:0; z-index:640; pointer-events:none; }
    .jwidget {
      position:absolute; pointer-events:auto; display:flex; flex-direction:column;
      background:rgba(8,16,26,0.82); border:1px solid rgba(49,225,255,0.28);
      border-radius:14px; box-shadow:0 8px 34px rgba(0,0,0,0.5),
      inset 0 0 0 1px rgba(49,225,255,0.06); backdrop-filter:blur(9px);
      color:#dff6ff; font-family:-apple-system,'SF Pro Display',system-ui,sans-serif;
      overflow:hidden; transition:opacity .18s ease, transform .18s ease;
      animation:jw-in .22s ease;
    }
    @keyframes jw-in { from{opacity:0; transform:translateY(8px) scale(.98)} to{opacity:1} }
    .jwidget.jw-hiding { opacity:0; transform:translateY(8px) scale(.98); }
    .jw-head {
      display:flex; align-items:center; gap:8px; padding:9px 11px; cursor:grab;
      background:rgba(49,225,255,0.07); border-bottom:1px solid rgba(49,225,255,0.16);
      user-select:none; flex:0 0 auto;
    }
    .jw-head:active { cursor:grabbing; }
    .jw-icon { color:#31e1ff; font-size:13px; }
    .jw-title { font-size:11px; letter-spacing:.16em; color:#8fd8ea; flex:1; font-weight:600; }
    .jw-x { color:#5f8296; cursor:pointer; font-size:15px; line-height:1; padding:0 3px; }
    .jw-x:hover { color:#ff6b6b; }
    .jw-body { flex:1; overflow:auto; padding:12px; min-height:0; }
    .jw-body::-webkit-scrollbar { width:6px; } .jw-body::-webkit-scrollbar-thumb{ background:rgba(49,225,255,.2); border-radius:3px; }

    /* time */
    .jw-clock { font-size:32px; font-weight:200; letter-spacing:.02em; color:#eafcff; font-variant-numeric:tabular-nums; white-space:nowrap; }
    .jw-date  { font-size:12px; letter-spacing:.14em; color:#7fbdd0; margin-top:4px; text-transform:uppercase; }

    /* calendar */
    .jw-cal-month { text-align:center; font-size:12px; letter-spacing:.14em; color:#8fd8ea; margin-bottom:8px; text-transform:uppercase; }
    .jw-cal-grid { display:grid; grid-template-columns:repeat(7,1fr); gap:2px; }
    .jw-cal-dow { font-size:9px; color:#5f8296; text-align:center; padding:2px 0; }
    .jw-cal-day { font-size:11px; text-align:center; padding:5px 0; border-radius:6px; color:#bfe6f2; }
    .jw-cal-day.jw-today { background:#31e1ff; color:#04121a; font-weight:700; }
    .jw-cal-day.jw-empty { color:transparent; }
    .jw-agenda { margin-top:11px; border-top:1px solid rgba(49,225,255,.14); padding-top:9px; }
    .jw-ag-item { display:flex; gap:8px; margin-bottom:7px; font-size:12px; }
    .jw-ag-time { color:#31e1ff; flex:0 0 auto; font-variant-numeric:tabular-nums; }
    .jw-ag-title { color:#cfeefb; }
    .jw-ag-empty { color:#5f8296; font-size:12px; }

    /* browser */
    .jw-nav { display:flex; gap:5px; padding:7px 8px; background:rgba(0,0,0,.25); flex:0 0 auto; align-items:center; }
    .jw-nav button { background:rgba(49,225,255,.1); border:1px solid rgba(49,225,255,.22); color:#bfe6f2;
      border-radius:7px; padding:3px 8px; font-size:11px; cursor:pointer; white-space:nowrap; }
    .jw-nav button:hover { background:rgba(49,225,255,.22); }
    .jw-url { flex:1; background:rgba(0,0,0,.35); border:1px solid rgba(49,225,255,.18); color:#dff6ff;
      border-radius:7px; padding:3px 9px; font-size:11px; outline:none; }
    .jw-web { flex:1; border:0; width:100%; background:#000; }
    `;
    document.head.appendChild(s);
  }

  // ── Persistence ──────────────────────────────────────────────────────────
  function load() {
    try { return JSON.parse(localStorage.getItem(LS_KEY)) || {}; }
    catch { return {}; }
  }
  function save() {
    try { localStorage.setItem(LS_KEY, JSON.stringify(state)); } catch {}
  }
  function geom(id) {
    const d = REGISTRY[id].def;
    const s = state[id] || {};
    const w = s.w || d.w, h = s.h || d.h;
    const y = (s.y != null ? s.y : d.y);
    const visible = (s.visible != null) ? s.visible : d.visible;
    if (s.x != null)     return { x: s.x, y, w, h, visible };          // dragged → absolute left
    if (d.right != null) return { right: d.right, y, w, h, visible };  // default → CSS right-anchor
    return { x: (d.x != null ? d.x : 40), y, w, h, visible };
  }

  // ── Board ──────────────────────────────────────────────────────────────────
  function board() {
    let b = document.getElementById('widget-board');
    if (!b) { b = document.createElement('div'); b.id = 'widget-board'; document.body.appendChild(b); }
    return b;
  }

  function build(id) {
    const reg = REGISTRY[id];
    const g = geom(id);
    const el = document.createElement('div');
    el.className = 'jwidget'; el.id = 'jw-' + id;
    if (g.right != null) el.style.right = g.right + 'px';
    else el.style.left = g.x + 'px';
    el.style.top = g.y + 'px';
    el.style.width = g.w + 'px'; el.style.height = g.h + 'px';
    el.innerHTML =
      `<div class="jw-head"><span class="jw-icon">${reg.icon}</span>` +
      `<span class="jw-title">${reg.title}</span><span class="jw-x" title="hide">✕</span></div>` +
      `<div class="jw-body"></div>`;
    board().appendChild(el);
    el.querySelector('.jw-x').addEventListener('click', () => hide(id));
    el.addEventListener('mousedown', () => { lastFocused = id; });
    makeDraggable(el, id);
    try { reg.render(el.querySelector('.jw-body'), id); } catch (e) { console.error('widget render', id, e); }
    return el;
  }

  function makeDraggable(el, id) {
    const head = el.querySelector('.jw-head');
    let sx, sy, ox, oy, dragging = false;
    head.addEventListener('mousedown', (e) => {
      if (e.target.classList.contains('jw-x')) return;
      el.style.left = el.offsetLeft + 'px'; el.style.right = 'auto';   // right→left before dragging
      dragging = true; sx = e.clientX; sy = e.clientY;
      ox = el.offsetLeft; oy = el.offsetTop; e.preventDefault();
      document.addEventListener('mousemove', mv);
      document.addEventListener('mouseup', up);
    });
    function mv(e) {
      if (!dragging) return;
      const nx = Math.max(0, Math.min(window.innerWidth - 80,  ox + e.clientX - sx));
      const ny = Math.max(0, Math.min(window.innerHeight - 40, oy + e.clientY - sy));
      el.style.left = nx + 'px'; el.style.top = ny + 'px';
    }
    function up() {
      dragging = false;
      document.removeEventListener('mousemove', mv);
      document.removeEventListener('mouseup', up);
      state[id] = Object.assign({}, state[id], {
        x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight, visible: true,
      });
      save();
    }
  }

  // ── Show / hide / reset ──────────────────────────────────────────────────
  function show(id) {
    id = resolve(id); if (!id) return false;
    if (!document.getElementById('jw-' + id)) build(id);
    state[id] = Object.assign({}, state[id], { visible: true }); save();
    lastFocused = id; return true;
  }
  function hide(id) {
    id = resolve(id); if (!id) return false;
    const el = document.getElementById('jw-' + id);
    if (el) { el.classList.add('jw-hiding'); setTimeout(() => el.remove(), 180); }
    state[id] = Object.assign({}, state[id], { visible: false }); save();
    return true;
  }
  function reset() {
    state = {}; save();
    board().innerHTML = '';
    Object.keys(REGISTRY).forEach((id) => { if (REGISTRY[id].def.visible) build(id); });
  }
  function resolve(name) {
    if (!name) return lastFocused;                       // "this one"
    const n = String(name).toLowerCase().trim();
    if (n === 'this' || n === 'this one' || n === 'that') return lastFocused;
    if (REGISTRY[n]) return n;
    for (const k in ALIASES) if (n.includes(k)) return ALIASES[k];
    return null;
  }

  // ── Widget renderers ─────────────────────────────────────────────────────
  function renderTime(body) {
    body.innerHTML = `<div class="jw-clock" id="jw-clock">--:--</div><div class="jw-date" id="jw-date"></div>`;
    const tick = () => {
      const c = document.getElementById('jw-clock'); if (!c) return;
      const d = new Date();
      c.textContent = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      const dt = document.getElementById('jw-date');
      if (dt) dt.textContent = d.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' });
    };
    tick(); clearInterval(renderTime._t); renderTime._t = setInterval(tick, 1000);
  }

  function renderCalendar(body) {
    const d = new Date(), y = d.getFullYear(), m = d.getMonth(), today = d.getDate();
    const first = new Date(y, m, 1).getDay(), days = new Date(y, m + 1, 0).getDate();
    const dow = ['S', 'M', 'T', 'W', 'T', 'F', 'S'];
    let cells = dow.map((x) => `<div class="jw-cal-dow">${x}</div>`).join('');
    for (let i = 0; i < first; i++) cells += `<div class="jw-cal-day jw-empty">0</div>`;
    for (let n = 1; n <= days; n++)
      cells += `<div class="jw-cal-day ${n === today ? 'jw-today' : ''}">${n}</div>`;
    body.innerHTML =
      `<div class="jw-cal-month">${d.toLocaleDateString([], { month: 'long', year: 'numeric' })}</div>` +
      `<div class="jw-cal-grid">${cells}</div>` +
      `<div class="jw-agenda" id="jw-agenda"></div>`;
    paintAgenda();
  }
  function paintAgenda() {
    const box = document.getElementById('jw-agenda'); if (!box) return;
    if (!agendaCache.length) { box.innerHTML = `<div class="jw-ag-empty">Nothing on the agenda today.</div>`; return; }
    box.innerHTML = agendaCache.map((ev) =>
      `<div class="jw-ag-item"><span class="jw-ag-time">${esc(ev.time || '')}</span>` +
      `<span class="jw-ag-title">${esc(ev.title || ev.summary || 'Event')}</span></div>`).join('');
  }

  function renderBrowser(body, id) {
    const el = document.getElementById('jw-' + id);
    if (el) el.querySelector('.jw-body').style.padding = '0', el.querySelector('.jw-body').style.display = 'flex', el.querySelector('.jw-body').style.flexDirection = 'column';
    const sites = {
      Netflix: 'https://www.netflix.com', Peacock: 'https://www.peacocktv.com',
      'Disney+': 'https://www.disneyplus.com', Prime: 'https://www.primevideo.com',
      YouTube: 'https://www.youtube.com',
    };
    const btns = Object.entries(sites)
      .map(([k, u]) => `<button data-url="${u}">${k}</button>`).join('');
    body.innerHTML =
      `<div class="jw-nav">
         <button data-act="back">‹</button><button data-act="reload">⟳</button>
         <input class="jw-url" placeholder="Search or type a URL…" />
         ${btns}
       </div>
       <webview class="jw-web" partition="persist:jarvis" src="https://www.google.com"
                allowpopups></webview>`;
    const wv = body.querySelector('webview');
    const url = body.querySelector('.jw-url');
    const go = (u) => {
      if (!/^https?:\/\//.test(u)) u = /\.\w{2,}($|\/)/.test(u) ? 'https://' + u
            : 'https://www.google.com/search?q=' + encodeURIComponent(u);
      try { wv.src = u; } catch {}
    };
    body.querySelectorAll('button[data-url]').forEach((b) =>
      b.addEventListener('click', () => { go(b.dataset.url); if (url) url.value = b.dataset.url; }));
    body.querySelector('[data-act="back"]').addEventListener('click', () => { try { wv.goBack(); } catch {} });
    body.querySelector('[data-act="reload"]').addEventListener('click', () => { try { wv.reload(); } catch {} });
    url.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(url.value.trim()); });
  }

  function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

  // ── Voice bridge (called from app.js) ──────────────────────────────────────
  // {action:"show"|"hide"|"reset"|"toggle", widget:"calendar"|"this"|...}
  window._widgetControl = function (msg) {
    const a = (msg && msg.action || '').toLowerCase();
    if (a === 'reset' || a === 'restore') return reset();
    if (a === 'hide' || a === 'remove' || a === 'close') return hide(msg.widget);
    if (a === 'show' || a === 'open' || a === 'add' || a === 'pull_up') return show(msg.widget);
    if (a === 'toggle') {
      const id = resolve(msg.widget);
      return (id && geom(id).visible && document.getElementById('jw-' + id)) ? hide(id) : show(id);
    }
  };
  // Calendar data feed (called from app.js agenda_update handler)
  window._widgetsAgenda = function (events) { agendaCache = Array.isArray(events) ? events : []; paintAgenda(); };

  // ── Boot ────────────────────────────────────────────────────────────────
  function boot() {
    injectCSS();
    Object.keys(REGISTRY).forEach((id) => { if (geom(id).visible) build(id); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
