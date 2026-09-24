<script lang="ts">
  import { onMount } from 'svelte'
  import * as maplibregl from 'maplibre-gl'
  import type { FeatureCollection } from 'geojson'
  import 'maplibre-gl/dist/maplibre-gl.css'
  // MapLibre runs GeoJSON layers in a web worker loaded from a separate file.
  // Let Vite bundle it (with its imports) and tell MapLibre where it ends up.
  import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'
  import {
    addMyFinding,
    deleteMyFinding,
    fetchConfig,
    fetchMyFindings,
    fetchPoint,
    fetchSpecies,
    fetchTrails,
    formatFoundAt,
    fetchStatus,
    findingsUrl,
    IMAGERY_URL,
    STRAVA_ACTIVITIES,
    stravaTilesUrl,
    TRAILS_URL,
    myFindingsUrl,
    startRetrain,
    tilesUrl,
    weightsParam,
    type MyFinding,
    type PointInfo,
    type TrailRoute,
    type Species,
    type Status,
  } from './lib/api'
  import Legend from './lib/Legend.svelte'
  import { Badge } from '$lib/components/ui/badge'
  import { Button } from '$lib/components/ui/button'
  import * as Card from '$lib/components/ui/card'
  import * as Collapsible from '$lib/components/ui/collapsible'
  import { Label } from '$lib/components/ui/label'
  import * as Select from '$lib/components/ui/select'
  import { Separator } from '$lib/components/ui/separator'
  import { Slider } from '$lib/components/ui/slider'
  import { Switch } from '$lib/components/ui/switch'
  import * as ToggleGroup from '$lib/components/ui/toggle-group'
  import {
    ChevronDown,
    Crosshair,
    Layers,
    MapPin,
    Menu,
    RotateCcw,
    SlidersHorizontal,
    Sparkles,
    X,
  } from '@lucide/svelte'
  import PointPanel from './lib/PointPanel.svelte'

  let mapEl: HTMLDivElement
  let map: maplibregl.Map | undefined
  let marker: maplibregl.Marker | undefined

  const SPECIES_STORAGE_KEY = 'soppkart.species'
  // New key when the default changes, so it applies once to everyone.
  const TOP_PCT_STORAGE_KEY = 'soppkart.topPct.v2'
  const DEFAULT_TOP_PCT = 5

  let speciesList = $state<Species[]>([])
  let species = $state(loadSpecies())
  let status = $state<Status | null>(null)
  let statusError = $state<string | null>(null)
  let opacity = $state(0.65)
  let showHeatmap = $state(true)
  let showFindings = $state(true)
  let showTrails = $state(true)
  let showStravaHeat = $state(false)
  let stravaActivity = $state('run')
  let stravaAvailable = $state(false)
  let point = $state<PointInfo | null>(null)
  let pointLoading = $state(false)
  let pointError = $state<string | null>(null)
  let selected: { lat: number; lon: number } | null = null
  let routes = $state<TrailRoute[]>([])
  // The finding popup that is open, if any (a map tap closes it).
  let openPopup: maplibregl.Popup | null = null
  // Set once the map's own layers exist, so effects that style them re-run then.
  let mapLoaded = $state(false)
  // Share of the habitat to colour: the best topPct %.
  let topPct = $state(loadTopPct())
  let threshold = $derived(1 - topPct / 100)
  let gps = $state<{ lat: number; lon: number; accuracy: number } | null>(null)
  let findingMessage = $state<string | null>(null)
  let myFindings = $state<MyFinding[]>([])
  let showMyList = $state(false)
  // On phones the sidebar is a drawer over the map; on wider screens it is always shown.
  let sidebarOpen = $state(false)
  // Weight per feature group in percent (100 = the model's own weighting).
  let weightsPct = $state<Record<string, number>>(loadWeights())
  let groups = $derived(status?.model?.groups ?? [])
  let wParam = $derived(weightsParam(groups, weightsPct))
  // Background: Kartverket's topo map, or aerial photos when an Esri key is configured.
  let basemap = $state<'kart' | 'flyfoto'>(loadBasemap())
  let imageryAvailable = $state(false)
  let trainPoll: ReturnType<typeof setInterval> | undefined
  let tilesTimer: ReturnType<typeof setTimeout> | undefined

  const WEIGHTS_STORAGE_KEY = 'soppkart.weights'
  const BASEMAP_STORAGE_KEY = 'soppkart.basemap'
  const EMPTY_COLLECTION: FeatureCollection = { type: 'FeatureCollection', features: [] }

  const TILE_BASE = 'https://cache.kartverket.no/v1/wmts/1.0.0/topo/default/webmercator/{z}/{y}/{x}.png'

  maplibregl.setWorkerUrl(workerUrl)

  onMount(() => {
    map = new maplibregl.Map({
      container: mapEl,
      style: {
        version: 8,
        sources: {
          topo: {
            type: 'raster',
            tiles: [TILE_BASE],
            tileSize: 256,
            maxzoom: 18,
            attribution: '© <a href="https://www.kartverket.no/">Kartverket</a>',
          },
        },
        layers: [{ id: 'topo', type: 'raster', source: 'topo' }],
      },
      center: [10.75, 59.95],
      zoom: 6,
      maxZoom: 17,
      attributionControl: { compact: true },
    })

    map.addControl(new maplibregl.NavigationControl({ showCompass: true }), 'top-right')
    const geolocate = new maplibregl.GeolocateControl({
      positionOptions: { enableHighAccuracy: true },
      trackUserLocation: true,
      showAccuracyCircle: true,
    })
    map.addControl(geolocate, 'top-right')
    map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left')

    map.on('load', () => {
      if (imageryAvailable) addImagery()
      map!.addSource('score', {
        type: 'raster',
        tiles: [tilesUrl(species, status?.model?.trained_at, threshold, wParam)],
        tileSize: 256,
        minzoom: 3,
        maxzoom: 14,
      })
      map!.addLayer({
        id: 'score',
        type: 'raster',
        source: 'score',
        paint: { 'raster-opacity': opacity, 'raster-resampling': 'nearest' },
      })

      map!.addSource('trails', {
        type: 'raster',
        tiles: [TRAILS_URL],
        tileSize: 256,
        minzoom: 8,
        attribution: 'Turruter © <a href="https://www.kartverket.no/">Kartverket</a>',
      })
      map!.addLayer({ id: 'trails', type: 'raster', source: 'trails', layout: { visibility: 'none' } })

      // Strava heatmap (only requested once switched on).
      map!.addSource('strava-heat', {
        type: 'raster',
        tiles: [stravaTilesUrl(stravaActivity)],
        tileSize: 512,
        maxzoom: 15,
        attribution: 'Heatmap © <a href="https://www.strava.com/maps/global-heatmap">Strava</a>',
      })
      map!.addLayer({
        id: 'strava-heat',
        type: 'raster',
        source: 'strava-heat',
        layout: { visibility: 'none' },
        paint: { 'raster-opacity': 0.8 },
      })

      map!.addSource('findings', {
        type: 'geojson',
        data: findingsUrl(species),
        cluster: true,
        clusterRadius: 40,
        clusterMaxZoom: 11,
      })
      map!.addLayer({
        id: 'findings-clusters',
        type: 'circle',
        source: 'findings',
        filter: ['has', 'point_count'],
        layout: { visibility: 'none' },
        paint: {
          'circle-color': '#f2a900',
          'circle-stroke-color': '#6b4a00',
          'circle-stroke-width': 1,
          'circle-radius': ['step', ['get', 'point_count'], 10, 20, 14, 200, 18],
        },
      })
      map!.addLayer({
        id: 'findings-count',
        type: 'symbol',
        source: 'findings',
        filter: ['has', 'point_count'],
        layout: {
          visibility: 'none',
          'text-field': ['get', 'point_count_abbreviated'],
          'text-size': 11,
        },
      })
      map!.addLayer({
        id: 'findings-points',
        type: 'circle',
        source: 'findings',
        filter: ['!', ['has', 'point_count']],
        layout: { visibility: 'none' },
        paint: {
          'circle-color': '#f2a900',
          'circle-stroke-color': '#6b4a00',
          'circle-stroke-width': 1,
          'circle-radius': 5,
        },
      })

      // Precision circle for the finding whose popup is open (like Artskart).
      map!.addSource('finding-radius', { type: 'geojson', data: EMPTY_COLLECTION })
      map!.addLayer({
        id: 'finding-radius-fill',
        type: 'fill',
        source: 'finding-radius',
        paint: { 'fill-color': '#1f78b4', 'fill-opacity': 0.15 },
      })
      map!.addLayer({
        id: 'finding-radius-line',
        type: 'line',
        source: 'finding-radius',
        paint: { 'line-color': '#1f78b4', 'line-width': 2 },
      })

      map!.addSource('my-findings', { type: 'geojson', data: myFindingsUrl(species) })
      map!.addLayer({
        id: 'my-findings',
        type: 'circle',
        source: 'my-findings',
        paint: {
          'circle-color': '#1a9641',
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 2,
          'circle-radius': 7,
        },
      })
      for (const id of ['my-findings', 'findings-points', 'findings-clusters']) {
        map!.on('mouseenter', id, () => (map!.getCanvas().style.cursor = 'pointer'))
        map!.on('mouseleave', id, () => (map!.getCanvas().style.cursor = ''))
      }

      mapLoaded = true
      geolocate.trigger()
    })

    geolocate.on('geolocate', (e) => {
      gps = { lat: e.coords.latitude, lon: e.coords.longitude, accuracy: e.coords.accuracy }
    })

    map.on('click', (e) => onMapClick(e))

    fetchConfig()
      .then((c) => {
        imageryAvailable = c.imagery
        stravaAvailable = c.strava
        if (imageryAvailable && map?.isStyleLoaded() && !map.getLayer('imagery')) addImagery()
      })
      .catch(() => (imageryAvailable = false))

    fetchSpecies()
      .then((list) => {
        speciesList = list
        if (!list.some((s) => s.key === species) && list.length > 0) species = list[0].key
      })
      .catch((err) => (statusError = String(err)))

    return () => {
      clearInterval(trainPoll)
      map?.remove()
    }
  })

  // Everything that depends on the chosen species.
  $effect(() => {
    const key = species
    saveSpecies(key)
    status = null
    statusError = null
    refreshStatus(key)
    map?.getSource<maplibregl.GeoJSONSource>('findings')?.setData(findingsUrl(key))
    refreshMyFindings(key)
    if (selected) loadPoint(selected.lat, selected.lon)
  })

  // The sliders: re-request tiles (and the tapped point) shortly after they stop moving.
  $effect(() => {
    const t = threshold
    const w = wParam
    saveTopPct(topPct)
    saveWeights(weightsPct)
    clearTimeout(tilesTimer)
    tilesTimer = setTimeout(() => {
      updateTiles(species, status?.model?.trained_at, t, w)
      if (selected) loadPoint(selected.lat, selected.lon)
    }, 250)
  })

  function updateTiles(key: string, version: string | undefined, t: number, w: string) {
    map?.getSource<maplibregl.RasterTileSource>('score')?.setTiles([tilesUrl(key, version, t, w)])
  }

  function refreshStatus(key: string) {
    fetchStatus(key)
      .then((s) => {
        if (key !== species) return
        status = s
        updateTiles(key, s.model?.trained_at, threshold, weightsParam(s.model?.groups ?? [], weightsPct))
        // While a retrain runs, check back until the new model is in place.
        clearInterval(trainPoll)
        if (s.training) trainPoll = setInterval(() => refreshStatus(key), 15000)
      })
      .catch((err) => (statusError = String(err)))
  }

  async function registerFinding(lat: number, lon: number, accuracy: number | null) {
    findingMessage = null
    try {
      await addMyFinding(species, lat, lon, accuracy)
      refreshMyFindings(species)
      findingMessage =
        accuracy !== null && accuracy > 50
          ? `Lagret, men GPS-nøyaktigheten er ${Math.round(accuracy)} m. Bare funn innen 50 m brukes i treningen.`
          : 'Funnet er lagret. Tren modellen på nytt for å bruke det.'
      refreshStatus(species)
    } catch (err) {
      findingMessage = `Kunne ikke lagre: ${err}`
    }
  }

  function showMyFinding(feature: maplibregl.MapGeoJSONFeature) {
    if (!map || feature.geometry.type !== 'Point') return
    const [lon, lat] = feature.geometry.coordinates
    const { id, found_at, accuracy_m } = feature.properties as { id: number; found_at: string; accuracy_m: number }
    const content = document.createElement('div')
    content.className = 'finding-popup'
    const text = document.createElement('div')
    text.textContent = `Ditt funn ${formatFoundAt(found_at)} (±${Math.round(accuracy_m)} m)`
    const button = document.createElement('button')
    button.textContent = 'Slett'
    const popup = showFindingPopup(lon, lat, accuracy_m, content)
    button.onclick = async () => {
      if (await removeFinding(id)) popup.remove()
    }
    content.append(text, button)
  }

  /** Reload your findings for the list and the map. */
  async function refreshMyFindings(key: string) {
    map?.getSource<maplibregl.GeoJSONSource>('my-findings')?.setData(myFindingsUrl(key))
    try {
      const list = await fetchMyFindings(key)
      if (key === species) myFindings = list
    } catch (err) {
      findingMessage = `Kunne ikke hente dine funn: ${err}`
    }
  }

  async function removeFinding(id: number): Promise<boolean> {
    if (!confirm('Slette dette funnet?')) return false
    try {
      await deleteMyFinding(id)
    } catch (err) {
      findingMessage = `Kunne ikke slette: ${err}`
      return false
    }
    refreshMyFindings(species)
    refreshStatus(species)
    return true
  }

  function flyToFinding(f: MyFinding) {
    sidebarOpen = false
    map?.flyTo({ center: [f.lon, f.lat], zoom: Math.max(map.getZoom(), 16) })
  }

  async function retrain() {
    try {
      await startRetrain(species)
    } catch (err) {
      findingMessage = `Kunne ikke starte trening: ${err}`
    }
    refreshStatus(species)
  }

  function showGbifFinding(feature: maplibregl.MapGeoJSONFeature) {
    if (!map || feature.geometry.type !== 'Point') return
    const [lon, lat] = feature.geometry.coordinates
    const p = feature.properties as {
      gbif_id?: number
      year?: number
      uncertainty_m?: number
      basis?: string
      taxon?: string
    }
    const basis: Record<string, string> = {
      HUMAN_OBSERVATION: 'Observasjon',
      PRESERVED_SPECIMEN: 'Belegg i samling',
      MATERIAL_SAMPLE: 'Materialprøve',
    }
    const content = document.createElement('div')
    content.className = 'finding-popup'
    const title = document.createElement('strong')
    title.textContent = 'Funnopplysninger'
    const lines = [
      p.taxon ?? status?.model?.latin,
      p.year ? `Funnet ${p.year}` : 'Ukjent år',
      p.uncertainty_m ? `Nøyaktighet ±${Math.round(p.uncertainty_m)} m` : 'Ukjent nøyaktighet',
      p.basis ? (basis[p.basis] ?? p.basis) : null,
    ]
    content.append(title)
    for (const line of lines) {
      if (!line) continue
      const div = document.createElement('div')
      div.textContent = line
      content.append(div)
    }
    if (p.gbif_id) {
      const link = document.createElement('a')
      link.href = `https://www.gbif.org/occurrence/${p.gbif_id}`
      link.target = '_blank'
      link.rel = 'noopener'
      link.textContent = 'Se funnet hos GBIF / Artsdatabanken ↗'
      content.append(link)
    }
    showFindingPopup(lon, lat, p.uncertainty_m ?? null, content)
  }

  /** Popup for a finding, with its location precision drawn as a circle while it is open. */
  function showFindingPopup(lon: number, lat: number, radiusM: number | null, content: HTMLElement) {
    const source = map!.getSource<maplibregl.GeoJSONSource>('finding-radius')
    source?.setData(radiusM ? circle(lon, lat, radiusM) : EMPTY_COLLECTION)
    const popup = new maplibregl.Popup({ closeButton: true, closeOnClick: false })
      .setLngLat([lon, lat])
      .setDOMContent(content)
      .addTo(map!)
    openPopup = popup
    popup.on('close', () => {
      source?.setData(EMPTY_COLLECTION)
      if (openPopup === popup) openPopup = null
    })
    return popup
  }

  /** A circle of radiusM metres around a point, as a 64-sided polygon. */
  function circle(lon: number, lat: number, radiusM: number): FeatureCollection {
    const earth = 6_371_000
    const dLat = (radiusM / earth) * (180 / Math.PI)
    const dLon = dLat / Math.cos((lat * Math.PI) / 180)
    const ring = Array.from({ length: 65 }, (_, i) => {
      const a = (i / 64) * 2 * Math.PI
      return [lon + dLon * Math.cos(a), lat + dLat * Math.sin(a)]
    })
    return {
      type: 'FeatureCollection',
      features: [{ type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [ring] } }],
    }
  }

  /** Aerial photo layer, drawn right above the topo map and below everything else. */
  function addImagery() {
    map!.addSource('imagery', {
      type: 'raster',
      tiles: [IMAGERY_URL],
      tileSize: 256,
      maxzoom: 19,
      attribution:
        'Powered by <a href="https://www.esri.com">Esri</a> | Esri, Vantor, GeoEye, Earthstar Geographics, CNES/Airbus DS, USDA, USGS, AeroGRID, IGN',
    })
    const above = map!.getStyle().layers.find((l) => l.id !== 'topo')?.id
    map!.addLayer(
      {
        id: 'imagery',
        type: 'raster',
        source: 'imagery',
        layout: { visibility: basemap === 'flyfoto' ? 'visible' : 'none' },
      },
      above,
    )
  }

  function loadBasemap(): 'kart' | 'flyfoto' {
    try {
      return localStorage.getItem(BASEMAP_STORAGE_KEY) === 'flyfoto' ? 'flyfoto' : 'kart'
    } catch {
      return 'kart'
    }
  }

  function saveBasemap(value: string) {
    try {
      localStorage.setItem(BASEMAP_STORAGE_KEY, value)
    } catch {
      // Private mode etc.: just don't remember the choice.
    }
  }

  function loadWeights(): Record<string, number> {
    try {
      return JSON.parse(localStorage.getItem(WEIGHTS_STORAGE_KEY) ?? '{}') as Record<string, number>
    } catch {
      return {}
    }
  }

  function saveWeights(value: Record<string, number>) {
    try {
      localStorage.setItem(WEIGHTS_STORAGE_KEY, JSON.stringify(value))
    } catch {
      // Private mode etc.: just don't remember the choice.
    }
  }

  function resetWeights() {
    weightsPct = {}
  }

  function loadTopPct(): number {
    try {
      return Number(localStorage.getItem(TOP_PCT_STORAGE_KEY)) || DEFAULT_TOP_PCT
    } catch {
      return DEFAULT_TOP_PCT
    }
  }

  function saveTopPct(value: number) {
    try {
      localStorage.setItem(TOP_PCT_STORAGE_KEY, String(value))
    } catch {
      // Private mode etc.: just don't remember the choice.
    }
  }

  // Read the values before checking the map: Svelte only re-runs an effect for
  // state it actually read.
  $effect(() => {
    const layerOpacity = opacity
    const visibility = showHeatmap ? 'visible' : 'none'
    if (!mapLoaded || !map) return
    map.setPaintProperty('score', 'raster-opacity', layerOpacity)
    map.setLayoutProperty('score', 'visibility', visibility)
  })

  $effect(() => {
    const showImagery = basemap === 'flyfoto' && imageryAvailable
    saveBasemap(basemap)
    if (!mapLoaded || !map || !map.getLayer('imagery')) return
    map.setLayoutProperty('imagery', 'visibility', showImagery ? 'visible' : 'none')
  })

  $effect(() => {
    const visibility = showTrails ? 'visible' : 'none'
    if (!mapLoaded || !map) return
    map.setLayoutProperty('trails', 'visibility', visibility)
  })

  $effect(() => {
    const visibility = showStravaHeat && stravaAvailable ? 'visible' : 'none'
    const url = stravaTilesUrl(stravaActivity)
    if (!mapLoaded || !map) return
    map.getSource<maplibregl.RasterTileSource>('strava-heat')?.setTiles([url])
    map.setLayoutProperty('strava-heat', 'visibility', visibility)
  })

  $effect(() => {
    const visibility = showFindings ? 'visible' : 'none'
    if (!mapLoaded || !map) return
    for (const id of ['findings-clusters', 'findings-count', 'findings-points']) {
      map.setLayoutProperty(id, 'visibility', visibility)
    }
  })

  function loadSpecies(): string {
    try {
      return localStorage.getItem(SPECIES_STORAGE_KEY) ?? 'kantarell'
    } catch {
      return 'kantarell'
    }
  }

  function saveSpecies(key: string) {
    try {
      localStorage.setItem(SPECIES_STORAGE_KEY, key)
    } catch {
      // Private mode etc.: just don't remember the choice.
    }
  }

  /**
   * One handler for all map taps: if something is open, the tap only closes it.
   * Otherwise it opens the finding under the tap, zooms into a cluster, or shows
   * the score for the spot.
   */
  async function onMapClick(e: maplibregl.MapMouseEvent) {
    if (!map) return
    if (openPopup || point || pointError || pointLoading) {
      closeAll()
      return
    }
    const layers = ['my-findings', 'findings-points', 'findings-clusters'].filter((id) => map!.getLayer(id))
    const feature = map.queryRenderedFeatures(e.point, { layers })[0]
    if (feature?.geometry.type === 'Point') {
      const [lon, lat] = feature.geometry.coordinates
      if (feature.layer.id === 'my-findings') showMyFinding(feature)
      else if (feature.layer.id === 'findings-points') showGbifFinding(feature)
      else {
        const source = map.getSource<maplibregl.GeoJSONSource>('findings')
        const zoom = await source!.getClusterExpansionZoom(feature.properties.cluster_id as number)
        map.easeTo({ center: [lon, lat], zoom })
      }
      return
    }
    selectPoint(e.lngLat.lat, e.lngLat.lng)
  }

  function closeAll() {
    openPopup?.remove()
    openPopup = null
    closePanel()
  }

  function selectPoint(lat: number, lon: number) {
    if (!map) return
    marker?.remove()
    marker = new maplibregl.Marker({ color: '#7a3e00' }).setLngLat([lon, lat]).addTo(map)
    selected = { lat, lon }
    loadPoint(lat, lon)
    loadRoutes(lat, lon)
  }

  /** Routes within ~12 screen pixels of the tapped spot, when trails are shown and zoomed in. */
  async function loadRoutes(lat: number, lon: number) {
    routes = []
    // Only at hiking zoom, where single routes can be told apart.
    if (!showTrails || !map || map.getZoom() < 11) return
    const metresPerPixel = (156_543 * Math.cos((lat * Math.PI) / 180)) / 2 ** map.getZoom()
    try {
      const found = await fetchTrails(lat, lon, Math.max(10, 12 * metresPerPixel))
      if (selected?.lat === lat && selected?.lon === lon) routes = found
    } catch {
      // Route info is a bonus; the score panel works without it.
    }
  }

  async function loadPoint(lat: number, lon: number) {
    pointLoading = true
    pointError = null
    try {
      point = await fetchPoint(species, lat, lon, wParam)
    } catch (err) {
      point = null
      pointError = String(err)
    } finally {
      pointLoading = false
    }
  }

  function closePanel() {
    point = null
    pointError = null
    routes = []
    selected = null
    marker?.remove()
    marker = undefined
  }
