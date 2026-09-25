export interface Species {
  key: string
  name: string
  latin: string
  ready: boolean
}

export interface Status {
  ready: boolean
  message: string
  training: boolean
  my_findings: number
  model: {
    name: string
    latin: string
    trained_at: string
    resolution_m: number
    n_findings: number
    n_findings_used?: number
    n_own_findings_used?: number
    n_presence_cells: number
    n_background_cells: number
    cv_auc: number | null
    habitat: string | null
    /** Feature groups in the order the backend expects weights in. */
    groups: { key: string; label: string; importance: number }[]
    feature_importance: Record<string, number>
  } | null
}

export interface FeatureValue {
  key: string
  label: string
  value: number | string | null
  unit: string
}

export interface GroupContribution {
  key: string
  label: string
  weight: number
  /** Weighted log-odds: > 0 pulls the score up at this spot, < 0 pulls it down. */
  contribution: number
}

export interface PointInfo {
  lat: number
  lon: number
  inside: boolean
  score: number | null
  groups?: GroupContribution[]
  features: FeatureValue[]
}

/** The backend's error message ({"detail": "..."}), or the status and body. */
async function errorOf(res: Response): Promise<Error> {
  const body = await res.text()
  try {
    const detail = (JSON.parse(body) as { detail?: unknown }).detail
    if (typeof detail === 'string') return new Error(detail)
  } catch {
    // Not JSON: fall through.
  }
  return new Error(`${res.status}: ${body}`)
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url)
  if (!res.ok) throw await errorOf(res)
  return res.json() as Promise<T>
}

export function fetchSpecies(): Promise<Species[]> {
  return getJson<Species[]>('/api/species')
}

export function fetchStatus(species: string): Promise<Status> {
  return getJson<Status>(`/api/${species}/status`)
}

/** weights: "w" parameter from weightsParam, or '' for the model's own weighting. */
export function fetchPoint(species: string, lat: number, lon: number, weights: string): Promise<PointInfo> {
  const params = new URLSearchParams({ lat: lat.toFixed(6), lon: lon.toFixed(6) })
  if (weights) params.set('w', weights)
  return getJson<PointInfo>(`/api/${species}/point?${params}`)
}

/**
 * The "w" parameter for group weights in percent, in the backend's group order.
 * Empty when every weight is 100 %, i.e. the model's own prediction.
 */
export function weightsParam(groups: { key: string }[], weightsPct: Record<string, number>): string {
  const values = groups.map((g) => (weightsPct[g.key] ?? 100) / 100)
  return values.every((v) => v === 1) ? '' : values.join(',')
}

/**
 * Score tiles. The model version (training time) is in the URL so a retrained
 * map never shows cached tiles; scores below threshold (0..1) are transparent.
 */
export function tilesUrl(
  species: string,
  version: string | undefined,
  threshold: number,
  weights: string,
): string {
  const params = new URLSearchParams({ threshold: threshold.toFixed(2) })
  if (weights) params.set('w', weights)
  if (version) params.set('v', version)
  return `${location.origin}/api/${species}/tiles/{z}/{x}/{y}.png?${params}`
}

export function findingsUrl(species: string): string {
  return `${location.origin}/api/${species}/findings.geojson`
}

/** Whose findings to show: your own, or (admins only) everyone's. */
export type FindingsScope = 'mine' | 'all'

export function myFindingsUrl(species: string, scope: FindingsScope): string {
  return `${location.origin}/api/${species}/my-findings?scope=${scope}`
}

async function send<T>(url: string, method: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) throw await errorOf(res)
  return res.json() as Promise<T>
}

/** accuracyM is the GPS accuracy, or null for a spot picked on the map. */
export function addMyFinding(species: string, lat: number, lon: number, accuracyM: number | null): Promise<unknown> {
  return send(`/api/${species}/my-findings`, 'POST', { lat, lon, accuracy_m: accuracyM })
}

export function deleteMyFinding(id: number): Promise<unknown> {
  return send(`/api/my-findings/${id}`, 'DELETE')
}

export function startRetrain(species: string): Promise<unknown> {
  return send(`/api/${species}/retrain`, 'POST')
}

export interface MyFinding {
  id: number
  lat: number
  lon: number
  accuracy_m: number | null
  found_at: string
  /** Who found it; only in the admin's "all users" view. */
  username?: string
}

export async function fetchMyFindings(species: string, scope: FindingsScope): Promise<MyFinding[]> {
  const collection = await getJson<{
    features: { geometry: { coordinates: [number, number] }; properties: Omit<MyFinding, 'lat' | 'lon'> }[]
  }>(myFindingsUrl(species, scope))
  return collection.features
    .map((f) => ({ ...f.properties, lon: f.geometry.coordinates[0], lat: f.geometry.coordinates[1] }))
    .sort((a, b) => b.found_at.localeCompare(a.found_at))
}

/** Date and time a finding was saved, in Norwegian format. */
export function formatFoundAt(foundAt: string): string {
  return new Date(foundAt).toLocaleString('no', { dateStyle: 'short', timeStyle: 'short' })
}

export interface ClientConfig {
  /** Whether the server has an Esri key and can serve aerial photos. */
  imagery: boolean
  /** Whether the server has a Strava login and can serve the Strava heatmap. */
  strava: boolean
}

export function fetchConfig(): Promise<ClientConfig> {
  return getJson<ClientConfig>('/api/config')
}

/** Aerial photo tiles (Esri World Imagery), fetched through our backend so the API key stays secret. */
export const IMAGERY_URL = `${location.origin}/api/imagery/{z}/{x}/{y}.jpg`

