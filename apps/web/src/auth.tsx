import { PublicClientApplication } from "@azure/msal-browser"
import { MsalProvider } from "@azure/msal-react"
import type { ReactNode } from "react"
import { type AuthConfig, api } from "./api/client"

/**
 * Creates and initializes MSAL, then attaches the bearer-token middleware to
 * the shared API client.
 */
export async function initializeAuth(auth: AuthConfig) {
  const msal = new PublicClientApplication({
    auth: {
      clientId: auth.clientId,
      authority: `https://login.microsoftonline.com/${auth.tenantId}`,
      redirectUri: window.location.origin,
    },
    cache: { cacheLocation: "sessionStorage" },
  })
  await msal.initialize()

  api.use({
    async onRequest({ request }) {
      const account = msal.getAllAccounts()[0]
      if (account) {
        try {
          const { accessToken } = await msal.acquireTokenSilent({
            scopes: [auth.scope],
            account,
          })
          request.headers.set("Authorization", `Bearer ${accessToken}`)
        } catch {
          // Token acquisition failed (e.g. expired session): send the request
          // without a token rather than breaking the guest-open endpoints.
        }
      }
      return request
    },
  })

  return msal
}

/**
 * Wraps the app in the MSAL provider. Sign-in is optional: the app renders
 * for anonymous callers, who are served as guests by the API.
 */
export function authGate(msal: PublicClientApplication, children: ReactNode) {
  return <MsalProvider instance={msal}>{children}</MsalProvider>
}
