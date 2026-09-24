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
    formatFoundAt,
    fetchStatus,
    findingsUrl,
    imageryUrl,
    stravaHeatmapUrl,
    TRAILS_URL,
    myFindingsUrl,
    startRetrain,
    tilesUrl,
    weightsParam,
    type MyFinding,
    type PointInfo,
    type Species,
    type Status,
  } from './lib/api'
  import Legend from './lib/Legend.svelte'
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
  let showFindings = $state(false)
  let showTrails = $state(false)
  let point = $state<PointInfo | null>(null)
  let pointLoading = $state(false)
  let pointError = $state<string | null>(null)
  let selected: { lat: number; lon: number } | null = null
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
  let esriKey = $state<string | null>(null)
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
      if (esriKey) addImagery(esriKey)
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
      map!.on('click', 'my-findings', (e) => {
        const feature = e.features?.[0]
        if (feature?.geometry.type === 'Point') showMyFinding(feature)
      })
      map!.on('click', 'findings-points', (e) => {
        const feature = e.features?.[0]
        if (feature?.geometry.type === 'Point') showGbifFinding(feature)
      })
      map!.on('click', 'findings-clusters', async (e) => {
        const feature = e.features?.[0]
        if (!feature || feature.geometry.type !== 'Point') return
        const source = map!.getSource<maplibregl.GeoJSONSource>('findings')
        const zoom = await source!.getClusterExpansionZoom(feature.properties.cluster_id as number)
        const [lon, lat] = feature.geometry.coordinates
        map!.easeTo({ center: [lon, lat], zoom })
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

    map.on('click', (e) => {
      // Clicks on findings open their popup (or zoom into a cluster) instead.
      const layers = ['my-findings', 'findings-points', 'findings-clusters'].filter((id) => map!.getLayer(id))
      if (map!.queryRenderedFeatures(e.point, { layers }).length) return
      selectPoint(e.lngLat.lat, e.lngLat.lng)
    })

    fetchConfig()
      .then((c) => {
        esriKey = c.esri_api_key
        if (esriKey && map?.isStyleLoaded() && !map.getLayer('imagery')) addImagery(esriKey)
      })
      .catch(() => (esriKey = null))

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
    const popup = new maplibregl.Popup({ closeButton: true }).setLngLat([lon, lat]).setDOMContent(content).addTo(map!)
    popup.on('close', () => source?.setData(EMPTY_COLLECTION))
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
  function addImagery(apiKey: string) {
    map!.addSource('imagery', {
      type: 'raster',
      tiles: [imageryUrl(apiKey)],
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

  function openStrava() {
    if (!map) return
    const { lat, lng } = map.getCenter()
    window.open(stravaHeatmapUrl(lat, lng, Math.max(map.getZoom(), 12)), '_blank', 'noopener')
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
    const showImagery = basemap === 'flyfoto' && esriKey !== null
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

  function selectPoint(lat: number, lon: number) {
    if (!map) return
    marker?.remove()
    marker = new maplibregl.Marker({ color: '#7a3e00' }).setLngLat([lon, lat]).addTo(map)
    selected = { lat, lon }
    loadPoint(lat, lon)
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
    selected = null
    marker?.remove()
    marker = undefined
  }
</script>

<div class="layout">
  <aside class="sidebar" class:open={sidebarOpen}>
    <header>
      <div class="title">🍄 Soppkart</div>
      <button class="close" onclick={() => (sidebarOpen = false)} aria-label="Lukk meny">×</button>
    </header>

    {#if statusError}
      <div class="warn">Backend utilgjengelig: {statusError}</div>
    {:else if status && !status.ready}
      <div class="warn">{status.message}</div>
    {/if}

    <section>
      <h2>Art</h2>
      <div class="species" role="radiogroup" aria-label="Art">
        {#each speciesList as s (s.key)}
          <button
            role="radio"
            aria-checked={s.key === species}
            class:active={s.key === species}
            onclick={() => (species = s.key)}
          >
            <span>{s.name}</span>
            <small>{s.latin}</small>
          </button>
        {/each}
      </div>
    </section>

    <section>
      <h2>Kart</h2>
      <div class="basemap" role="radiogroup" aria-label="Bakgrunnskart">
        <button role="radio" aria-checked={basemap === 'kart'} class:active={basemap === 'kart'} onclick={() => (basemap = 'kart')}>
          Kart
        </button>
        <button
          role="radio"
          aria-checked={basemap === 'flyfoto'}
          class:active={basemap === 'flyfoto'}
          disabled={!esriKey}
          title={esriKey ? 'Flyfoto (Esri World Imagery)' : 'Flyfoto krever ESRI_API_KEY på serveren'}
          onclick={() => (basemap = 'flyfoto')}
        >
          Flyfoto
        </button>
      </div>
      <label class="check">
        <input type="checkbox" bind:checked={showHeatmap} /> Vis sannsynlighet
      </label>
      <label class="slider">
        <span>Vis topp <strong>{topPct} %</strong></span>
        <input type="range" min="1" max="100" step="1" bind:value={topPct} disabled={!showHeatmap} />
      </label>
      <Legend {topPct} />
      <label class="slider">
        <span>Gjennomsiktighet</span>
        <input type="range" min="0" max="1" step="0.05" bind:value={opacity} disabled={!showHeatmap} />
      </label>
      <label class="check">
        <input type="checkbox" bind:checked={showFindings} /> Vis registrerte funn (Artsdatabanken)
      </label>
      <label class="check">
        <input type="checkbox" bind:checked={showTrails} /> Vis turstier (Kartverket)
      </label>
      <button class="action" onclick={openStrava}>Åpne Strava heatmap her ↗</button>
    </section>

    {#if groups.length}
      <section>
        <h2>Vekting</h2>
        <div class="hint">
          Hvor mye hver faktor teller i sannsynligheten. 100 % = modellens egen vekting, 0 % = se bort fra
          faktoren.
        </div>
        {#each groups as g (g.key)}
          <label class="slider">
            <span class="weight-label">
              <span>{g.label}</span>
              <strong>{weightsPct[g.key] ?? 100} %</strong>
            </span>
            <input
              type="range"
              min="0"
              max="200"
              step="10"
              value={weightsPct[g.key] ?? 100}
              oninput={(e) => (weightsPct[g.key] = Number(e.currentTarget.value))}
              disabled={!showHeatmap}
            />
            <small class="muted">Betydning i modellen: {Math.round(g.importance * 100)} %</small>
          </label>
        {/each}
        <button class="action" onclick={resetWeights} disabled={!wParam}>Tilbakestill vekting</button>
      </section>
    {/if}

    <section>
      <h2>Mine funn</h2>
      <button
        class="action primary"
        disabled={!gps}
        title={gps ? `GPS ±${Math.round(gps.accuracy)} m` : 'Venter på GPS-posisjon'}
        onclick={() => gps && registerFinding(gps.lat, gps.lon, gps.accuracy)}
      >
        📍 Registrer funn her
      </button>
      <div class="hint">Eller trykk på kartet og velg «Jeg fant … her».</div>
      {#if findingMessage}
        <div class="message">{findingMessage}</div>
      {/if}

      <button class="list-toggle" onclick={() => (showMyList = !showMyList)} disabled={!myFindings.length}>
        <span>{myFindings.length} funn lagret</span>
        {#if myFindings.length}<span class="muted">{showMyList ? '▴ skjul' : '▾ vis'}</span>{/if}
      </button>
      {#if showMyList && myFindings.length}
        <ul class="mine-list">
          {#each myFindings as f (f.id)}
            <li>
              <span class="when">
                {formatFoundAt(f.found_at)}
                {#if f.accuracy_m !== null}<small>±{Math.round(f.accuracy_m)} m</small>{/if}
              </span>
              <span class="actions">
                <button class="action" onclick={() => flyToFinding(f)}>Vis</button>
                <button class="action" onclick={() => removeFinding(f.id)}>Slett</button>
              </span>
            </li>
          {/each}
        </ul>
      {/if}

      {#if status?.training}
        <div class="message">Trener modellen… (noen minutter)</div>
      {:else}
        <button class="action" disabled={!myFindings.length} onclick={retrain}>Tren modellen med mine funn</button>
      {/if}
    </section>

    {#if status?.model}
      <section class="about">
        <h2>Om modellen</h2>
        <dl>
          <dt>Treffsikkerhet (AUC)</dt>
          <dd>{status.model.cv_auc?.toFixed(2) ?? '–'}</dd>
          <dt>Funn brukt i treningen</dt>
          <dd>{status.model.n_findings_used ?? status.model.n_findings}{#if status.model.n_own_findings_used}, {status.model.n_own_findings_used} egne{/if}</dd>
          {#if status.model.habitat}
            <dt>Habitat</dt>
            <dd>{status.model.habitat}</dd>
          {/if}
          <dt>Trent</dt>
          <dd>{formatFoundAt(status.model.trained_at)}</dd>
        </dl>
      </section>
    {/if}
  </aside>

  {#if sidebarOpen}
    <button class="backdrop" onclick={() => (sidebarOpen = false)} aria-label="Lukk meny"></button>
  {/if}

  <main class="map-area">
    <div class="map" bind:this={mapEl}></div>
    <button class="menu" onclick={() => (sidebarOpen = true)} aria-label="Åpne meny">☰</button>
    {#if point || pointLoading || pointError}
      <PointPanel
        {point}
        loading={pointLoading}
        error={pointError}
        {status}
        onclose={closePanel}
        onregister={(lat, lon) => registerFinding(lat, lon, null)}
      />
    {/if}
  </main>
</div>

<style>
  .layout {
    display: flex;
    height: 100%;
  }

  .sidebar {
    width: 300px;
    flex-shrink: 0;
    overflow-y: auto;
    background: var(--sidebar-bg);
    color: var(--text);
    border-right: 1px solid var(--border);
    padding: max(14px, env(safe-area-inset-top)) 16px 20px max(16px, env(safe-area-inset-left));
    display: flex;
    flex-direction: column;
    gap: 18px;
    font-size: 14px;
    z-index: 4;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .title {
    font-weight: 700;
    font-size: 18px;
  }

  h2 {
    margin: 0 0 8px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--muted);
  }

  section {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .species {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px;
  }

  .species button {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 1px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    border-radius: 8px;
    padding: 7px 9px;
    cursor: pointer;
    text-align: left;
  }

  .species button span {
    font-weight: 600;
    font-size: 13px;
  }

  .species button small {
    font-size: 11px;
    font-style: italic;
    color: var(--muted);
  }

  .species button.active {
    border-color: var(--accent);
    background: var(--accent-soft);
    box-shadow: inset 0 0 0 1px var(--accent);
  }

  .basemap {
    display: grid;
    grid-template-columns: 1fr 1fr;
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
  }

  .basemap button {
    border: none;
    background: var(--surface);
    color: var(--text);
    padding: 6px;
    font-size: 13px;
    cursor: pointer;
  }

  .basemap button + button {
    border-left: 1px solid var(--border);
  }

  .basemap button.active {
    background: var(--accent-soft);
    font-weight: 600;
  }

  .basemap button:disabled {
    color: var(--muted);
    cursor: default;
  }

  .check {
    display: flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
  }

  .slider {
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-size: 13px;
  }

  .weight-label {
    display: flex;
    justify-content: space-between;
  }

  .slider input {
    width: 100%;
    accent-color: var(--accent);
  }

  .action {
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    border-radius: 8px;
    padding: 8px 10px;
    font-size: 13px;
    cursor: pointer;
  }

  .action.primary {
    background: var(--green);
    border-color: var(--green);
    color: #fff;
    font-weight: 600;
  }

  .action:disabled,
  .list-toggle:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .list-toggle {
    display: flex;
    justify-content: space-between;
    border: none;
    background: none;
    color: var(--text);
    padding: 4px 0 0;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
  }

  .muted,
  .hint {
    color: var(--muted);
    font-weight: 400;
    font-size: 12px;
  }

  .message {
    font-size: 12px;
    background: var(--accent-soft);
    border-radius: 6px;
    padding: 6px 8px;
  }

  .mine-list {
    list-style: none;
    margin: 0;
    padding: 0;
    max-height: 220px;
    overflow-y: auto;
    font-size: 12px;
  }

  .mine-list li {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 6px;
    padding: 5px 0;
    border-top: 1px solid var(--border);
  }

  .mine-list .when small {
    color: var(--muted);
    margin-left: 4px;
  }

  .mine-list .actions {
    display: flex;
    gap: 4px;
  }

  .mine-list .action {
    padding: 3px 7px;
    font-size: 12px;
  }

  .about dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 4px 10px;
    margin: 0;
    font-size: 12px;
  }

  .about dt {
    color: var(--muted);
  }

  .about dd {
    margin: 0;
    text-align: right;
  }

  .warn {
    background: #fff4d6;
    color: #6b4a00;
    border-radius: 6px;
    padding: 6px 8px;
    font-size: 12px;
  }

  .map-area {
    position: relative;
    flex: 1;
    min-width: 0;
  }

  .map {
    position: absolute;
    inset: 0;
  }

  .close,
  .menu,
  .backdrop {
    display: none;
  }

  :global(.finding-popup) {
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 13px;
  }

  /* Phones: the sidebar becomes a drawer opened with the ☰ button. */
  @media (max-width: 700px) {
    .sidebar {
      position: fixed;
      top: 0;
      bottom: 0;
      left: 0;
      width: min(320px, 86vw);
      transform: translateX(-100%);
      transition: transform 0.2s ease;
      box-shadow: 2px 0 16px rgba(0, 0, 0, 0.25);
    }

    .sidebar.open {
      transform: none;
    }

    .close {
      display: block;
      border: none;
      background: none;
      color: var(--muted);
      font-size: 26px;
      line-height: 1;
      cursor: pointer;
    }

    .backdrop {
      display: block;
      position: fixed;
      inset: 0;
      z-index: 3;
      border: none;
      background: rgba(0, 0, 0, 0.3);
    }

    .menu {
      display: block;
      position: absolute;
      top: max(10px, env(safe-area-inset-top));
      left: max(10px, env(safe-area-inset-left));
      z-index: 1;
      width: 44px;
      height: 44px;
      border: none;
      border-radius: 10px;
      background: var(--sidebar-bg);
      color: var(--text);
      font-size: 20px;
      box-shadow: 0 2px 10px rgba(0, 0, 0, 0.25);
      cursor: pointer;
    }
  }
</style>
