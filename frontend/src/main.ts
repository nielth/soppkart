import { mount } from 'svelte'
import './app.css'
import App from './App.svelte'
import { registerServiceWorker } from './lib/offline'

// shadcn-svelte themes dark mode with a .dark class; follow the device setting.
const darkMedia = window.matchMedia('(prefers-color-scheme: dark)')
const applyTheme = () => document.documentElement.classList.toggle('dark', darkMedia.matches)
applyTheme()
darkMedia.addEventListener('change', applyTheme)

registerServiceWorker()

const app = mount(App, {
  target: document.getElementById('app')!,
})

export default app
