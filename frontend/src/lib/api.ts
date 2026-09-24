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

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url)
  if (!res.ok) {
    const body = await res.text()
    throw new Error(`${res.status}: ${body}`)
  }
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

export function myFindingsUrl(species: string): string {
  return `${location.origin}/api/${species}/my-findings`
}

async function send<T>(url: string, method: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`)
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
}

export async function fetchMyFindings(species: string): Promise<MyFinding[]> {
  const collection = await getJson<{
    features: { geometry: { coordinates: [number, number] }; properties: Omit<MyFinding, 'lat' | 'lon'> }[]
  }>(`/api/${species}/my-findings`)
  return collection.features
    .map((f) => ({ ...f.properties, lon: f.geometry.coordinates[0], lat: f.geometry.coordinates[1] }))
    .sort((a, b) => b.found_at.localeCompare(a.found_at))
}

/** Date and time a finding was saved, in Norwegian format. */
export function formatFoundAt(foundAt: string): string {
  return new Date(foundAt).toLocaleString('no', { dateStyle: 'short', timeStyle: 'short' })
}

export interface ClientConfig {
  esri_api_key: string | null
}

export function fetchConfig(): Promise<ClientConfig> {
  return getJson<ClientConfig>('/api/config')
}

/** Esri World Imagery tiles (needs an ArcGIS Location Platform API key). */
export function imageryUrl(apiKey: string): string {
  return (
    'https://static-map-tiles-api.arcgis.com/arcgis/rest/services/static-basemap-tiles-service/v1/' +
    `arcgis/imagery/static/tile/{z}/{y}/{x}?token=${encodeURIComponent(apiKey)}`
  )
}
