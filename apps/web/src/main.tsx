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
      const { msal, actions } = await initializeAuth(data.auth)
      // Sign-in stays optional: no account renders as a guest with a sign-in
      // action. Redirects navigate the page, so render-time state is accurate;
      // a failed silent renewal blocks the request, offering re-auth, guest
      // or sign-out.
      app = authGate(
        msal,
        <App
          signedIn={msal.getAllAccounts().length > 0}
          signIn={actions.signIn}
          signOut={actions.signOut}
          continueAsGuest={actions.continueAsGuest}
        />,
      )
    }
  } catch {
    // /config unreachable or sign-in setup failed: render without sign-in.
  }
  createRoot(container).render(<StrictMode>{app}</StrictMode>)
}

bootstrap()
