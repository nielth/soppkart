/**
 * Offline use: the service worker (public/sw.js) serves saved copies without reception.
 * This module saves the app itself, downloads map areas ("Last ned område"), answers
 * taps on the map from downloaded area data, and keeps findings registered without
 * reception until they can be sent.
 * Cache names and cacheKey() must match public/sw.js.
 */

import proj4 from 'proj4'
import type { FeatureValue, GroupContribution, PointInfo, TrailRoute } from './api'

const APP_CACHE = 'soppkart-app'
const AREAS_CACHE = 'soppkart-areas'
const SEEN_CACHE = 'soppkart-seen'
const PENDING_STORAGE_KEY = 'soppkart.pendingFindings'
const AREAS_STORAGE_KEY = 'soppkart.offlineAreas'

/** Largest download at once; a bigger view must be zoomed into first. */
export const MAX_DOWNLOAD_TILES = 30000

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
  /**
   * Tile zoom relative to the map's zoom: -1 for 512 px tiles, lower for 3D terrain,
   * which MapLibre loads a little coarser than the map.
   */
  zoomOffset?: number
  /** Rough size of one tile, for the size estimate before downloading. */
  kbPerTile: number
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

/**
 * Every tile of the layers that covers the area [west, south, east, north], from the
 * map's zoom now (fromZoom) and all the way in; nothing coarser, which would reach
 * outside the area. Returns the addresses and a size estimate in MB.
 */
export function tileUrls(
  bounds: [number, number, number, number],
  layers: TileLayer[],
  fromZoom: number,
): { urls: string[]; mb: number } {
  const [west, south, east, north] = bounds
  const urls: string[] = []
  let kb = 0
  for (const layer of layers) {
    const first = Math.max(layer.minzoom, Math.min(layer.maxzoom, Math.floor(fromZoom) + (layer.zoomOffset ?? 0)))
    for (let z = first; z <= layer.maxzoom; z++) {
      const max = 2 ** z - 1
      const x0 = Math.max(0, tileX(west, z))
      const x1 = Math.min(max, tileX(east, z))
      const y0 = Math.max(0, tileY(north, z))
      const y1 = Math.min(max, tileY(south, z))
      for (let x = x0; x <= x1; x++) {
        for (let y = y0; y <= y1; y++) urls.push(tileUrl(layer.url, z, x, y))
      }
      kb += (x1 - x0 + 1) * (y1 - y0 + 1) * layer.kbPerTile
    }
  }
  return { urls, mb: kb / 1000 }
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
          // no-store: the service worker then leaves it to us, instead of also keeping a copy.
          const response = await fetch(url, { signal, credentials: 'same-origin', cache: 'no-store' })
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
  saveAreaIndex([])
  parsed.clear()
}

// ---- Tapping the map without reception ----------------------------------------------

type Bounds = [number, number, number, number]

/** A downloaded area with data for tapping: point data for a species, or routes. */
interface OfflineArea {
  kind: 'points' | 'trails'
  species?: string
  bounds: Bounds
  url: string
}

function loadAreaIndex(): OfflineArea[] {
  try {
    return JSON.parse(localStorage.getItem(AREAS_STORAGE_KEY) ?? '[]') as OfflineArea[]
  } catch {
    return []
  }
}

function saveAreaIndex(areas: OfflineArea[]) {
  try {
    localStorage.setItem(AREAS_STORAGE_KEY, JSON.stringify(areas))
  } catch {
    // Private mode etc.: taps just won't work offline.
  }
}

/**
 * Download what taps on the map need in an area: the species' point data
 * (/api/{species}/area) or the routes (/api/trails/area). Throws with the server's
 * message if it refuses (e.g. an area too large for point data).
 */
export async function downloadAreaData(kind: OfflineArea['kind'], species: string | undefined, bounds: Bounds) {
  const [west, south, east, north] = bounds.map((v) => v.toFixed(5))
  const params = new URLSearchParams({ west, south, east, north })
  const url = `${location.origin}/api/${kind === 'points' ? species : 'trails'}/area?${params}`
  const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store' })
  if (!response.ok) {
    const body = await response.text()
    let detail = `${response.status}`
    try {
      detail = (JSON.parse(body) as { detail?: string }).detail ?? detail
    } catch {
      // Not JSON.
    }
    throw new Error(detail)
  }
  await (await caches.open(AREAS_CACHE)).put(url, response)
  // The newest download of the same kind replaces older ones for the same area.
  const areas = loadAreaIndex().filter((a) => a.url !== url)
  saveAreaIndex([...areas, { kind, species, bounds, url }])
  parsed.delete(url)
}

interface PointArea {
  species: string
  trained_at: string
  grid: { x0: number; y0: number; res: number; width: number; height: number }
  groups: { key: string; label: string }[]
  features: { key: string; label: string; unit: string }[]
  soil_names: string[]
  scale: number
  nodata: number
  bias: number
  steps: Int8Array
  values: Float32Array
  land: Uint8Array
  reference: Float32Array
}

const parsed = new Map<string, unknown>()

function bytes(b64: string): Uint8Array {
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
}

/** Half-precision floats (as sent by the backend) to normal floats. */
function float16(data: Uint8Array): Float32Array {
  const halves = new Uint16Array(data.buffer, data.byteOffset, data.byteLength / 2)
  const out = new Float32Array(halves.length)
  for (let i = 0; i < halves.length; i++) {
    const h = halves[i]
    const sign = h & 0x8000 ? -1 : 1
    const exp = (h >> 10) & 0x1f
    const frac = h & 0x3ff
    out[i] =
      exp === 0
        ? sign * 2 ** -14 * (frac / 1024)
        : exp === 31
          ? frac
            ? NaN
            : sign * Infinity
          : sign * 2 ** (exp - 15) * (1 + frac / 1024)
  }
  return out
}

