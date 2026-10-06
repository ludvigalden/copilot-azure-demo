import {
  FluentProvider,
  makeStaticStyles,
  makeStyles,
  Spinner,
  Text,
  tokens,
} from "@fluentui/react-components"
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
  loading: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    minHeight: "100dvh",
    gap: tokens.spacingHorizontalS,
  },
  pendingIndicator: {
    "@media (prefers-reduced-motion: reduce)": {
      animationName: "none",
      animationDuration: "0s",
      animationIterationCount: "1",
      "&::before, &::after": { animationName: "none", animationDuration: "0s" },
    },
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

/**
 * The loading phase: quiet, themed, identity-free. It owns the screen
 * until initial auth state — including any login-redirect callback —
 * resolves, so no chrome renders on a guess.
 */
function AppLoading() {
  const styles = useStyles()
  return (
    <div className={styles.loading} role="status" aria-live="polite">
      <Spinner
        size="tiny"
        aria-hidden="true"
        spinner={{ className: styles.pendingIndicator }}
        spinnerTail={{ className: styles.pendingIndicator }}
      />
      <Text>Loading…</Text>
    </div>
  )
}

async function bootstrap() {
  const container = document.getElementById("root")
  if (!container) throw new Error("Missing #root element")
  const root = createRoot(container)
  root.render(
    <StrictMode>
      <AppSurface>
        <AppLoading />
      </AppSurface>
    </StrictMode>,
  )

  // Only the inner subtree varies between the anonymous and authenticated
  // shapes; the FluentProvider must stay mounted above both, or production
  // visitors get an unthemed app.
  let children: ReactNode = <App />
  try {
    const { data } = await api.GET("/config")
    if (needsAuth(data)) {
      // MSAL lives in a separate chunk, fetched only when the API reports a configured sign-in.
      const { authGate, initializeAuth } = await import("./auth")
      const { msal, actions, signedIn, redirectError } = await initializeAuth(data.auth)
      // The callback is awaited inside initializeAuth, so the signed-in
      // state is settled at render time; a failed redirect opens on the
      // explicit recovery choice, never a silent guest.
      children = authGate(
        msal,
        <App
          signedIn={signedIn}
          initialAuthError={redirectError !== null}
          signIn={actions.signIn}
          signOut={actions.signOut}
          continueAsGuest={actions.continueAsGuest}
        />,
      )
    }
  } catch {
    // /config unreachable or sign-in setup failed: render without sign-in.
  }
  root.render(
    <StrictMode>
      <AppSurface>{children}</AppSurface>
    </StrictMode>,
  )
}

bootstrap()
