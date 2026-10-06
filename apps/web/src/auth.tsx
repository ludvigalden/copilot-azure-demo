import { PublicClientApplication } from "@azure/msal-browser"
import { MsalProvider } from "@azure/msal-react"
import type { Middleware } from "openapi-fetch"
import type { ReactNode } from "react"
import { type AuthConfig, api, ReauthRequiredError } from "./api/client"

/**
 * The sign-in actions the app surface can take. Sign-in and sign-out
 * navigate the whole page; the continue-as-guest choice takes effect in
 * place, so subsequent requests travel deliberately without credentials.
 */
export type AuthActions = {
  signIn: () => void
  signOut: () => void
  continueAsGuest: () => void
}

/**
 * The auth boundary: a caller with no account, and one who chose guest
 * mode, travels without credentials; a signed-in caller whose silent
 * renewal fails blocks the request, so nothing travels tokenless by accident.
 */
function createAuthMiddleware(
  msal: Pick<PublicClientApplication, "getAllAccounts" | "acquireTokenSilent">,
  scope: string,
  isGuestMode: () => boolean,
): Middleware {
  return {
    async onRequest({ request }) {
      if (isGuestMode()) return request
      const account = msal.getAllAccounts()[0]
      if (!account) return request
      try {
        const { accessToken } = await msal.acquireTokenSilent({
          scopes: [scope],
          account,
        })
        request.headers.set("Authorization", `Bearer ${accessToken}`)
        return request
      } catch (error) {
        throw new ReauthRequiredError("The signed-in session needs a renewed token", {
          cause: error,
        })
      }
    },
  }
}

/**
 * Initializes MSAL, resolves any pending login-redirect callback, and
 * attaches the bearer middleware. Returns the settled auth state so the
 * first render paints the caller as they are, not as a guess.
 */
export async function initializeAuth(auth: AuthConfig): Promise<{
  msal: PublicClientApplication
  actions: AuthActions
  signedIn: boolean
  redirectError: unknown
}> {
  const msal = new PublicClientApplication({
    auth: {
      clientId: auth.clientId,
      authority: `https://login.microsoftonline.com/${auth.tenantId}`,
      redirectUri: window.location.origin,
    },
    cache: { cacheLocation: "sessionStorage" },
  })
  await msal.initialize()

  // The redirect back from sign-in carries its redemption in the URL:
  // consume it before anyone reads account state, or the caller renders
  // as a guest until a manual refresh. MsalProvider's own call receives
  // the same cached promise.
  let redirectError: unknown = null
  try {
    await msal.handleRedirectPromise()
  } catch (error) {
    // A failed redirect is an auth failure to surface with explicit
    // recovery choices, never a silent fallback to guest.
    redirectError = error
  }

  let guestMode = false
  api.use(createAuthMiddleware(msal, auth.scope, () => guestMode))
  const actions: AuthActions = {
    // loginRedirect and logoutRedirect navigate the whole page, so the
    // signed-in state a render sees is accurate without reactive hooks.
    signIn: () => msal.loginRedirect({ scopes: [auth.scope] }),
    signOut: () => msal.logoutRedirect({ account: msal.getAllAccounts()[0] }),
    continueAsGuest: () => {
      guestMode = true
    },
  }
  return {
    msal,
    actions,
    signedIn: msal.getAllAccounts().length > 0,
    redirectError,
  }
}

/**
 * Wraps the app in the MSAL provider.
 */
export function authGate(msal: PublicClientApplication, children: ReactNode) {
  return <MsalProvider instance={msal}>{children}</MsalProvider>
}
