<script lang="ts">
  import type { PointInfo, Status, TrailRoute } from './api'
  import { Badge } from '$lib/components/ui/badge'
  import { Button } from '$lib/components/ui/button'
  import * as Card from '$lib/components/ui/card'
  import * as Collapsible from '$lib/components/ui/collapsible'
  import { Check, ChevronDown, ExternalLink, Footprints, Info, LoaderCircle, MapPinPlus, X } from '@lucide/svelte'

  interface Props {
    point: PointInfo | null
    loading: boolean
    error: string | null
    status: Status | null
    /** Hiking/ski/cycle routes near the spot (Kartverket, incl. DNT/UT.no). */
    routes: TrailRoute[]
    /** Only logged-in users can save findings. */
    canregister: boolean
    onclose: () => void
    onregister: (lat: number, lon: number) => void
  }

  let { point, loading, error, status, routes, canregister, onclose, onregister }: Props = $props()
  let registered = $state(false)

  // A new spot gets a fresh "register" button.
  $effect(() => {
    void point
    registered = false
  })

  function formatValue(value: number | string | null): string {
    if (value === null) return '–'
    if (typeof value === 'string') return value
    return Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(1)
  }

  // Largest absolute group contribution at this spot, for scaling the bars.
  let maxContribution = $derived(
    Math.max(0.01, ...(point?.groups ?? []).map((g) => Math.abs(g.contribution))),
  )
  let ownUsed = $derived(status?.model?.n_own_findings_used ?? 0)
  let gbifUsed = $derived((status?.model?.n_findings_used ?? 0) - ownUsed)
  let scorePct = $derived(point?.score === null || point?.score === undefined ? null : Math.round(point.score * 100))

  function importance(key: string): number | null {
    return status?.model?.feature_importance[key] ?? null
  }

  /** Badge colour by score, matching the map's yellow → red scale. */
  function scoreClass(pct: number): string {
    if (pct >= 90) return 'bg-[#bd0026] text-white'
    if (pct >= 75) return 'bg-[#f03b20] text-white'
    if (pct >= 50) return 'bg-[#fd8d3c] text-white'
    if (pct >= 25) return 'bg-[#fecc5c] text-black'
    return 'bg-muted text-muted-foreground'
  }
</script>

<Card.Root
  class="absolute bottom-[max(12px,env(safe-area-inset-bottom))] left-1/2 z-20 max-h-[60vh] w-[min(440px,calc(100vw-24px))] -translate-x-1/2 gap-3 overflow-y-auto py-4 shadow-2xl"
