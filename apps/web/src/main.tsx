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
      app = authGate(await initializeAuth(data.auth), app)
    }
  } catch {
    // /config unreachable or sign-in setup failed: render without sign-in.
  }
  createRoot(container).render(<StrictMode>{app}</StrictMode>)
}

bootstrap()
