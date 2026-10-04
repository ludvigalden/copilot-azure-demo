import { FluentProvider, webLightTheme } from "@fluentui/react-components"
import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { App } from "./App"
import { api, needsAuth } from "./api/client"

async function bootstrap() {
  const container = document.getElementById("root")
  if (!container) throw new Error("Missing #root element")

  let app = (
    <FluentProvider theme={webLightTheme}>
      <App />
    </FluentProvider>
  )
  try {
    const { data } = await api.GET("/config")
    if (needsAuth(data)) {
      // MSAL lives in a separate chunk, fetched only when the API reports a configured sign-in.
      const { authGate, initializeAuth } = await import("./auth")
      const msal = await initializeAuth(data.auth)
      // Sign-in stays optional: a signed-out session renders as a guest and
      // carries a sign-in action. loginRedirect navigates the whole page, so
      // the signed-in state at render time is accurate without reactive hooks.
      const signIn =
        msal.getAllAccounts().length > 0
          ? undefined
          : () => msal.loginRedirect({ scopes: [data.auth.scope] })
      app = authGate(msal, <App signIn={signIn} />)
    }
  } catch {
    // /config unreachable or sign-in setup failed: render without sign-in.
  }
  createRoot(container).render(<StrictMode>{app}</StrictMode>)
}

bootstrap()
