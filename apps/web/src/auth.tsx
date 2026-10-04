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
 * Creates and initializes MSAL, attaches the bearer-token middleware to the
 * shared API client, and returns the sign-in actions. Sign-in is optional:
 * the app renders for anonymous callers, who are served as guests by the API.
 */
export async function initializeAuth(
  auth: AuthConfig,
): Promise<{ msal: PublicClientApplication; actions: AuthActions }> {
  const msal = new PublicClientApplication({
    auth: {
      clientId: auth.clientId,
      authority: `https://login.microsoftonline.com/${auth.tenantId}`,
      redirectUri: window.location.origin,
    },
    cache: { cacheLocation: "sessionStorage" },
  })
  await msal.initialize()

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
  return { msal, actions }
}

/**
 * Wraps the app in the MSAL provider.
 */
export function authGate(msal: PublicClientApplication, children: ReactNode) {
  return <MsalProvider instance={msal}>{children}</MsalProvider>
}
