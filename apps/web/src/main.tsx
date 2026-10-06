import { FluentProvider, makeStaticStyles, makeStyles, tokens } from "@fluentui/react-components"
import { type ReactNode, StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { App } from "./App"
import { api, needsAuth } from "./api/client"
import { appTheme } from "./theme"

const useGlobalStyles = makeStaticStyles({
  body: { margin: 0 },
  "*, *::before, *::after": { boxSizing: "border-box" },
})

const useStyles = makeStyles({
  provider: {
    minHeight: "100dvh",
    backgroundColor: tokens.colorNeutralBackground2,
  },
})

function AppSurface({ children }: { children: ReactNode }) {
  useGlobalStyles()
  const styles = useStyles()
  return (
    <FluentProvider theme={appTheme} className={styles.provider}>
      {children}
    </FluentProvider>
  )
}

async function bootstrap() {
  const container = document.getElementById("root")
  if (!container) throw new Error("Missing #root element")

  // Only the inner subtree varies between the anonymous and authenticated
  // shapes; the FluentProvider must stay mounted above both, or production
  // visitors get an unthemed app.
  let children: ReactNode = <App />
  try {
    const { data } = await api.GET("/config")
    if (needsAuth(data)) {
      // MSAL lives in a separate chunk, fetched only when the API reports a configured sign-in.
      const { authGate, initializeAuth } = await import("./auth")
      const { msal, actions } = await initializeAuth(data.auth)
      // Sign-in is optional; no account renders as a guest.
      // Redirects navigate the page. Failed renewal offers an explicit
      // re-auth, guest, or sign-out choice before another request.
      children = authGate(
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
  createRoot(container).render(
    <StrictMode>
      <AppSurface>{children}</AppSurface>
    </StrictMode>,
  )
}

bootstrap()
