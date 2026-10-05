// @vitest-environment jsdom
// test-support must load before the app graph: it installs the absolutizing
// Request and the recording fetch that openapi-fetch's createClient captures
// at client creation. See test-support.ts.
import "./test-support"
import { FluentProvider, webLightTheme } from "@fluentui/react-components"
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { App } from "./App"
import { api, ReauthRequiredError } from "./api/client"
import { API_ORIGIN, clearFetchLog, fetchLog, setFetchImpl } from "./test-support"

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  })
}

const PROFILE = { displayName: "Jane Doe", email: "jane@contoso.com" }
const ANSWER = {
  text: "Reset it in Settings.",
  citations: [{ title: "Password reset", url: "https://kb.example/reset" }],
  chunks: [],
}
const TICKET = {
  number: "IT-20261004-ABCD",
  shortDescription: "Support request",
  caller: PROFILE,
  createdAt: "2026-10-04T12:00:00Z",
}

const signIn = vi.fn()
const signOut = vi.fn()
const continueAsGuest = vi.fn()

let container: HTMLDivElement | null = null
let root: Root | null = null
let wired: Parameters<typeof api.use>[0] | null = null

function useMiddleware(onRequest: (ctx: { request: Request }) => unknown): void {
  wired = { onRequest } as Parameters<typeof api.use>[0]
  api.use(wired)
}

/** A signed-in middleware that renews successfully, like the real one. */
function useSignedInMiddleware(token = "token-123"): void {
  useMiddleware(({ request }) => {
    request.headers.set("Authorization", `Bearer ${token}`)
  })
}

function renderApp(props: Partial<Parameters<typeof App>[0]> = {}): ReturnType<typeof surface> {
  container = document.createElement("div")
  document.body.append(container)
  root = createRoot(container)
  act(() => {
    root?.render(
      <FluentProvider theme={webLightTheme}>
        <App signIn={signIn} signOut={signOut} continueAsGuest={continueAsGuest} {...props} />
      </FluentProvider>,
    )
  })
  return surface()
}

function surface() {
  return {
    text: () => container?.textContent ?? "",
    has: (label: string) => (container?.textContent ?? "").includes(label),
    button(label: string): HTMLButtonElement {
      const found = [...(container?.querySelectorAll("button") ?? [])].find(
        (b) => b.textContent?.trim() === label,
      )
      if (!found) {
        throw new Error(`no button "${label}" on the surface: ${container?.textContent}`)
      }
      return found
    },
    click: async (label: string) => {
      await act(async () => {
        surface()
          .button(label)
          .dispatchEvent(new MouseEvent("click", { bubbles: true }))
      })
      await flush()
    },
    type: async (value: string) => {
      const area = container?.querySelector("textarea")
      if (!area) throw new Error("no textarea on the surface")
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set
      await act(async () => {
        setter?.call(area, value)
        area.dispatchEvent(new Event("input", { bubbles: true }))
      })
    },
    alert: () => container?.querySelector('[role="alert"]')?.textContent ?? null,
  }
}

async function flush(rounds = 8): Promise<void> {
  for (let i = 0; i < rounds; i += 1) {
    await act(async () => {})
  }
}

