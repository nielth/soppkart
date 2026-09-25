/**
 * Offline use: the service worker (public/sw.js) serves saved copies without reception.
 * This module saves the app itself, downloads map areas ("Last ned område") and keeps
 * findings registered without reception until they can be sent.
 * Cache names and cacheKey() must match public/sw.js.
 */

const APP_CACHE = 'soppkart-app'
const AREAS_CACHE = 'soppkart-areas'
const SEEN_CACHE = 'soppkart-seen'
const PENDING_STORAGE_KEY = 'soppkart.pendingFindings'

/** Largest download at once; a bigger view must be zoomed into first. */
export const MAX_DOWNLOAD_TILES = 8000

export function registerServiceWorker() {
  if (!('serviceWorker' in navigator) || !import.meta.env.PROD) return
  navigator.serviceWorker.register('/sw.js').catch(() => {
    // Offline use just won't work; the app does.
  })
}

/** Keep the page and the files it loaded, so the app opens without reception. */
export async function cacheAppShell(extraUrls: string[]) {
  if (!('caches' in window) || !import.meta.env.PROD) return
  const assets = performance
    .getEntriesByType('resource')
    .map((e) => e.name)
    .filter((url) => url.startsWith(`${location.origin}/assets/`))
  const keep = new Set([`${location.origin}/`, ...assets, ...extraUrls])
  const cache = await caches.open(APP_CACHE)
  try {
    await cache.addAll([...keep])
  } catch {
    return // no reception: keep the saved copy as it is
  }
  // Files from earlier versions of the app are no longer needed.
  for (const request of await cache.keys()) {
    if (!keep.has(request.url)) await cache.delete(request)
  }
}

/** WMS tile addresses carry a computed BBOX; round it so the same tile always has the same key. */
function cacheKey(url: string): string {
  const u = new URL(url)
  const bbox = u.searchParams.get('BBOX')
  if (bbox) u.searchParams.set('BBOX', bbox.split(',').map((v) => Math.round(Number(v))).join(','))
  return u.toString()
}

export interface TileLayer {
  /** Tile address with {z}/{x}/{y} or {bbox-epsg-3857}, as given to MapLibre. */
  url: string
  minzoom: number
  maxzoom: number
}

const EXTENT = 20037508.342789244

function tileX(lon: number, z: number): number {
  return Math.floor(((lon + 180) / 360) * 2 ** z)
}

function tileY(lat: number, z: number): number {
  const rad = (lat * Math.PI) / 180
  return Math.floor(((1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2) * 2 ** z)
}

function tileUrl(template: string, z: number, x: number, y: number): string {
  const size = (2 * EXTENT) / 2 ** z
  const minX = -EXTENT + x * size
  const maxY = EXTENT - y * size
  const bbox = `${minX},${maxY - size},${minX + size},${maxY}`
  return template
    .replace('{z}', String(z))
    .replace('{x}', String(x))
    .replace('{y}', String(y))
    .replace('{bbox-epsg-3857}', bbox)
}

/** Every tile of the layers that covers the area [west, south, east, north]. */
export function tileUrls(bounds: [number, number, number, number], layers: TileLayer[]): string[] {
  const [west, south, east, north] = bounds
  const urls: string[] = []
  for (const layer of layers) {
    for (let z = layer.minzoom; z <= layer.maxzoom; z++) {
      const max = 2 ** z - 1
      const x0 = Math.max(0, tileX(west, z))
      const x1 = Math.min(max, tileX(east, z))
      const y0 = Math.max(0, tileY(north, z))
      const y1 = Math.min(max, tileY(south, z))
      for (let x = x0; x <= x1; x++) {
        for (let y = y0; y <= y1; y++) urls.push(tileUrl(layer.url, z, x, y))
      }
    }
  }
  return urls
}

export interface DownloadProgress {
  done: number
  total: number
  failed: number
}

/** Download the tiles into the offline cache, a few at a time. Tiles already saved are skipped. */
export async function downloadTiles(
  urls: string[],
  onProgress: (p: DownloadProgress) => void,
  signal: AbortSignal,
): Promise<DownloadProgress> {
  // Ask the browser not to clear the saved maps when space runs low.
  await navigator.storage?.persist?.().catch(() => false)
  const cache = await caches.open(AREAS_CACHE)
  const progress = { done: 0, total: urls.length, failed: 0 }
  const queue = [...urls]
  async function worker() {
    while (queue.length && !signal.aborted) {
      const url = queue.shift()!
      const key = cacheKey(url)
      try {
        if (!(await cache.match(key, { ignoreVary: true }))) {
          const response = await fetch(url, { signal, credentials: 'same-origin' })
          if (response.ok) await cache.put(key, response)
          else progress.failed++
        }
      } catch {
        if (!signal.aborted) progress.failed++
      }
      progress.done++
      if (progress.done % 10 === 0 || progress.done === progress.total) onProgress({ ...progress })
    }
  }
  await Promise.all(Array.from({ length: 6 }, worker))
  return progress
}

/** Space used by the saved app and maps, in bytes (null if the browser doesn't say). */
export async function storageUsed(): Promise<number | null> {
  const estimate = await navigator.storage?.estimate?.().catch(() => null)
  return estimate?.usage ?? null
}

/** Delete downloaded areas and the tiles saved while looking at the map. */
export async function clearSavedMaps() {
  await caches.delete(AREAS_CACHE)
  await caches.delete(SEEN_CACHE)
}

/** Whether the app runs from the home screen (where iOS keeps saved data). */
export function isStandalone(): boolean {
  return (
    window.matchMedia('(display-mode: standalone)').matches ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true
  )
}

/** fetch() fails with a TypeError when there is no connection. */
export function isNetworkError(err: unknown): boolean {
  return err instanceof TypeError || !navigator.onLine
}

/** A finding registered without reception, sent when the connection is back. */
export interface PendingFinding {
  species: string
  lat: number
  lon: number
  accuracy_m: number | null
  found_at: string
}

export function loadPendingFindings(): PendingFinding[] {
  try {
    return JSON.parse(localStorage.getItem(PENDING_STORAGE_KEY) ?? '[]') as PendingFinding[]
  } catch {
    return []
  }
}

export function savePendingFindings(findings: PendingFinding[]) {
  try {
    localStorage.setItem(PENDING_STORAGE_KEY, JSON.stringify(findings))
  } catch {
    // Private mode etc.: nothing to do.
  }
}
