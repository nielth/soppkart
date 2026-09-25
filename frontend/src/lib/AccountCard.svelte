<script lang="ts">
  import { changePassword, login, logout, type User } from './api'
  import { Badge } from '$lib/components/ui/badge'
  import { Button } from '$lib/components/ui/button'
  import * as Card from '$lib/components/ui/card'
  import * as Collapsible from '$lib/components/ui/collapsible'
  import { Input } from '$lib/components/ui/input'
  import { ChevronDown, LogIn, LogOut, UserRound, Users } from '@lucide/svelte'

  interface Props {
    user: User | null
    /** Called after logging in or out, so the app can reload what the user can see. */
    onchange: (user: User | null) => void
    onadmin: () => void
  }

  let { user, onchange, onadmin }: Props = $props()
  let username = $state('')
  let password = $state('')
  let error = $state<string | null>(null)
  let busy = $state(false)
  let showPasswordForm = $state(false)
  let currentPassword = $state('')
  let newPassword = $state('')
  let passwordMessage = $state<string | null>(null)

  async function submitLogin(e: SubmitEvent) {
    e.preventDefault()
    busy = true
    error = null
    try {
      const u = await login(username.trim(), password)
      password = ''
      onchange(u)
    } catch (err) {
      error = err instanceof Error ? err.message : String(err)
    } finally {
      busy = false
    }
  }

  async function submitLogout() {
    await logout().catch(() => {})
    onchange(null)
  }

  async function submitPassword(e: SubmitEvent) {
    e.preventDefault()
    passwordMessage = null
    try {
      await changePassword(currentPassword, newPassword)
      currentPassword = newPassword = ''
      // A new password ends every session, including this one.
      onchange(null)
    } catch (err) {
      passwordMessage = err instanceof Error ? err.message : String(err)
    }
  }
</script>

<Card.Root class="gap-3 py-4">
  <Card.Header class="px-4">
    <Card.Title class="flex items-center gap-2 text-sm"><UserRound class="size-4" /> Konto</Card.Title>
  </Card.Header>
  <Card.Content class="flex flex-col gap-3 px-4">
    {#if user}
      <div class="flex items-center justify-between gap-2 text-sm">
        <span>Innlogget som <strong>{user.username}</strong></span>
        {#if user.is_admin}<Badge variant="secondary">admin</Badge>{/if}
      </div>
      <div class="flex flex-wrap gap-2">
        {#if user.is_admin}
          <Button variant="outline" size="sm" onclick={onadmin}><Users /> Brukere</Button>
        {/if}
        <Button variant="outline" size="sm" onclick={submitLogout}><LogOut /> Logg ut</Button>
      </div>
      <Collapsible.Root bind:open={showPasswordForm}>
        <Collapsible.Trigger class="flex w-full items-center justify-between text-xs text-muted-foreground">
          Bytt passord <ChevronDown class="size-4" />
        </Collapsible.Trigger>
        <Collapsible.Content>
          <form class="mt-2 flex flex-col gap-2" onsubmit={submitPassword}>
            <Input type="password" placeholder="Nåværende passord" autocomplete="current-password" bind:value={currentPassword} required />
            <Input type="password" placeholder="Nytt passord (minst 8 tegn)" autocomplete="new-password" minlength={8} bind:value={newPassword} required />
            <Button type="submit" size="sm" variant="outline">Lagre nytt passord</Button>
            {#if passwordMessage}<p class="text-xs text-destructive">{passwordMessage}</p>{/if}
          </form>
        </Collapsible.Content>
      </Collapsible.Root>
    {:else}
      <p class="text-xs text-muted-foreground">Logg inn for flyfoto, Strava, flere arter og for å lagre egne funn.</p>
      <form class="flex flex-col gap-2" onsubmit={submitLogin}>
        <Input placeholder="Brukernavn" autocomplete="username" autocapitalize="none" bind:value={username} required />
        <Input type="password" placeholder="Passord" autocomplete="current-password" bind:value={password} required />
        <Button type="submit" disabled={busy}><LogIn /> Logg inn</Button>
        {#if error}<p class="text-xs text-destructive">{error}</p>{/if}
      </form>
    {/if}
  </Card.Content>
</Card.Root>
