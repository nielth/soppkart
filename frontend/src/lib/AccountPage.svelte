<script lang="ts">
  import { changePassword, type User } from './api'
  import { Button } from '$lib/components/ui/button'
  import * as Card from '$lib/components/ui/card'
  import { Input } from '$lib/components/ui/input'
  import { Label } from '$lib/components/ui/label'
  import Page from './Page.svelte'

  interface Props {
    user: User
    /** A new password logs the user out everywhere, including here. */
    onloggedout: () => void
  }

  let { user, onloggedout }: Props = $props()
  let currentPassword = $state('')
  let newPassword = $state('')
  let repeat = $state('')
  let error = $state<string | null>(null)

  async function submit(e: SubmitEvent) {
    e.preventDefault()
    error = null
    if (newPassword !== repeat) {
      error = 'Passordene er ulike'
      return
    }
    try {
      await changePassword(currentPassword, newPassword)
      onloggedout()
    } catch (err) {
      error = err instanceof Error ? err.message : String(err)
    }
  }
</script>

<Page>
  <Card.Root>
    <Card.Header>
      <Card.Title class="text-lg">Bytt passord</Card.Title>
      <Card.Description>For {user.username}. Du logges ut overalt og må logge inn på nytt.</Card.Description>
    </Card.Header>
    <Card.Content>
      <form class="flex flex-col gap-4" onsubmit={submit}>
        <div class="flex flex-col gap-2">
          <Label for="current-password">Nåværende passord</Label>
          <Input id="current-password" type="password" autocomplete="current-password" bind:value={currentPassword} required />
        </div>
        <div class="flex flex-col gap-2">
          <Label for="new-password">Nytt passord</Label>
          <Input
            id="new-password"
            type="password"
            autocomplete="new-password"
            minlength={8}
            placeholder="Minst 8 tegn"
            bind:value={newPassword}
            required
          />
        </div>
        <div class="flex flex-col gap-2">
          <Label for="repeat-password">Gjenta nytt passord</Label>
          <Input id="repeat-password" type="password" autocomplete="new-password" bind:value={repeat} required />
        </div>
        {#if error}<p class="text-sm text-destructive">{error}</p>{/if}
        <Button type="submit">Lagre nytt passord</Button>
      </form>
    </Card.Content>
  </Card.Root>
</Page>
