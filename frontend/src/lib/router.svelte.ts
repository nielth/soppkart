/**
 * A tiny router: the login, registration, account and user pages have their own
 * address (/login, ...) and open over the map, which stays loaded underneath.
 * Caddy (and Vite) serve index.html for every path, so the addresses work directly.
 */
export const router = $state({ path: location.pathname })

export function navigate(path: string) {
  if (path === router.path) return
  history.pushState({}, '', path)
  router.path = path
}

window.addEventListener('popstate', () => (router.path = location.pathname))
