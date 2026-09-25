<script lang="ts">
  import { onMount } from 'svelte'
  import {
    addUser,
    deleteUser,
    fetchUsers,
    formatFoundAt,
    updateUser,
    type AdminUser,
    type AdminUsers,
    type User,
  } from './api'
  import { Badge } from '$lib/components/ui/badge'
  import { Button } from '$lib/components/ui/button'
  import * as Card from '$lib/components/ui/card'
  import { Input } from '$lib/components/ui/input'
  import { Label } from '$lib/components/ui/label'
  import { Separator } from '$lib/components/ui/separator'
  import { Switch } from '$lib/components/ui/switch'
  import { KeyRound, Trash2, UserPlus } from '@lucide/svelte'
  import Page from './Page.svelte'

  interface Props {
    /** The admin using the page (can't remove their own admin access). */
    me: User
  }

  let { me }: Props = $props()
  let data = $state<AdminUsers | null>(null)
  let error = $state<string | null>(null)
  let newUsername = $state('')
  let newPassword = $state('')
  let newIsAdmin = $state(false)
  let newPermissions = $state<string[]>([])

  onMount(load)

  async function load() {
    try {
      data = await fetchUsers()
    } catch (err) {
      error = message(err)
    }
  }

  function message(err: unknown): string {
    return err instanceof Error ? err.message : String(err)
  }

  /** Run a change, then show the users as the server now has them. */
  async function change(action: () => Promise<unknown>) {
    error = null
    try {
      await action()
    } catch (err) {
      error = message(err)
    }
    await load()
  }

  function togglePermission(user: AdminUser, key: string, on: boolean) {
    const permissions = on ? [...user.permissions, key] : user.permissions.filter((p) => p !== key)
    change(() => updateUser(user.id, { permissions }))
  }

  function resetPassword(user: AdminUser) {
    const password = prompt(`Nytt passord for ${user.username} (minst 8 tegn):`)
    if (password) change(() => updateUser(user.id, { password }))
  }

  function remove(user: AdminUser) {
    if (confirm(`Slette ${user.username}? Funnene deres blir liggende.`)) change(() => deleteUser(user.id))
  }

  async function submitNewUser(e: SubmitEvent) {
    e.preventDefault()
    await change(async () => {
      await addUser({
        username: newUsername.trim(),
        password: newPassword,
        is_admin: newIsAdmin,
        permissions: newPermissions,
      })
      newUsername = newPassword = ''
      newIsAdmin = false
      newPermissions = []
    })
  }
</script>

<Page wide>
  <Card.Root class="w-full gap-4 py-4">
    <Card.Header class="px-4">
      <Card.Title class="text-lg">Brukere</Card.Title>
      <Card.Description>
        Hvem som kan logge inn, og hva de har tilgang til. Nye brukere som registrerer seg selv får
        ingenting ekstra før du gir dem tilgang. «Trening»: funnene deres brukes når modellen trenes,
        og de kan starte trening. Admin har tilgang til alt.
      </Card.Description>
    </Card.Header>

    <Card.Content class="flex flex-col gap-4 px-4">
      {#if error}
        <div class="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">{error}</div>
      {/if}

      {#if data}
        <ul class="flex flex-col gap-3">
          {#each data.users as user (user.id)}
            <li class="flex flex-col gap-2 rounded-lg border p-3">
              <div class="flex items-center justify-between gap-2">
                <span class="flex flex-col">
                  <span class="flex items-center gap-2 font-medium">
                    {user.username}
                    {#if user.id === me.id}<Badge variant="secondary">deg</Badge>{/if}
                  </span>
                  <span class="text-xs text-muted-foreground">Registrert {formatFoundAt(user.created_at)}</span>
                </span>
                <span class="flex gap-1">
                  <Button variant="ghost" size="icon" class="size-8" title="Nytt passord" onclick={() => resetPassword(user)}>
                    <KeyRound />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    class="size-8 text-destructive"
                    title="Slett bruker"
                    disabled={user.id === me.id}
                    onclick={() => remove(user)}
                  >
                    <Trash2 />
                  </Button>
                </span>
              </div>
              <div class="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                <div class="flex items-center justify-between gap-2">
                  <Label for="admin-{user.id}">Admin</Label>
                  <Switch
                    id="admin-{user.id}"
                    checked={user.is_admin}
                    disabled={user.id === me.id}
                    onCheckedChange={(on) => change(() => updateUser(user.id, { is_admin: on }))}
                  />
                </div>
                {#each data.permissions as p (p.key)}
                  <div class="flex items-center justify-between gap-2">
                    <Label for="{p.key}-{user.id}" class={user.is_admin ? 'text-muted-foreground' : ''}>{p.label}</Label>
                    <Switch
                      id="{p.key}-{user.id}"
                      checked={user.is_admin || user.permissions.includes(p.key)}
                      disabled={user.is_admin}
                      onCheckedChange={(on) => togglePermission(user, p.key, on)}
                    />
                  </div>
                {/each}
              </div>
            </li>
          {/each}
        </ul>

        <Separator />

        <form class="flex flex-col gap-3" onsubmit={submitNewUser}>
          <div class="text-sm font-medium">Ny bruker</div>
          <Input placeholder="Brukernavn" autocomplete="off" autocapitalize="none" bind:value={newUsername} required />
          <Input
            type="password"
            placeholder="Passord (minst 8 tegn)"
            autocomplete="new-password"
            minlength={8}
            bind:value={newPassword}
            required
          />
          <div class="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
            <div class="flex items-center justify-between gap-2">
              <Label for="new-admin">Admin</Label>
              <Switch id="new-admin" bind:checked={newIsAdmin} />
            </div>
            {#each data.permissions as p (p.key)}
              <div class="flex items-center justify-between gap-2">
                <Label for="new-{p.key}">{p.label}</Label>
                <Switch
                  id="new-{p.key}"
                  checked={newIsAdmin || newPermissions.includes(p.key)}
                  disabled={newIsAdmin}
                  onCheckedChange={(on) =>
                    (newPermissions = on ? [...newPermissions, p.key] : newPermissions.filter((k) => k !== p.key))}
                />
              </div>
            {/each}
          </div>
          <Button type="submit"><UserPlus /> Legg til bruker</Button>
        </form>
      {:else if !error}
        <p class="text-sm text-muted-foreground">Henter brukere…</p>
      {/if}
    </Card.Content>
  </Card.Root>
</Page>
