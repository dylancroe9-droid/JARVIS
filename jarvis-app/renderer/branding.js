/* ── Product branding ─────────────────────────────────────────────────────────
 * One renderer, two products. This applies the running product's identity —
 * name, accent, tagline, and the study-vs-assistant surfaces — at startup so a
 * student sees "Alex", not "JARVIS".
 *
 * For JARVIS the values are identical to what's hard-coded in index.html, so
 * this is a no-op for JARVIS and a re-skin for Alex. Behaviour lives in the
 * Python backend; this file only touches presentation. window.PRODUCT is set
 * synchronously in preload.js, so this runs without awaiting anything.
 * ───────────────────────────────────────────────────────────────────────────*/
(function applyBranding () {
  const product = (window.PRODUCT) ||
    { key: 'jarvis', name: 'JARVIS', tagline: "Dylan's assistant.", accent: '#00d4ff' }

  document.documentElement.setAttribute('data-product', product.key)

  // JARVIS markup is already correct — never risk re-tinting the established look.
  if (product.key === 'jarvis') return

  const run = () => {
    const setText = (id, t) => { const el = document.getElementById(id); if (el) el.textContent = t }

    document.title = product.name
    setText('sl-logo-main', product.name)
    setText('sl-logo-sub', product.tagline)
    setText('gm-title', product.name)

    // Empty-state line + study quick-actions (replace JARVIS's assistant chips).
    setText('empty-line', 'READY WHEN YOU ARE')
    const qa = document.getElementById('quick-actions')
    if (qa) {
      qa.innerHTML = [
        ['what\'s the answer to this?', '📸 Read my screen'],
        ['make me a practice test', '📝 Practice test'],
        ['quiz me on what I\'m studying', '🎯 Quiz me'],
        ['how many credits do I have', '💳 Credits'],
      ].map(([msg, label]) =>
        `<button class="chip" data-msg="${msg}">${label}</button>`).join('')
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run)
  } else { run() }

  // Accent: remap the palette variables to the product accent. Most of the UI
  // reads var(--cyan); raw #00d4ff usages are cleaned up separately.
  const a = product.accent
  const style = document.createElement('style')
  style.textContent = `:root{--cyan:${a};--cyan-dim:${a}8c;--cyan-glow:${a}1f;--accent:${a};}`
  ;(document.head || document.documentElement).appendChild(style)
})()
