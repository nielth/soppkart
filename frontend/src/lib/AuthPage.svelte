<script lang="ts">
  import { login, register, type User } from './api'
  import { Button } from '$lib/components/ui/button'
  import * as Card from '$lib/components/ui/card'
  import { Input } from '$lib/components/ui/input'
  import { Label } from '$lib/components/ui/label'
  import Page from './Page.svelte'
  import { navigate } from './router.svelte'

  interface Props {
    mode: 'login' | 'register'
    /** Called with the user once logged in (or registered and logged in). */
    ondone: (user: User) => void
  }

  let { mode, ondone }: Props = $props()
  let username = $state('')
  let password = $state('')
  let repeat = $state('')
  let error = $state<string | null>(null)
  let busy = $state(false)

  async function submit(e: SubmitEvent) {
    e.preventDefault()
    error = null
    if (mode === 'register' && password !== repeat) {
      error = 'Passordene er ulike'
      return
    }
    busy = true
    try {
      const user = mode === 'login' ? await login(username.trim(), password) : await register(username.trim(), password)
      password = repeat = ''
      ondone(user)
    } catch (err) {
      error = err instanceof Error ? err.message : String(err)
    } finally {
      busy = false
    }
  }
</script>

<Page>
  <div class="mb-6 flex flex-col items-center gap-2 text-center">
    <span class="text-4xl">🍄</span>
    <h1 class="text-xl font-semibold">Soppkart</h1>
  </div>
  <Card.Root>
    <Card.Header>
      <Card.Title class="text-lg">{mode === 'login' ? 'Logg inn' : 'Registrer deg'}</Card.Title>
      <Card.Description>
        {#if mode === 'login'}
          Med kontoen din kan du lagre egne funn, og se det admin har gitt deg tilgang til.
        {:else}
          Du kan lagre egne funn med en gang. Flyfoto, Strava og flere arter får du når admin gir deg
          tilgang.
        {/if}
      </Card.Description>
    </Card.Header>
    <Card.Content>
      <form class="flex flex-col gap-4" onsubmit={submit}>
        <div class="flex flex-col gap-2">
          <Label for="auth-username">Brukernavn</Label>
          <Input
            id="auth-username"
            autocomplete="username"
            autocapitalize="none"
            pattern={mode === 'register' ? '[\\w.@\\-]{2,100}' : undefined}
            title={mode === 'register' ? 'Bokstaver, tall og . @ - _ (minst 2 tegn)' : undefined}
            bind:value={username}
            required
          />
        </div>
        <div class="flex flex-col gap-2">
          <Label for="auth-password">Passord</Label>
          <Input
            id="auth-password"
            type="password"
            autocomplete={mode === 'login' ? 'current-password' : 'new-password'}
            minlength={mode === 'register' ? 8 : undefined}
            placeholder={mode === 'register' ? 'Minst 8 tegn' : undefined}
            bind:value={password}
            required
          />
        </div>
        {#if mode === 'register'}
          <div class="flex flex-col gap-2">
            <Label for="auth-repeat">Gjenta passord</Label>
            <Input id="auth-repeat" type="password" autocomplete="new-password" bind:value={repeat} required />
          </div>
        {/if}
        {#if error}<p class="text-sm text-destructive">{error}</p>{/if}
        <Button type="submit" disabled={busy}>{mode === 'login' ? 'Logg inn' : 'Opprett konto'}</Button>
      </form>
    </Card.Content>
  </Card.Root>
  <p class="mt-4 text-center text-sm text-muted-foreground">
    {#if mode === 'login'}
      Har du ikke konto?
      <button class="font-medium text-foreground underline underline-offset-4" onclick={() => navigate('/registrer')}>
        Registrer deg
      </button>
    {:else}
      Har du allerede konto?
      <button class="font-medium text-foreground underline underline-offset-4" onclick={() => navigate('/login')}>
        Logg inn
      </button>
    {/if}
  </p>
</Page>
