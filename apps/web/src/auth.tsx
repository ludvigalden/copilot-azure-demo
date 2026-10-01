import { InteractionType, PublicClientApplication } from "@azure/msal-browser"
import { MsalAuthenticationTemplate, MsalProvider } from "@azure/msal-react"
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
        const { accessToken } = await msal.acquireTokenSilent({
          scopes: [auth.scope],
          account,
        })
        request.headers.set("Authorization", `Bearer ${accessToken}`)
      }
      return request
    },
  })

  return msal
}

/** Wraps the app in the MSAL provider and forces redirect sign-in. */
export function authGate(msal: PublicClientApplication, children: ReactNode) {
  return (
    <MsalProvider instance={msal}>
      <MsalAuthenticationTemplate interactionType={InteractionType.Redirect}>
        {children}
      </MsalAuthenticationTemplate>
    </MsalProvider>
  )
}
