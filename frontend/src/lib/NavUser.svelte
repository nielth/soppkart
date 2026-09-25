<script lang="ts">
  import type { User } from './api'
  import * as Avatar from '$lib/components/ui/avatar'
  import * as DropdownMenu from '$lib/components/ui/dropdown-menu'
  import * as Sidebar from '$lib/components/ui/sidebar'
  import { useSidebar } from '$lib/components/ui/sidebar'
  import { ChevronsUpDown, KeyRound, LogIn, LogOut, UserRound, Users } from '@lucide/svelte'
  import { navigate } from './router.svelte'

  interface Props {
    user: User | null
    onlogout: () => void
  }

  let { user, onlogout }: Props = $props()
  const sidebar = useSidebar()

  let initials = $derived(user ? user.username.slice(0, 2).toUpperCase() : '')

  function go(path: string) {
    sidebar.setOpenMobile(false)
    navigate(path)
  }
</script>

{#snippet identity()}
  <Avatar.Root class="size-8 rounded-lg">
    <Avatar.Fallback class="rounded-lg">
      {#if user}{initials}{:else}<UserRound class="size-4" />{/if}
    </Avatar.Fallback>
  </Avatar.Root>
  <div class="grid flex-1 text-left text-sm leading-tight">
    <span class="truncate font-semibold">{user ? user.username : 'Gjest'}</span>
    <span class="truncate text-xs text-muted-foreground">
      {user ? (user.is_admin ? 'Admin' : 'Bruker') : 'Logg inn eller registrer deg'}
    </span>
  </div>
{/snippet}

<!-- Who is logged in, at the bottom of the sidebar: the account menu, or for guests the way to the login page. -->
<Sidebar.Menu>
  <Sidebar.MenuItem>
    {#if user}
      <DropdownMenu.Root>
        <DropdownMenu.Trigger>
          {#snippet child({ props })}
            <Sidebar.MenuButton
              size="lg"
              class="data-[state=open]:bg-sidebar-accent data-[state=open]:text-sidebar-accent-foreground"
              {...props}
            >
              {@render identity()}
              <ChevronsUpDown class="ml-auto size-4" />
            </Sidebar.MenuButton>
          {/snippet}
        </DropdownMenu.Trigger>
        <DropdownMenu.Content
          class="w-(--bits-dropdown-menu-anchor-width) min-w-56 rounded-lg"
          side={sidebar.isMobile ? 'top' : 'right'}
          align="end"
          sideOffset={4}
        >
          <DropdownMenu.Label class="font-normal">
            Innlogget som <span class="font-semibold">{user.username}</span>
          </DropdownMenu.Label>
          <DropdownMenu.Separator />
          {#if user.is_admin}
            <DropdownMenu.Item onSelect={() => go('/brukere')}><Users /> Brukere</DropdownMenu.Item>
          {/if}
          <DropdownMenu.Item onSelect={() => go('/konto')}><KeyRound /> Bytt passord</DropdownMenu.Item>
          <DropdownMenu.Separator />
          <DropdownMenu.Item onSelect={onlogout}><LogOut /> Logg ut</DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Root>
    {:else}
      <Sidebar.MenuButton size="lg" onclick={() => go('/login')}>
        {@render identity()}
        <LogIn class="ml-auto size-4" />
      </Sidebar.MenuButton>
    {/if}
  </Sidebar.MenuItem>
</Sidebar.Menu>