async function loadArea<T>(area: OfflineArea, parse: (json: never) => T): Promise<T | null> {
  if (parsed.has(area.url)) return parsed.get(area.url) as T
  const response = await caches.match(area.url)
  if (!response) return null
  const value = parse((await response.json()) as never)
  parsed.set(area.url, value)
  return value
}

function parsePointArea(json: Record<string, unknown>): PointArea {
  const raw = json as unknown as PointArea & { steps: string; values: string; land: string; reference: string }
  const steps = bytes(raw.steps)
  const reference = bytes(raw.reference)
  return {
    ...raw,
    steps: new Int8Array(steps.buffer),
    values: float16(bytes(raw.values)),
    land: bytes(raw.land),
    reference: new Float32Array(reference.buffer),
  }
}

function contains(bounds: Bounds, lat: number, lon: number): boolean {
  return lon >= bounds[0] && lat >= bounds[1] && lon <= bounds[2] && lat <= bounds[3]
}

// The model's grid: ETRS89 / UTM zone 33N.
const TO_GRID = proj4('EPSG:4326', '+proj=utm +zone=33 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs')

/**
 * What /api/{species}/point would answer, from a downloaded area; null when no
 * downloaded area covers the spot. weights: per group, in the backend's group order.
 */
export async function offlinePoint(
  species: string,
  lat: number,
  lon: number,
  weights: number[] | null,
): Promise<PointInfo | null> {
  const area = loadAreaIndex()
    .reverse()
    .find((a) => a.kind === 'points' && a.species === species && contains(a.bounds, lat, lon))
  if (!area) return null
  const data = await loadArea(area, parsePointArea)
  if (!data) return null
  const { grid } = data
  const [x, y] = TO_GRID.forward([lon, lat])
  const col = Math.floor((x - grid.x0) / grid.res)
  const row = Math.floor((grid.y0 - y) / grid.res)
  if (col < 0 || row < 0 || col >= grid.width || row >= grid.height) return null
  const cells = grid.width * grid.height
  const cell = row * grid.width + col
  const g = data.groups.length
  const w = weights ?? data.groups.map(() => 1)
  const steps = data.groups.map((_, i) => data.steps[i * cells + cell])
  const features: FeatureValue[] = data.features.map((f, i) => {
    const v = data.values[i * cells + cell]
    const value = Number.isNaN(v) ? null : f.key === 'soil' ? data.soil_names[Math.round(v)] : Math.round(v * 100) / 100
    return { key: f.key, label: f.label, unit: f.unit, value }
  })
  let score: number | null = null
  let groups: GroupContribution[] = []
  if (steps[0] !== data.nodata) {
    // The percentile of this square among the background sample, as the backend does it.
    const margin = data.bias + data.scale * steps.reduce((sum, s, i) => sum + s * w[i], 0)
    const n = data.reference.length / g
    const ref = new Float64Array(n)
    for (let r = 0; r < n; r++) {
      let m = data.bias
      for (let i = 0; i < g; i++) m += data.reference[r * g + i] * w[i]
      ref[r] = m
    }
    ref.sort()
    let lo = 0
    let hi = n
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (ref[mid] < margin) lo = mid + 1
      else hi = mid
    }
    score = Math.round((lo / n) * 1000) / 1000
    groups = data.groups.map((group, i) => ({
      key: group.key,
      label: group.label,
      weight: w[i],
      contribution: Math.round(steps[i] * data.scale * w[i] * 100) / 100,
    }))
  } else if (data.land[cell]) {
    score = 0 // land, but outside the species' habitat
  }
  return { lat, lon, inside: true, score, groups, features, offline: true }
}

interface TrailFeature {
  properties: TrailRoute
  geometry: { coordinates: [number, number][][] }
}

/** Routes within toleranceM metres of a spot, from downloaded areas; null if none covers it. */
export async function offlineRoutes(lat: number, lon: number, toleranceM: number): Promise<TrailRoute[] | null> {
  const areas = loadAreaIndex().filter((a) => a.kind === 'trails' && contains(a.bounds, lat, lon))
  if (!areas.length) return null
  // Metres per degree around the spot (flat-earth approximation, fine for short distances).
  const my = 111_320
  const mx = my * Math.cos((lat * Math.PI) / 180)
  const routes: TrailRoute[] = []
  const seen = new Set<string>()
  for (const area of areas) {
    const data = await loadArea(area, (json: { features: TrailFeature[] }) => json.features)
    for (const feature of data ?? []) {
      const near = feature.geometry.coordinates.some((line) =>
        line.some((p, i) => {
          if (i === 0) return false
          const [ax, ay] = [(line[i - 1][0] - lon) * mx, (line[i - 1][1] - lat) * my]
          const [bx, by] = [(p[0] - lon) * mx, (p[1] - lat) * my]
          // Distance from the spot (origin) to the segment a-b.
          const dx = bx - ax
          const dy = by - ay
          const t = Math.max(0, Math.min(1, -(ax * dx + ay * dy) / (dx * dx + dy * dy || 1)))
          return Math.hypot(ax + t * dx, ay + t * dy) <= toleranceM
        }),
      )
      const key = JSON.stringify(feature.properties)
      if (near && feature.properties.fields.length && !seen.has(key)) {
        seen.add(key)
        routes.push(feature.properties)
      }
    }
  }
  return routes
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
