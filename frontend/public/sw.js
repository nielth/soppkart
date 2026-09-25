// Soppkart service worker: lets the app and the map work without reception.
//
// - The app itself (index.html and the built files) is served from the cache when offline.
// - Map tiles come from the cache when they are there: either downloaded with
//   "Last ned område" (see src/lib/offline.ts), or saved as you look at the map.
// - Species, status and findings lists are fetched fresh when online, and the last
//   copy is used when offline.
// Cache names and cacheKey() must match src/lib/offline.ts.

const APP_CACHE = 'soppkart-app'
const DATA_CACHE = 'soppkart-data'
const SEEN_CACHE = 'soppkart-seen'
// Tiles saved while looking at the map; the oldest are dropped beyond this.
const MAX_SEEN = 4000
// How long to wait for the network before using the saved copy (poor reception).
const NETWORK_TIMEOUT_MS = 5000

const TILE_PATTERNS = [
  /^https:\/\/cache\.kartverket\.no\//,
  /^https:\/\/wms\.geonorge\.no\/skwms1\/wms\.friluftsruter2\?/,
  /^https:\/\/kart\.nve\.no\/enterprise\/services\/Bratthet\//,
  /^https:\/\/elevation-tiles-prod\.s3\.amazonaws\.com\//,
  /\/api\/[^/]+\/tiles\//,
  /\/api\/imagery\//,
  /\/api\/strava\//,
]
const DATA_PATTERN = /\/api\/(species|config|auth\/me|[^/]+\/status|[^/]+\/findings\.geojson|[^/]+\/my-findings)(\?|$)/

/** WMS tile addresses carry a computed BBOX; round it so the same tile always has the same key. */
function cacheKey(url) {
  const u = new URL(url)
  const bbox = u.searchParams.get('BBOX')
  if (bbox) u.searchParams.set('BBOX', bbox.split(',').map((v) => Math.round(Number(v))).join(','))
  return u.toString()
}

self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()))

self.addEventListener('fetch', (event) => {
  const request = event.request
  if (request.method !== 'GET') return
  const url = new URL(request.url)
  const sameOrigin = url.origin === self.location.origin
  if (TILE_PATTERNS.some((p) => p.test(request.url))) {
    event.respondWith(tile(request))
  } else if (sameOrigin && DATA_PATTERN.test(url.pathname + url.search)) {
    event.respondWith(networkFirst(request, DATA_CACHE, request.url))
  } else if (request.mode === 'navigate') {
    // Every page (/, /login, ...) is the same index.html.
    event.respondWith(networkFirst(request, APP_CACHE, new URL('/', self.location.origin).toString()))
  } else if (sameOrigin && url.pathname.startsWith('/assets/')) {
    event.respondWith(cacheFirst(request, APP_CACHE))
  }
})

/** A saved tile if there is one (downloaded or seen before), otherwise the network. */
async function tile(request) {
  const key = cacheKey(request.url)
  const hit = await caches.match(key, { ignoreVary: true })
  if (hit) return hit
  const response = await fetch(request)
  // "Last ned område" fetches with cache: 'no-store' and saves the tile itself.
  if (response.ok && request.cache !== 'no-store') {
    const cache = await caches.open(SEEN_CACHE)
    await cache.put(key, response.clone())
    trim(cache)
  }
  return response
}

async function networkFirst(request, cacheName, key) {
  const cache = await caches.open(cacheName)
  try {
    const response = await withTimeout(fetch(request), NETWORK_TIMEOUT_MS)
    if (response.ok) await cache.put(key, response.clone())
    return response
  } catch (err) {
    const hit = await caches.match(key, { ignoreVary: true })
    if (hit) return hit
    throw err
  }
}

async function cacheFirst(request, cacheName) {
  const hit = await caches.match(request, { ignoreVary: true })
  if (hit) return hit
  const response = await fetch(request)
  if (response.ok) await (await caches.open(cacheName)).put(request, response.clone())
  return response
}

function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timeout')), ms)
    promise.then(
      (value) => (clearTimeout(timer), resolve(value)),
      (err) => (clearTimeout(timer), reject(err)),
    )
  })
}

let putsSinceTrim = 0

/** Drop the oldest seen tiles now and then, so the cache doesn't grow forever. */
async function trim(cache) {
  if (++putsSinceTrim < 200) return
  putsSinceTrim = 0
  const keys = await cache.keys()
  for (const key of keys.slice(0, Math.max(0, keys.length - MAX_SEEN))) await cache.delete(key)
}
