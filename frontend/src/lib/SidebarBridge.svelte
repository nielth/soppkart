<script lang="ts">
  // Hands the sidebar's state to App.svelte, which renders the sidebar provider
  // itself and so can't read its context directly.
  import { useSidebar } from '$lib/components/ui/sidebar'

  let { sidebar = $bindable() }: { sidebar?: ReturnType<typeof useSidebar> } = $props()
  sidebar = useSidebar()
</script>