</script>

<div class="flex h-full">
  <aside
    class="fixed inset-y-0 left-0 z-40 flex w-[min(340px,88vw)] -translate-x-full flex-col gap-3 overflow-y-auto border-r bg-background p-3 *:shrink-0 pt-[max(12px,env(safe-area-inset-top))] shadow-xl transition-transform duration-200 md:static md:w-[340px] md:translate-x-0 md:shadow-none"
    class:translate-x-0={sidebarOpen}
  >
    <header class="flex items-center justify-between px-1">
      <div class="flex items-center gap-2">
        <span class="text-2xl">🍄</span>
        <div>
          <div class="text-lg leading-tight font-semibold">Soppkart</div>
          <div class="text-xs text-muted-foreground">Finn de beste soppstedene</div>
        </div>
      </div>
      <Button variant="ghost" size="icon" class="md:hidden" onclick={() => (sidebarOpen = false)} aria-label="Lukk meny">
        <X />
      </Button>
    </header>

    {#if statusError}
      <div class="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
        Backend utilgjengelig: {statusError}
      </div>
    {:else if status && !status.ready}
      <div class="rounded-md border bg-accent px-3 py-2 text-xs">{status.message}</div>
    {/if}

    <Card.Root class="gap-3 py-4">
      <Card.Header class="px-4">
        <Card.Title class="text-sm">Art</Card.Title>
      </Card.Header>
      <Card.Content class="grid grid-cols-2 gap-2 px-4">
        {#each speciesList as s (s.key)}
          <button
            class="flex flex-col items-start rounded-lg border px-3 py-2 text-left transition-colors hover:bg-accent {s.key === species
              ? 'border-primary bg-accent ring-1 ring-primary'
              : ''}"
            aria-pressed={s.key === species}
            onclick={() => (species = s.key)}
          >
            <span class="text-sm font-medium">{s.name}</span>
            <span class="text-[11px] text-muted-foreground italic">{s.latin}</span>
          </button>
        {/each}
      </Card.Content>
    </Card.Root>

    <Card.Root class="gap-3 py-4">
      <Card.Header class="px-4">
        <Card.Title class="flex items-center gap-2 text-sm"><Layers class="size-4" /> Kart</Card.Title>
      </Card.Header>
      <Card.Content class="flex flex-col gap-4 px-4">
        <ToggleGroup.Root
          type="single"
          variant="outline"
          class="w-full"
          value={basemap}
          onValueChange={(v) => v && (basemap = v as 'kart' | 'flyfoto')}
        >
          <ToggleGroup.Item value="kart" class="flex-1">Kart</ToggleGroup.Item>
          <ToggleGroup.Item
            value="flyfoto"
            class="flex-1"
            disabled={!imageryAvailable}
            title={imageryAvailable ? 'Flyfoto (Esri World Imagery)' : 'Flyfoto krever ESRI_API_KEY på serveren'}
          >
            Flyfoto
          </ToggleGroup.Item>
        </ToggleGroup.Root>

        <div class="flex items-center justify-between">
          <Label for="show-heatmap">Vis sannsynlighet</Label>
          <Switch id="show-heatmap" bind:checked={showHeatmap} />
        </div>
        <div class="flex flex-col gap-2">
          <div class="flex items-center justify-between text-sm">
            <span>Vis topp</span>
            <Badge variant="secondary">{topPct} %</Badge>
          </div>
          <Slider type="single" bind:value={topPct} min={1} max={100} step={1} disabled={!showHeatmap} />
          <Legend {topPct} />
        </div>
        <div class="flex flex-col gap-2">
          <div class="flex items-center justify-between text-sm">
            <span>Gjennomsiktighet</span>
            <span class="text-xs text-muted-foreground">{Math.round(opacity * 100)} %</span>
          </div>
          <Slider type="single" bind:value={opacity} min={0} max={1} step={0.05} disabled={!showHeatmap} />
        </div>

        <Separator />

        <div class="flex items-center justify-between">
          <Label for="show-findings">Registrerte funn (Artsdatabanken)</Label>
          <Switch id="show-findings" bind:checked={showFindings} />
        </div>
        <div class="flex items-center justify-between">
          <Label for="show-trails">Turstier (Kartverket, DNT)</Label>
          <Switch id="show-trails" bind:checked={showTrails} />
        </div>
        {#if showTrails}
          <div class="-mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
            <span class="flex items-center gap-1"><span class="h-1 w-4 rounded bg-[#7b1fa2]"></span> Fotrute</span>
            <span class="flex items-center gap-1">
              <span class="h-0 w-4 border-t-2 border-dashed border-[#0d47a1]"></span> Skiløype
            </span>
            <span class="flex items-center gap-1"><span class="h-1 w-4 rounded bg-[#2e7d32]"></span> Sykkel</span>
            <span class="flex items-center gap-1"><span class="h-0.5 w-4 rounded bg-[#616161]"></span> Annet</span>
          </div>
        {/if}
        {#if stravaAvailable}
          <div class="flex items-center justify-between">
            <Label for="show-strava">Strava heatmap</Label>
            <Switch id="show-strava" bind:checked={showStravaHeat} />
          </div>
          {#if showStravaHeat}
            <Select.Root type="single" bind:value={stravaActivity}>
              <Select.Trigger class="w-full">
                {STRAVA_ACTIVITIES.find((a) => a.key === stravaActivity)?.label}
              </Select.Trigger>
              <Select.Content>
                {#each STRAVA_ACTIVITIES as a (a.key)}
                  <Select.Item value={a.key} label={a.label}>{a.label}</Select.Item>
                {/each}
              </Select.Content>
            </Select.Root>
          {/if}
        {/if}
      </Card.Content>
    </Card.Root>

    {#if groups.length}
      <Collapsible.Root>
        <Card.Root class="gap-3 py-4">
          <Card.Header class="px-4">
            <Collapsible.Trigger class="flex w-full items-center justify-between">
              <Card.Title class="flex items-center gap-2 text-sm">
                <SlidersHorizontal class="size-4" /> Vekting
                {#if wParam}<Badge>tilpasset</Badge>{/if}
              </Card.Title>
              <ChevronDown class="size-4 text-muted-foreground" />
            </Collapsible.Trigger>
          </Card.Header>
          <Collapsible.Content>
            <Card.Content class="flex flex-col gap-4 px-4">
              <p class="text-xs text-muted-foreground">
                Hvor mye hver faktor teller. 100 % = modellens egen vekting, 0 % = se bort fra faktoren.
              </p>
              {#each groups as g (g.key)}
                <div class="flex flex-col gap-2">
                  <div class="flex items-baseline justify-between text-sm">
                    <span>{g.label}</span>
                    <span class="font-medium tabular-nums">{weightsPct[g.key] ?? 100} %</span>
                  </div>
                  <Slider
                    type="single"
                    value={weightsPct[g.key] ?? 100}
                    onValueChange={(v) => (weightsPct[g.key] = v)}
                    min={0}
                    max={200}
                    step={10}
                    disabled={!showHeatmap}
                  />
                  <span class="text-[11px] text-muted-foreground">
                    Betydning i modellen: {Math.round(g.importance * 100)} %
                  </span>
                </div>
              {/each}
              <Button variant="outline" size="sm" onclick={resetWeights} disabled={!wParam}>
                <RotateCcw /> Tilbakestill vekting
              </Button>
            </Card.Content>
          </Collapsible.Content>
        </Card.Root>
      </Collapsible.Root>
    {/if}

    <Card.Root class="gap-3 py-4">
      <Card.Header class="px-4">
        <Card.Title class="flex items-center gap-2 text-sm"><MapPin class="size-4" /> Mine funn</Card.Title>
      </Card.Header>
      <Card.Content class="flex flex-col gap-3 px-4">
        <Button
          class="bg-success text-white hover:bg-success/90"
          disabled={!gps}
          title={gps ? `GPS ±${Math.round(gps.accuracy)} m` : 'Venter på GPS-posisjon'}
          onclick={() => gps && registerFinding(gps.lat, gps.lon, gps.accuracy)}
        >
          <Crosshair /> Registrer funn her
        </Button>
        <p class="text-xs text-muted-foreground">Eller trykk på kartet og velg «Jeg fant … her».</p>
        {#if findingMessage}
          <div class="rounded-md bg-accent px-3 py-2 text-xs">{findingMessage}</div>
        {/if}

        <Collapsible.Root bind:open={showMyList}>
          <Collapsible.Trigger
            class="flex w-full items-center justify-between text-sm font-medium disabled:opacity-60"
            disabled={!myFindings.length}
          >
            <span>{myFindings.length} funn lagret</span>
            {#if myFindings.length}<ChevronDown class="size-4 text-muted-foreground" />{/if}
          </Collapsible.Trigger>
          <Collapsible.Content>
            <ul class="mt-2 flex max-h-56 flex-col divide-y overflow-y-auto rounded-md border text-xs">
              {#each myFindings as f (f.id)}
                <li class="flex items-center justify-between gap-2 px-2 py-1.5">
                  <span>
                    {formatFoundAt(f.found_at)}
                    {#if f.accuracy_m !== null}<span class="text-muted-foreground">±{Math.round(f.accuracy_m)} m</span>{/if}
                  </span>
                  <span class="flex gap-1">
                    <Button variant="outline" size="sm" class="h-7 px-2" onclick={() => flyToFinding(f)}>Vis</Button>
                    <Button variant="ghost" size="sm" class="h-7 px-2 text-destructive" onclick={() => removeFinding(f.id)}>
                      Slett
                    </Button>
                  </span>
                </li>
              {/each}
            </ul>
          </Collapsible.Content>
        </Collapsible.Root>

        {#if status?.training}
          <div class="rounded-md bg-accent px-3 py-2 text-xs">Trener modellen… (noen minutter)</div>
        {:else}
          <Button variant="outline" disabled={!myFindings.length} onclick={retrain}>
            <Sparkles /> Tren modellen med mine funn
          </Button>
        {/if}
      </Card.Content>
    </Card.Root>

    {#if status?.model}
      <div class="grid grid-cols-2 gap-x-3 gap-y-1 px-2 pb-2 text-[11px] text-muted-foreground">
        <span>Treffsikkerhet (AUC)</span>
        <span class="text-right text-foreground">{status.model.cv_auc?.toFixed(2) ?? '–'}</span>
        <span>Funn i treningen</span>
        <span class="text-right text-foreground">
          {status.model.n_findings_used ?? status.model.n_findings}{#if status.model.n_own_findings_used}
            ({status.model.n_own_findings_used} egne){/if}
        </span>
        {#if status.model.habitat}
          <span>Habitat</span>
          <span class="text-right text-foreground">{status.model.habitat}</span>
        {/if}
        <span>Trent</span>
        <span class="text-right text-foreground">{formatFoundAt(status.model.trained_at)}</span>
      </div>
    {/if}
  </aside>

  {#if sidebarOpen}
    <button
      class="fixed inset-0 z-30 bg-black/30 md:hidden"
      onclick={() => (sidebarOpen = false)}
      aria-label="Lukk meny"
    ></button>
  {/if}

  <main class="relative min-w-0 flex-1">
    <!-- h-full/w-full rather than absolute: MapLibre's own CSS sets position: relative. -->
    <div class="h-full w-full" bind:this={mapEl}></div>
    <Button
      variant="secondary"
      size="icon"
      class="absolute top-[max(12px,env(safe-area-inset-top))] left-3 z-10 size-11 shadow-lg md:hidden"
      onclick={() => (sidebarOpen = true)}
      aria-label="Åpne meny"
    >
      <Menu />
    </Button>
    {#if point || pointLoading || pointError}
      <PointPanel
        {point}
        loading={pointLoading}
        error={pointError}
        {status}
        {routes}
        onclose={closePanel}
        onregister={(lat, lon) => registerFinding(lat, lon, null)}
      />
    {/if}
  </main>
</div>

<style>
  :global(.finding-popup) {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 13px;
  }

  :global(.finding-popup a) {
    color: var(--primary);
  }

  :global(.finding-popup button) {
    margin-top: 4px;
    border: 1px solid var(--border);
    border-radius: calc(var(--radius) - 2px);
    padding: 4px 8px;
    background: transparent;
    color: var(--destructive);
    cursor: pointer;
  }
</style>
