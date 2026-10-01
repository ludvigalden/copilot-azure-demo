import { InteractionType, PublicClientApplication } from "@azure/msal-browser"
import { MsalAuthenticationTemplate, MsalProvider } from "@azure/msal-react"
import { createElement, type ReactNode } from "react"
import { api } from "./api/client"
import type { components } from "./api/schema"

export type AuthConfig = components["schemas"]["AuthConfig"]
export type ClientConfig = components["schemas"]["ClientConfig"]

/** True only when /api/config returned a non-null auth block. */
export function needsAuth(
  config: ClientConfig | null | undefined,
): config is ClientConfig & { auth: AuthConfig } {
  return config?.auth != null
}

/**
 * Creates and initializes MSAL, then attaches the bearer-token middleware to
 * the shared API client. Called only when /api/config returned auth.
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
  return createElement(
    MsalProvider,
    { instance: msal },
    createElement(
      MsalAuthenticationTemplate,
      { interactionType: InteractionType.Redirect },
      children,
    ),
  )
}
