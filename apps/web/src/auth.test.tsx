// @vitest-environment jsdom
import type { PublicClientApplication } from "@azure/msal-browser"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { AuthConfig } from "./api/client"

// MSAL is mocked at its module boundary; every test drives the real
// openapi-fetch client and the real auth wiring on top of it.
const msalState = vi.hoisted(() => ({
  accounts: [] as unknown[],
  silentImpl: null as null | ((args: unknown) => Promise<{ accessToken: string }>),
  silentCalls: [] as unknown[],
  loginCalls: [] as unknown[],
  logoutCalls: [] as unknown[],
  initCalls: 0,
}))

vi.mock("@azure/msal-browser", () => {
  class MockPublicClientApplication {
    getAllAccounts(): unknown[] {
      return msalState.accounts
    }
    async acquireTokenSilent(args: unknown): Promise<{ accessToken: string }> {
      msalState.silentCalls.push(args)
      if (!msalState.silentImpl) throw new Error("no silent impl configured")
      return msalState.silentImpl(args)
    }
    async initialize(): Promise<void> {
      msalState.initCalls += 1
    }
    async loginRedirect(args: unknown): Promise<void> {
      msalState.loginCalls.push(args)
    }
    async logoutRedirect(args: unknown): Promise<void> {
      msalState.logoutCalls.push(args)
    }
  }
  return {
    PublicClientApplication:
      MockPublicClientApplication as unknown as typeof PublicClientApplication,
  }
})

const API_ORIGIN = "https://demo.local"

// openapi-fetch constructs `new Request("/api/...")` from the relative base
// URL, which only parses inside a browser. The test environment absolutizes
// relative URLs against a fake origin instead.
class AbsoluteRequest extends Request {
  constructor(input: RequestInfo | URL, init?: RequestInit) {
    const resolved =
      typeof input === "string" && input.startsWith("/") ? new URL(input, API_ORIGIN).href : input
    super(resolved, init)
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  })
}

const AUTH: AuthConfig = {
  clientId: "client-id",
  tenantId: "tenant-id",
  scope: "api://demo/access",
}

const SIGNED_IN = [{ username: "jane@contoso.com", homeAccountId: "h1" }]

let fetchCalls: { url: string; authorization: string | null; body: unknown }[] = []

/**
 * Re-imports the auth and client modules into a fresh registry so each test
 * gets its own API singleton and its own ReauthRequiredError identity.
 */
async function loadAuth() {
  vi.resetModules()
  const [auth, client] = await Promise.all([import("./auth"), import("./api/client")])
  return { ...auth, ...client }
}

describe("the auth boundary at the real API client", () => {
  beforeEach(() => {
    msalState.accounts = []
    msalState.silentImpl = null
    msalState.silentCalls = []
    msalState.loginCalls = []
    msalState.logoutCalls = []
    msalState.initCalls = 0
    fetchCalls = []
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const request = input as Request
        let body: unknown = null
        const raw = await request.text()
        if (raw) {
          try {
            body = JSON.parse(raw)
          } catch {
            body = raw
          }
        }
        fetchCalls.push({
          url: request.url,
          authorization: request.headers.get("authorization"),
          body,
        })
        return jsonResponse({ displayName: "Guest", email: "guest@example.com" })
      }),
    )
    vi.stubGlobal("Request", AbsoluteRequest)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("travels tokenless when no account exists and never asks MSAL for a token", async () => {
    const { initializeAuth, api } = await loadAuth()
    await initializeAuth(AUTH)
    const { response } = await api.GET("/me")
    expect(response.ok).toBe(true)
    expect(fetchCalls).toHaveLength(1)
    expect(fetchCalls[0].authorization).toBeNull()
    expect(msalState.silentCalls).toHaveLength(0)
    expect(msalState.initCalls).toBe(1)
  })

  it("attaches the bearer token for a signed-in account", async () => {
    msalState.accounts = SIGNED_IN
    msalState.silentImpl = async () => ({ accessToken: "token-123" })
    const { initializeAuth, api } = await loadAuth()
    await initializeAuth(AUTH)
    await api.GET("/me")
    expect(fetchCalls[0].authorization).toBe("Bearer token-123")
    expect(msalState.silentCalls[0]).toMatchObject({ scopes: [AUTH.scope] })
  })

  it("blocks the request and throws ReauthRequiredError when silent renewal fails", async () => {
    msalState.accounts = SIGNED_IN
    msalState.silentImpl = async () => {
      throw new Error("interaction_required")
    }
    const { initializeAuth, api, ReauthRequiredError } = await loadAuth()
    await initializeAuth(AUTH)
    await expect(api.GET("/me")).rejects.toBeInstanceOf(ReauthRequiredError)
    expect(fetchCalls).toHaveLength(0)
  })

  it("sends a deliberately chosen guest session tokenless without token renewal", async () => {
    msalState.accounts = SIGNED_IN
    msalState.silentImpl = async () => ({ accessToken: "token-123" })
    const { initializeAuth, api } = await loadAuth()
    const { actions } = await initializeAuth(AUTH)
    await api.GET("/me")
    expect(fetchCalls[0].authorization).toBe("Bearer token-123")
    actions.continueAsGuest()
    await api.GET("/me")
    expect(fetchCalls[1].authorization).toBeNull()
    expect(msalState.silentCalls).toHaveLength(1)
  })

  it("sign-in and sign-out navigate through MSAL with the configured scope and account", async () => {
    msalState.accounts = SIGNED_IN
    const { initializeAuth } = await loadAuth()
    const { actions } = await initializeAuth(AUTH)
    actions.signIn()
    expect(msalState.loginCalls[0]).toMatchObject({ scopes: [AUTH.scope] })
    actions.signOut()
    expect(msalState.logoutCalls).toHaveLength(1)
    const logout = msalState.logoutCalls[0] as { account?: { username?: string } }
    expect(logout.account?.username).toBe("jane@contoso.com")
  })

  it("keeps the app subtree inside the MsalProvider element the gate returns", async () => {
    const { authGate, initializeAuth } = await loadAuth()
    const { msal } = await initializeAuth(AUTH)
    const gated = authGate(msal, "surface")
    expect(gated.props.children).toBe("surface")
    expect(gated.props.instance).toBe(msal)
  })
})
