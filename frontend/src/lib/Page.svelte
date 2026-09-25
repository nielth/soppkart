<script lang="ts">
  import type { Snippet } from 'svelte'
  import { Button } from '$lib/components/ui/button'
  import { ArrowLeft } from '@lucide/svelte'
  import { navigate } from './router.svelte'

  let { children, wide = false }: { children: Snippet; wide?: boolean } = $props()
</script>

<!-- A full-screen page over the map, with a way back to it. -->
<div class="fixed inset-0 z-50 overflow-y-auto bg-background">
  <div
    class="mx-auto flex min-h-full w-full flex-col gap-6 px-4 pt-[calc(env(safe-area-inset-top)+16px)] pb-10 {wide
      ? 'max-w-2xl'
      : 'max-w-sm'}"
  >
    <Button variant="ghost" size="sm" class="self-start" onclick={() => navigate('/')}>
      <ArrowLeft /> Tilbake til kartet
    </Button>
    <div class="flex flex-1 flex-col {wide ? '' : 'justify-center pb-16'}">
      {@render children()}
    </div>
  </div>
</div>
