// @vitest-environment jsdom
// Bootstrap regression: however the /config branch resolves, exactly one
// FluentProvider must sit above the rendered app, carrying the Fluent theme.
// This drives the production entry point itself, not a test-only provider
// tree. test-support must load before the app graph: openapi-fetch's
// createClient captures the absolutizing Request and the recording fetch at
// client creation (see test-support.ts).
import "./test-support"
import type { PublicClientApplication } from "@azure/msal-browser"
import { webLightTheme } from "@fluentui/react-components"
import { beforeEach, describe, expect, it, vi } from "vitest"
import type { AuthConfig } from "./api/client"
import { clearFetchLog, setFetchImpl } from "./test-support"

// MsalProvider runs real mount effects against the instance, so the module
// mock carries the event-callback and active-account surface it touches.
const msalState = vi.hoisted(() => ({
  accounts: [] as unknown[],
  silentImpl: null as null | (() => Promise<{ accessToken: string }>),
  initCalls: 0,
  silentCalls: 0,
}))

vi.mock("@azure/msal-browser", () => {
  class MockPublicClientApplication {
    async initialize(): Promise<void> {
      msalState.initCalls += 1
    }
    getAllAccounts(): unknown[] {
      return msalState.accounts
    }
    getActiveAccount(): unknown | null {
      return msalState.accounts[0] ?? null
    }
    getLogger(): {
      verbose(): void
      info(): void
      warning(): void
      error(): void
      clone(): unknown
    } {
      const logger = { verbose: () => {}, info: () => {}, warning: () => {}, error: () => {} }
      return { ...logger, clone: () => logger }
    }
    initializeWrapperLibrary(): void {}
    addEventCallback(): string {
      return "event-callback-id"
    }
    removeEventCallback(): void {}
    async acquireTokenSilent(): Promise<{ accessToken: string }> {
      msalState.silentCalls += 1
      if (!msalState.silentImpl) throw new Error("no silent impl configured")
      return msalState.silentImpl()
    }
    async loginRedirect(): Promise<void> {}
    async logoutRedirect(): Promise<void> {}
  }
  return {
    PublicClientApplication:
      MockPublicClientApplication as unknown as typeof PublicClientApplication,
  }
})

const AUTH: AuthConfig = { clientId: "client-id", tenantId: "tenant-id", scope: "api://demo/access" }
const PROFILE = { displayName: "Zelda Quartermain", email: "zelda@contoso.example" }

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  })
}

async function loadMain(): Promise<void> {
  vi.resetModules()
  await import("./main")
}

/** Lets the bootstrap fetches, the React render, and the effects settle. */
async function settle(rounds = 30): Promise<void> {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0))
  }
}

function themedRoot(): HTMLElement {
  const providers = document.querySelectorAll<HTMLElement>(".fui-FluentProvider")
  expect(providers).toHaveLength(1)
  const provider = providers[0]
  if (!provider) throw new Error("no FluentProvider root rendered")
  // jsdom resolves the Fluent theme's custom properties from griffel's
  // injected stylesheet, so every branch must compute real tokens here.
  // The rendered-font proof against a real browser stays with the
  // release gate (scripts/check-theme.mts).
  const providerStyle = getComputedStyle(provider)
  expect(providerStyle.getPropertyValue("--fontFamilyBase").trim()).not.toBe("")
  expect(providerStyle.getPropertyValue("--colorBrandBackground").trim()).toBe(
    webLightTheme.colorBrandBackground,
  )
  const ask = [...provider.querySelectorAll("button")].find(
    (button) => (button.textContent ?? "").trim() === "Ask",
  )
  if (ask) {
    const askStyle = getComputedStyle(ask)
    expect(askStyle.getPropertyValue("--fontFamilyBase").trim()).not.toBe("")
    expect(askStyle.getPropertyValue("--colorBrandBackground").trim()).toBe(
      webLightTheme.colorBrandBackground,
    )
  }
  return provider
}

describe("the bootstrap keeps one themed provider above every branch", () => {
  beforeEach(() => {
    document.body.innerHTML = ""
    const root = document.createElement("div")
    root.id = "root"
    document.body.append(root)
    clearFetchLog()
    msalState.accounts = []
    msalState.silentImpl = null
    msalState.initCalls = 0
    msalState.silentCalls = 0
    setFetchImpl((request) => {
      const path = new URL(request.url).pathname
      if (path === "/api/config") return jsonResponse({ auth: null })
      if (path === "/api/me") return jsonResponse(PROFILE)
      return jsonResponse({}, 404)
    })
  })

  it("renders the default surface inside one themed provider when auth is unconfigured", async () => {
    await loadMain()
    await settle()
    const provider = themedRoot()
    expect(provider.querySelector("textarea")).not.toBeNull()
    expect(provider.textContent).toContain("IT Support Assistant")
  })

  it("renders the guest surface inside the same themed provider when auth is configured", async () => {
    setFetchImpl((request) => {
      const path = new URL(request.url).pathname
      if (path === "/api/config") return jsonResponse({ auth: AUTH })
      if (path === "/api/me") return jsonResponse(PROFILE)
      return jsonResponse({}, 404)
    })
    await loadMain()
    await settle()
    const provider = themedRoot()
    expect(msalState.initCalls).toBeGreaterThan(0)
    expect(msalState.silentCalls).toBe(0)
    expect(provider.querySelector("textarea")).not.toBeNull()
  })

  it("renders the signed-in surface inside the same themed provider when an account exists", async () => {
    msalState.accounts = [{ homeAccountId: "h1", username: "zelda@contoso.example" }]
    msalState.silentImpl = async () => ({ accessToken: "token-123" })
    setFetchImpl((request) => {
      const path = new URL(request.url).pathname
      if (path === "/api/config") return jsonResponse({ auth: AUTH })
      if (path === "/api/me") return jsonResponse(PROFILE)
      return jsonResponse({}, 404)
    })
    await loadMain()
    await settle()
    const provider = themedRoot()
    expect(msalState.silentCalls).toBeGreaterThan(0)
    expect(provider.textContent).toContain(PROFILE.displayName)
    expect(provider.querySelector("textarea")).not.toBeNull()
  })

  it("renders the recovery choice inside the same themed provider when renewal fails", async () => {
    msalState.accounts = [{ homeAccountId: "h1", username: "zelda@contoso.example" }]
    msalState.silentImpl = async () => {
      throw new Error("interaction_required")
    }
    setFetchImpl((request) => {
      const path = new URL(request.url).pathname
      if (path === "/api/config") return jsonResponse({ auth: AUTH })
      return jsonResponse({}, 404)
    })
    await loadMain()
    await settle()
    const provider = themedRoot()
    expect(provider.textContent).not.toContain(PROFILE.displayName)
    expect(provider.textContent.trim().length).toBeGreaterThan(0)
  })
})
