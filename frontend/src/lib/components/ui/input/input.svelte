<script lang="ts">
	import type { HTMLInputAttributes } from "svelte/elements";
	import { cn, type WithElementRef } from "$lib/utils.js";

	let {
		ref = $bindable(null),
		value = $bindable(),
		class: className,
		...restProps
	}: WithElementRef<HTMLInputAttributes> = $props();
</script>

<input
	bind:this={ref}
	bind:value
	data-slot="input"
	class={cn(
		"border-input placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-ring/50 dark:bg-input/30 flex h-9 w-full min-w-0 rounded-md border bg-transparent px-3 py-1 text-base shadow-xs transition-[color,box-shadow] outline-none focus-visible:ring-3 disabled:cursor-not-allowed disabled:opacity-50 md:text-sm",
		className
	)}
	{...restProps}
/>
