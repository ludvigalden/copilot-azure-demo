import { FluentProvider, webLightTheme } from "@fluentui/react-components"
import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { App } from "./App"
import { api } from "./api/client"
import { authGate, initializeAuth, needsAuth } from "./auth"

async function bootstrap() {
  const container = document.getElementById("root")
  if (!container) throw new Error("Missing #root element")

  let authed = false
  try {
    const { data } = await api.GET("/config")
    if (data && needsAuth(data)) {
      // MSAL is loaded only when the API reports a configured sign-in.
      const msal = await initializeAuth(data.auth)
      authed = true
      createRoot(container).render(
        <StrictMode>
          {authGate(
            msal,
            <FluentProvider theme={webLightTheme}>
              <App />
            </FluentProvider>,
          )}
        </StrictMode>,
      )
    }
  } catch {
    // /config unreachable: fall through and render without sign-in.
  }
  if (!authed) {
    createRoot(container).render(
      <StrictMode>
        <FluentProvider theme={webLightTheme}>
          <App />
        </FluentProvider>
      </StrictMode>,
    )
  }
}

bootstrap()
