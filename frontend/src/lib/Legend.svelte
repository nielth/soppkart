<script lang="ts">
  interface Props {
    /** Percent of the species' habitat that is coloured (the best squares). */
    topPct: number
  }

  let { topPct }: Props = $props()

  // Must match RAMP in backend/src/soppkart/colormap.py
  const ramp: [number, string][] = [
    [0, '#ffffb2'],
    [0.3, '#fecc5c'],
    [0.6, '#fd8d3c'],
    [0.8, '#f03b20'],
    [1, '#bd0026'],
  ]
  const gradient = `linear-gradient(to right, ${ramp.map(([v, c]) => `${c} ${v * 100}%`).join(', ')})`
</script>

<div class="flex flex-col gap-1">
  <div class="h-2.5 rounded-full border" style:background={gradient}></div>
  <div class="flex justify-between text-[11px] text-muted-foreground">
    <span>Topp {topPct} %</span>
    <span>Beste</span>
  </div>
  <p class="text-[11px] text-muted-foreground">Resten av habitatet er ufarget. Trykk på kartet for detaljer.</p>
</div>
