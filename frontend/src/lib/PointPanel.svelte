<script lang="ts">
  import type { PointInfo, Status } from './api'

  interface Props {
    point: PointInfo | null
    loading: boolean
    error: string | null
    status: Status | null
    onclose: () => void
    onregister: (lat: number, lon: number) => void
  }

  let { point, loading, error, status, onclose, onregister }: Props = $props()
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

  function importance(key: string): number | null {
    return status?.model?.feature_importance[key] ?? null
  }
</script>

<div class="panel">
  <button class="close" onclick={onclose} aria-label="Lukk">×</button>
  {#if loading}
    <p>Henter…</p>
  {:else if error}
    <p class="error">{error}</p>
  {:else if point}
    <div class="coords">{point.lat.toFixed(5)}, {point.lon.toFixed(5)}</div>
    {#if !point.inside}
      <p>Utenfor dekningsområdet.</p>
    {:else}
      <div class="score">
        {#if point.score === null}
          Ingen prediksjon her (vann eller bebyggelse)
        {:else}
          Score for {status?.model?.name ?? "valgt art"}: <strong>{Math.round(point.score * 100)}</strong> / 100
          {#if point.score === 0 && status?.model?.habitat}
            <div class="habitat">Utenfor artens habitat ({status.model.habitat}).</div>
          {/if}
        {/if}
      </div>
      <button
        class="register"
        disabled={registered}
        onclick={() => {
          onregister(point!.lat, point!.lon)
          registered = true
        }}
      >
        {registered ? 'Lagret ✓' : `Jeg fant ${status?.model?.name ?? 'sopp'} her`}
      </button>
      <table>
        <tbody>
          {#each point.features as f (f.key)}
            <tr>
              <td>{f.label}</td>
              <td class="num">{formatValue(f.value)} {f.unit}</td>
              <td class="imp">
                {#if importance(f.key) !== null}
                  <div class="imp-bar" style:width="{Math.min(100, importance(f.key)! * 300)}%"></div>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
      <div class="hint">Stolpe = hvor viktig variabelen er i modellen.</div>
    {/if}
  {/if}
</div>

<style>
  .panel {
    position: absolute;
    left: 50%;
    transform: translateX(-50%);
    bottom: max(10px, env(safe-area-inset-bottom));
    width: min(420px, calc(100vw - 20px));
    max-height: 55vh;
    overflow-y: auto;
    background: var(--panel-bg);
    color: var(--text);
    border-radius: 12px;
    padding: 12px 14px;
    box-shadow: 0 2px 12px rgba(0, 0, 0, 0.3);
    font-size: 14px;
  }

  .close {
    position: absolute;
    top: 4px;
    right: 8px;
    border: none;
    background: none;
    font-size: 24px;
    color: var(--muted);
    cursor: pointer;
  }

  .coords {
    font-size: 12px;
    color: var(--muted);
  }

  .score {
    font-size: 16px;
    margin: 6px 0 8px;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }

  td {
    padding: 3px 4px;
    border-top: 1px solid var(--border);
  }

  .num {
    text-align: right;
    white-space: nowrap;
  }

  .imp {
    width: 60px;
  }

  .imp-bar {
    height: 6px;
    background: #fd8d3c;
    border-radius: 3px;
  }

  .hint {
    font-size: 11px;
    color: var(--muted);
    margin-top: 6px;
  }

  .register {
    border: 1px solid #1a9641;
    background: transparent;
    color: #1a9641;
    border-radius: 6px;
    padding: 5px 8px;
    font-size: 13px;
    margin-bottom: 8px;
    cursor: pointer;
  }

  .habitat {
    font-size: 12px;
    color: var(--muted);
  }

  .error {
    color: #b00020;
  }
</style>