describe("App at the component and auth boundary", () => {
  beforeEach(() => {
    ;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
    clearFetchLog()
    setFetchImpl((request) => {
      const url = new URL(request.url).pathname
      if (url === "/api/me") return jsonResponse(PROFILE)
      if (url === "/api/answers") return jsonResponse(ANSWER)
      if (url === "/api/tickets") return jsonResponse(TICKET, 201)
      return jsonResponse({}, 404)
    })
    signIn.mockClear()
    signOut.mockClear()
    continueAsGuest.mockClear()
  })

  afterEach(() => {
    if (root) {
      act(() => root?.unmount())
      root = null
    }
    if (wired) {
      api.eject(wired)
      wired = null
    }
    document.body.innerHTML = ""
  })

  it("renders an anonymous caller as a guest with a sign-in affordance, fetched tokenless", async () => {
    useMiddleware(() => {}) // passthrough: no bootstrap account, so no renewal
    const s = renderApp({ signedIn: false })
    await flush()
    expect(s.has("Guest")).toBe(true)
    expect(s.button("Sign in")).toBeTruthy()
    expect(s.has("Sign in again")).toBe(false)
    expect(fetchLog()[0]?.url).toBe(`${API_ORIGIN}/api/me`)
    expect(fetchLog()[0]?.authorization).toBeNull()
  })

  it("renders a signed-in identity with a sign-out affordance", async () => {
    useSignedInMiddleware()
    const s = renderApp({ signedIn: true })
    await flush()
    expect(s.has("Jane Doe")).toBe(true)
    expect(s.button("Sign out")).toBeTruthy()
    expect(s.has("Sign in")).toBe(false)
    expect(fetchLog()[0]?.authorization).toBe("Bearer token-123")
  })

  it("surfaces an explicit choice and blocks the request when renewal fails", async () => {
    useMiddleware(() => {
      throw new ReauthRequiredError("blocked")
    })
    const s = renderApp({ signedIn: true })
    await flush()
    expect(s.button("Sign in again")).toBeTruthy()
    expect(s.button("Continue as guest")).toBeTruthy()
    expect(s.button("Sign out")).toBeTruthy()
    expect(s.has("Could not load your profile.")).toBe(false)
    expect(fetchLog()).toHaveLength(0)
  })

  it("continue as guest clears the choice, and later requests travel tokenless by choice", async () => {
    let guest = false
    continueAsGuest.mockImplementation(() => {
      guest = true
    })
    useMiddleware(() => {
      if (guest) return
      throw new ReauthRequiredError("blocked")
    })
    const s = renderApp({ signedIn: true })
    await flush()
    await s.click("Continue as guest")
    expect(continueAsGuest).toHaveBeenCalledTimes(1)
    expect(s.has("Sign in again")).toBe(false)
    expect(s.has("Guest")).toBe(true)
    expect(s.button("Sign in")).toBeTruthy()
    await s.type("How do I reset my password?")
    await s.click("Ask")
    // The profile fetch was blocked and never left the app; the guest ask does.
    expect(fetchLog()).toHaveLength(1)
    expect(fetchLog()[0]?.url).toBe(`${API_ORIGIN}/api/answers`)
    expect(fetchLog()[0]?.authorization).toBeNull()
    expect(s.has("Reset it in Settings.")).toBe(true)
    expect(s.has("Password reset")).toBe(true)
  })

  it("sign in again re-runs the interactive sign-in", async () => {
    useMiddleware(() => {
      throw new ReauthRequiredError("blocked")
    })
    const s = renderApp({ signedIn: true })
    await flush()
    await s.click("Sign in again")
    expect(signIn).toHaveBeenCalledTimes(1)
  })

  it("sign out from the choice is a deliberate navigation", async () => {
    useMiddleware(() => {
      throw new ReauthRequiredError("blocked")
    })
    const s = renderApp({ signedIn: true })
    await flush()
    await s.click("Sign out")
    expect(signOut).toHaveBeenCalledTimes(1)
  })

  it("header sign out clears the identity at once and fetches nothing more", async () => {
    useSignedInMiddleware()
    const s = renderApp({ signedIn: true })
    await flush()
    expect(s.has("Jane Doe")).toBe(true)
    await s.click("Sign out")
    expect(signOut).toHaveBeenCalledTimes(1)
    expect(s.has("Jane Doe")).toBe(false)
    expect(s.has("Sign out")).toBe(false)
    expect(fetchLog()).toHaveLength(1)
  })

  it("a late profile result does not repaint a deliberate guest choice", async () => {
    let guest = false
    continueAsGuest.mockImplementation(() => {
      guest = true
    })
    const pendingMe: { resolve: ((response: Response) => void) | null } = {
      resolve: null,
    }
    useMiddleware(({ request }) => {
      if (new URL(request.url).pathname === "/api/me") {
        return new Promise<Response>((resolve) => {
          pendingMe.resolve = resolve
        })
      }
      if (guest) return
      throw new ReauthRequiredError("blocked")
    })
    const s = renderApp({ signedIn: true })
    await flush()
    await s.type("How do I reset my password?")
    await s.click("Ask") // fails: the choice card appears
    await s.click("Continue as guest")
    expect(s.has("Guest")).toBe(true)
    pendingMe.resolve?.(jsonResponse(PROFILE)) // the pre-choice fetch lands late
    await flush()
    expect(s.has("Jane Doe")).toBe(false)
    expect(s.has("Sign out")).toBe(false)
    expect(s.has("Guest")).toBe(true)
  })

  it("a late profile result does not repaint after a sign-out choice", async () => {
    const pendingMe: { resolve: ((response: Response) => void) | null } = {
      resolve: null,
    }
    useMiddleware(({ request }) => {
      if (new URL(request.url).pathname === "/api/me") {
        return new Promise<Response>((resolve) => {
          pendingMe.resolve = resolve
        })
      }
      throw new ReauthRequiredError("blocked")
    })
    const s = renderApp({ signedIn: true })
    await flush() // the profile fetch is still pending
    await s.type("How do I reset my password?")
    await s.click("Ask") // fails: the choice card appears
    await s.click("Sign out")
    expect(signOut).toHaveBeenCalledTimes(1)
    pendingMe.resolve?.(jsonResponse(PROFILE)) // the pre-choice fetch lands late
    await flush()
    expect(s.has("Jane Doe")).toBe(false)
  })

  it("shows a concise answer error on an API failure", async () => {
    useMiddleware(() => {})
    setFetchImpl((request) =>
      new URL(request.url).pathname === "/api/answers"
        ? jsonResponse({}, 500)
        : jsonResponse(PROFILE),
    )
    const s = renderApp({ signedIn: true })
    await flush()
    await s.type("How do I reset my password?")
    await s.click("Ask")
    expect(s.alert()).toContain("Could not get an answer. Try again.")
    expect(s.has("Reset it in Settings.")).toBe(false)
  })

  it("shows the same answer error on a network failure", async () => {
    useMiddleware(() => {})
    setFetchImpl((request) => {
      if (new URL(request.url).pathname === "/api/answers") {
        return Promise.reject(new TypeError("fetch failed"))
      }
      return jsonResponse(PROFILE)
    })
    const s = renderApp({ signedIn: true })
    await flush()
    await s.type("How do I reset my password?")
    await s.click("Ask")
    expect(s.alert()).toContain("Could not get an answer. Try again.")
  })

  it("an ask blocked at the auth boundary shows the choice, not a generic error", async () => {
    useMiddleware(({ request }) => {
      if (new URL(request.url).pathname === "/api/me") return
      throw new ReauthRequiredError("blocked")
    })
    const s = renderApp({ signedIn: true })
    await flush()
    await s.type("How do I reset my password?")
    await s.click("Ask")
    expect(s.has("Sign in again")).toBe(true)
    expect(s.has("Could not get an answer.")).toBe(false)
    expect(fetchLog()).toHaveLength(1) // only the profile fetch left the app
  })

  it("shows a concise ticket error on an API failure and the number on success", async () => {
    useMiddleware(() => {})
    const s = renderApp({ signedIn: true })
    await flush()
    await s.type("My laptop will not start.")
    await s.click("Escalate to IT")
    expect(s.has("IT-20261004-ABCD")).toBe(true)

    setFetchImpl((request) =>
      new URL(request.url).pathname === "/api/tickets"
        ? jsonResponse({}, 500)
        : jsonResponse(PROFILE),
    )
    await s.click("Escalate to IT")
    expect(s.alert()).toContain("Could not open a ticket. Try again.")
  })

  it("shows a concise profile error when the profile fetch fails", async () => {
    useMiddleware(() => {})
    setFetchImpl(() => jsonResponse({}, 500))
    const s = renderApp({ signedIn: true })
    await flush()
    expect(s.alert()).toContain("Could not load your profile.")
    expect(s.has("Jane Doe")).toBe(false)
  })

  it("keeps the FluentProvider root above the app surface", () => {
    useMiddleware(() => {})
    renderApp({ signedIn: false })
    const provider = document.querySelector(".fui-FluentProvider")
    expect(provider).not.toBeNull()
    expect(document.querySelectorAll(".fui-FluentProvider")).toHaveLength(1)
    expect(provider?.querySelector("textarea")).not.toBeNull()
  })
})
