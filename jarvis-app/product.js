// Product profile — the JS side of brain/product.py.
//
// The Electron shell needs the product's NAME, ACCENT and TAGLINE before the
// Python backend is even up (window title, tray, first-run branding), so the
// branding subset is mirrored here and selected by the same JARVIS_PRODUCT env
// var. Keep these values in sync with brain/product.py — the Python side remains
// the source of truth for behaviour (persona, tool whitelist); this file is
// branding only.

const PRODUCTS = {
  jarvis: {
    key: 'jarvis',
    name: 'JARVIS',
    tagline: "Dylan's assistant.",
    accent: '#00d4ff',           // cyan — matches the renderer's --cyan
    appId: 'app.jarvis.desktop',
  },
  alex: {
    key: 'alex',
    name: 'Alex',
    tagline: 'Your study partner.',
    accent: '#5b8dee',           // blue
    appId: 'app.alex.study',
  },
}

// Resolve the product key from, in order:
//   1. JARVIS_PRODUCT env var (dev: `JARVIS_PRODUCT=alex npm start`)
//   2. a `product.build` marker written by the Alex build script — this is how a
//      packaged Alex.app self-identifies, since there's no env var at launch
//   3. default: jarvis
function resolveKey () {
  const env = (process.env.JARVIS_PRODUCT || '').trim().toLowerCase()
  if (env) return env
  try {
    const fs = require('fs')
    const marker = require('path').join(__dirname, 'product.build')
    if (fs.existsSync(marker)) return fs.readFileSync(marker, 'utf8').trim().toLowerCase()
  } catch (_) { /* fall through to default */ }
  return 'jarvis'
}

function currentProduct () {
  return PRODUCTS[resolveKey()] || PRODUCTS.jarvis
}

module.exports = { PRODUCTS, currentProduct }