>
  <Button variant="ghost" size="icon" class="absolute top-2 right-2 size-8" onclick={onclose} aria-label="Lukk">
    <X />
  </Button>

  {#if loading}
    <Card.Content class="flex items-center gap-2 px-4 text-sm text-muted-foreground">
      <LoaderCircle class="size-4 animate-spin" /> Henter…
    </Card.Content>
  {:else if error}
    <Card.Content class="px-4 text-sm text-destructive">{error}</Card.Content>
  {:else if point}
    <Card.Header class="gap-1 px-4 pr-12">
      {#if !point.inside}
        <Card.Title>Utenfor dekningsområdet</Card.Title>
      {:else if scorePct === null}
        <Card.Title class="flex items-center gap-2"><Info class="size-4 text-muted-foreground" /> Ingen prediksjon her</Card.Title>
        <Card.Description>Vann eller bebyggelse, som ikke er med i kartet.</Card.Description>
      {:else}
        <div class="flex items-center gap-3">
          <span class="rounded-lg px-2.5 py-1 text-2xl font-semibold tabular-nums {scoreClass(scorePct)}">{scorePct}</span>
          <div>
            <Card.Title>{status?.model?.name ?? 'Valgt art'}</Card.Title>
            <Card.Description>
              {#if scorePct === 0 && status?.model?.habitat}
                Utenfor artens habitat ({status.model.habitat})
              {:else}
                Score av 100 (persentil i Norge)
              {/if}
            </Card.Description>
          </div>
        </div>
      {/if}
      <span class="text-[11px] text-muted-foreground tabular-nums">{point.lat.toFixed(5)}, {point.lon.toFixed(5)}</span>
    </Card.Header>

    <Card.Content class="flex flex-col gap-3 px-4">
      {#each routes as route, i (i)}
        <div class="rounded-lg border bg-accent/50 px-3 py-2 text-xs">
          <div class="mb-1 flex items-center gap-1.5 text-sm font-medium">
            <Footprints class="size-4" /> {route.type}
          </div>
          <dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
            {#each route.fields as f (f.label)}
              <dt class="text-muted-foreground">{f.label}</dt>
              <dd>{f.value}</dd>
            {/each}
          </dl>
        </div>
      {/each}

      {#if point.inside}
        {#if point.groups?.length}
          <div>
            <div class="mb-1.5 text-xs font-medium">Hva trekker opp og ned her</div>
            <ul class="flex flex-col gap-1 text-xs">
              {#each point.groups as g (g.key)}
                <li class="grid grid-cols-[1fr_1fr] items-center gap-2">
                  <span>
                    {g.label}{#if g.weight !== 1}<span class="text-muted-foreground"> ({Math.round(g.weight * 100)} %)</span>{/if}
                  </span>
                  <span class="relative h-2 rounded-full bg-muted">
                    <span class="absolute inset-y-0 left-1/2 w-px bg-border"></span>
                    <span
                      class="absolute inset-y-0 rounded-full {g.contribution >= 0 ? 'left-1/2 bg-success' : 'right-1/2 bg-destructive'}"
                      style:width="{(Math.abs(g.contribution) / maxContribution) * 50}%"
                    ></span>
                  </span>
                </li>
              {/each}
            </ul>
          </div>
        {/if}

        <div class="flex flex-wrap gap-2">
          <Button
            variant="outline"
            size="sm"
            href="https://www.google.com/maps/search/?api=1&query={point.lat.toFixed(6)},{point.lon.toFixed(6)}"
            target="_blank"
            rel="noopener"
          >
            <ExternalLink /> Google Maps
          </Button>
          {#if canregister}
          <Button
            size="sm"
            class="bg-success text-white hover:bg-success/90"
            disabled={registered}
            onclick={() => {
              onregister(point!.lat, point!.lon)
              registered = true
            }}
          >
            {#if registered}<Check /> Lagret{:else}<MapPinPlus /> Jeg fant {status?.model?.name ?? 'sopp'} her{/if}
          </Button>
          {/if}
        </div>

        {#if scorePct !== null}
          <p class="text-[11px] text-muted-foreground">
            Beregnet fra naturdataene under. Modellen har lært vektingen fra {gbifUsed} registrerte funn
            (Artsdatabanken via GBIF){#if ownUsed} og {ownUsed} egne funn{/if}, ikke fra funn akkurat her.
          </p>

          <Collapsible.Root>
            <Collapsible.Trigger class="flex w-full items-center justify-between rounded-md border px-3 py-2 text-xs font-medium hover:bg-accent">
              Naturdata ({point.features.length})
              <ChevronDown class="size-4 text-muted-foreground" />
            </Collapsible.Trigger>
            <Collapsible.Content>
              <table class="mt-2 w-full text-xs">
                <tbody class="divide-y">
                  {#each point.features as f (f.key)}
                    <tr>
                      <td class="py-1 pr-2">{f.label}</td>
                      <td class="py-1 text-right whitespace-nowrap tabular-nums">{formatValue(f.value)} {f.unit}</td>
                      <td class="w-14 py-1 pl-2">
                        {#if importance(f.key) !== null}
                          <div class="h-1.5 rounded-full bg-primary" style:width="{Math.min(100, importance(f.key)! * 300)}%"></div>
                        {/if}
                      </td>
                    </tr>
                  {/each}
                </tbody>
              </table>
              <p class="mt-1 text-[11px] text-muted-foreground">Stolpe = hvor viktig faktoren er i modellen.</p>
            </Collapsible.Content>
          </Collapsible.Root>
        {/if}
      {/if}
    </Card.Content>
  {/if}
</Card.Root>
