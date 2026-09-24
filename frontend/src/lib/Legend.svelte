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

<div class="legend">
  <div class="bar" style:background={gradient}></div>
  <div class="labels">
    <span>Topp {topPct} %</span>
    <span>Beste</span>
  </div>
  <div class="hint">Resten av habitatet er ufarget. Trykk på kartet for detaljer.</div>
</div>

<style>
  .bar {
    height: 10px;
    border-radius: 3px;
    border: 1px solid rgba(0, 0, 0, 0.2);
  }

  .labels {
    display: flex;
    justify-content: space-between;
    font-size: 12px;
    color: var(--muted);
  }

  .hint {
    font-size: 11px;
    color: var(--muted);
  }
</style>
