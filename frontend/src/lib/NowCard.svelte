<script lang="ts">
  import { fetchNow, formatFoundAt, type NowFactor, type NowInfo } from './api'
  import { isNetworkError } from './offline'
  import { CircleCheck, CircleDot, CircleX, Info, LoaderCircle } from '@lucide/svelte'

  interface Props {
    species: string
    lat: number
    lon: number
    /** Where the spot is, e.g. "midt på kartet". */
    place?: string
  }

  let { species, lat, lon, place }: Props = $props()
  let info = $state<NowInfo | null>(null)
  let error = $state<string | null>(null)
  let loading = $state(false)

  // Refetch only when the spot moves to another ~1 km square (the weather's resolution).
  let key = $derived(`${species}:${lat.toFixed(2)}:${lon.toFixed(2)}`)
  $effect(() => {
    const k = key
    loading = true
    error = null
    fetchNow(species, lat, lon)
      .then((result) => {
        if (k === key) info = result
      })
      .catch((err) => {
        if (k !== key) return
        info = null
        error = isNetworkError(err)
          ? 'Uten nett: ingen værdata lagret for dette stedet.'
          : String(err instanceof Error ? err.message : err)
      })
      .finally(() => {
        if (k === key) loading = false
      })
  })

  /** MET's symbol codes (e.g. "lightrainshowers_day") as an emoji. */
  function emoji(symbol: string | null): string {
    if (!symbol) return ''
    const night = symbol.endsWith('_night')
    const s = symbol.replace(/_(day|night|polartwilight)$/, '')
    if (s.includes('thunder')) return '⛈️'
    if (s.includes('snow')) return '❄️'
    if (s.includes('sleet')) return '🌨️'
    if (s.includes('showers')) return '🌦️'
    if (s.includes('rain')) return '🌧️'
    if (s === 'fog') return '🌫️'
    if (s === 'cloudy') return '☁️'
    if (s === 'partlycloudy') return '⛅'
    if (s === 'fair') return night ? '🌙' : '🌤️'
    if (s === 'clearsky') return night ? '🌙' : '☀️'
    return '☁️'
  }

  function hour(time: string): string {
    return new Date(time).toLocaleTimeString('no', { hour: '2-digit' })
  }

  function labelClass(score: number): string {
    if (score >= 60) return 'bg-[#2e7d32] text-white'
    if (score >= 35) return 'bg-[#7cb342] text-white'
    if (score >= 15) return 'bg-[#fdd835] text-black'
    return 'bg-muted text-muted-foreground'
  }

  const ICONS: Record<NowFactor['status'], { icon: typeof CircleCheck; class: string }> = {
    good: { icon: CircleCheck, class: 'text-[#2e7d32]' },
    ok: { icon: CircleDot, class: 'text-[#f9a825]' },
    bad: { icon: CircleX, class: 'text-destructive' },
    info: { icon: Info, class: 'text-muted-foreground' },
  }
</script>

<div class="flex flex-col gap-2 text-xs">
  {#if !info && loading}
    <div class="flex items-center gap-2 text-muted-foreground"><LoaderCircle class="size-4 animate-spin" /> Henter vær…</div>
  {:else if !info && error}
    <div class="text-destructive">{error}</div>
  {:else if info}
    <div class="flex items-center gap-2" class:opacity-60={loading}>
      <span class="rounded-md px-2 py-0.5 text-base font-semibold tabular-nums {labelClass(info.score)}">{info.score}</span>
      <div class="flex flex-col">
        <span class="text-sm font-medium">{info.label}</span>
        {#if place}<span class="text-[11px] text-muted-foreground">{place}</span>{/if}
      </div>
    </div>

    <div class="flex flex-col gap-0.5">
      <div class="flex items-center gap-2 text-sm">
        <span class="text-xl">{emoji(info.current.symbol)}</span>
        {#if info.current.temp_c !== null}<span class="font-medium">{info.current.temp_c.toFixed(1)}°</span>{/if}
        <span class="text-muted-foreground">· {info.current.hiking}</span>
      </div>
      <div class="text-muted-foreground">
        {#if info.current.wind}💨 {info.current.wind}{/if}
        {#if info.current.humidity_pct !== null}&nbsp;💧 {Math.round(info.current.humidity_pct)}% luftfuktighet{/if}
      </div>
    </div>

    {#if info.next_hours.length}
      <div>
        <div class="mb-1 text-[11px] text-muted-foreground">Temperatur, vind og regn de neste 6 timene</div>
        <div class="grid grid-cols-6 gap-1 text-center tabular-nums">
          {#each info.next_hours as h (h.time)}
            <div class="flex flex-col rounded-md bg-muted/60 py-1">
              <span class="text-[10px] text-muted-foreground">{hour(h.time)}</span>
              <span>{emoji(h.symbol)}</span>
              <span>{h.temp_c === null ? '–' : `${Math.round(h.temp_c)}°`}</span>
              <span class="text-[10px] text-muted-foreground">{h.wind_ms === null ? '' : `${Math.round(h.wind_ms)} m/s`}</span>
              <span class="text-[10px] text-[#1e88e5]">{h.precip_mm ? `${h.precip_mm.toFixed(1)} mm` : ''}</span>
            </div>
          {/each}
        </div>
      </div>
    {/if}

    <ul class="flex flex-col gap-1">
      {#each info.factors as f (f.text)}
        {@const Icon = ICONS[f.status].icon}
        <li class="flex items-start gap-1.5">
          <Icon class="mt-px size-3.5 shrink-0 {ICONS[f.status].class}" />
          <span>{f.text}</span>
        </li>
      {/each}
    </ul>

    <p class="text-[11px] text-muted-foreground">
      {info.note} Oppdatert {formatFoundAt(info.updated_at)}.
    </p>
  {/if}
</div>