/** An SLD line style: a white casing under a coloured line, so routes stand out from roads. */
function sldRouteLayer(name: string, color: string, width: number, dash?: string): string {
  const dasharray = dash ? `<CssParameter name="stroke-dasharray">${dash}</CssParameter>` : ''
  return (
    `<NamedLayer><Name>${name}</Name><UserStyle><FeatureTypeStyle><Rule>` +
    `<LineSymbolizer><Stroke><CssParameter name="stroke">#ffffff</CssParameter>` +
    `<CssParameter name="stroke-width">${width + 2.5}</CssParameter>` +
    `<CssParameter name="stroke-opacity">0.9</CssParameter></Stroke></LineSymbolizer>` +
    `<LineSymbolizer><Stroke><CssParameter name="stroke">${color}</CssParameter>` +
    `<CssParameter name="stroke-width">${width}</CssParameter>${dasharray}</Stroke></LineSymbolizer>` +
    `</Rule></FeatureTypeStyle></UserStyle></NamedLayer>`
  )
}

/** Our own colours for Turrutebasen: purple footpaths, dashed blue ski trails, green cycle routes. */
const TRAILS_SLD =
  '<StyledLayerDescriptor version="1.0.0" xmlns="http://www.opengis.net/sld" xmlns:ogc="http://www.opengis.net/ogc">' +
  sldRouteLayer('Fotrute', '#7b1fa2', 2.5) +
  sldRouteLayer('Skiloype', '#0d47a1', 2, '6 4') +
  sldRouteLayer('Sykkelrute', '#2e7d32', 2) +
  sldRouteLayer('AnnenRute', '#616161', 1.5) +
  '</StyledLayerDescriptor>'

/** Kartverket's Turrutebasen (hiking, ski, cycle and other routes) as transparent WMS tiles. */
export const TRAILS_URL =
  'https://wms.geonorge.no/skwms1/wms.friluftsruter2?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap' +
  '&LAYERS=Fotrute,Skiloype,Sykkelrute,AnnenRute&STYLES=&CRS=EPSG:3857&BBOX={bbox-epsg-3857}' +
  '&WIDTH=256&HEIGHT=256&FORMAT=image/png&TRANSPARENT=true' +
  `&SLD_BODY=${encodeURIComponent(TRAILS_SLD)}`

/** Strava heatmap types (Strava's own names) with their labels in the map. */
export const STRAVA_ACTIVITIES = [
  { key: 'run', label: 'Til fots (gå, løp, fjelltur)' },
  { key: 'all', label: 'Alle aktiviteter' },
  { key: 'ride', label: 'Sykkel' },
  { key: 'winter', label: 'Vinter (ski)' },
  { key: 'water', label: 'Vann (padling)' },
]

/**
 * Strava heatmap tiles, fetched through our backend with your Strava login.
 * Bump STRAVA_TILES_VERSION when tile handling changes, so browsers don't keep
 * showing old cached tiles.
 */
const STRAVA_TILES_VERSION = 2

export function stravaTilesUrl(activity: string): string {
  return `${location.origin}/api/strava/${activity}/{z}/{x}/{y}.png?v=${STRAVA_TILES_VERSION}`
}

export interface TrailRoute {
  /** Fotrute, Skiløype, Sykkelrute or Annen rute. */
  type: string
  fields: { label: string; value: string }[]
}

/** Kartverket routes (incl. DNT/UT.no routes) within toleranceM metres of a spot. */
export function fetchTrails(lat: number, lon: number, toleranceM: number): Promise<TrailRoute[]> {
  const params = new URLSearchParams({
    lat: lat.toFixed(6),
    lon: lon.toFixed(6),
    tolerance_m: toleranceM.toFixed(0),
  })
  return getJson<TrailRoute[]>(`/api/trails?${params}`)
}

export interface User {
  id: number
  username: string
  is_admin: boolean
  /** Everything this user can use (admins: all). */
  permissions: string[]
}

/** The logged-in user, or null for visitors. */
export function fetchMe(): Promise<User | null> {
  return getJson<User | null>('/api/auth/me')
}

export function login(username: string, password: string): Promise<User> {
  return send<User>('/api/auth/login', 'POST', { username, password })
}

export function logout(): Promise<unknown> {
  return send('/api/auth/logout', 'POST')
}

export function changePassword(currentPassword: string, newPassword: string): Promise<unknown> {
  return send('/api/auth/password', 'POST', { current_password: currentPassword, new_password: newPassword })
}

/** A user as the admin page sees it: the permissions granted (admins have all anyway). */
export interface AdminUser {
  id: number
  username: string
  is_admin: boolean
  permissions: string[]
}

export interface AdminUsers {
  /** What can be granted, with labels. */
  permissions: { key: string; label: string }[]
  users: AdminUser[]
}

export function fetchUsers(): Promise<AdminUsers> {
  return getJson<AdminUsers>('/api/admin/users')
}

export function addUser(user: { username: string; password: string; is_admin: boolean; permissions: string[] }): Promise<AdminUser> {
  return send<AdminUser>('/api/admin/users', 'POST', user)
}

export function updateUser(
  id: number,
  changes: { is_admin?: boolean; permissions?: string[]; password?: string },
): Promise<AdminUser> {
  return send<AdminUser>(`/api/admin/users/${id}`, 'PATCH', changes)
}

export function deleteUser(id: number): Promise<unknown> {
  return send(`/api/admin/users/${id}`, 'DELETE')
}
