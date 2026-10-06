// @vitest-environment jsdom
// test-support must load before the app graph: it installs the absolutizing
// Request and the recording fetch that openapi-fetch's createClient captures
// at client creation. See test-support.ts.
import "./test-support"
import { FluentProvider } from "@fluentui/react-components"
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { App } from "./App"
import { api, ReauthRequiredError } from "./api/client"
import { API_ORIGIN, clearFetchLog, fetchLog, setFetchImpl } from "./test-support"
import { appTheme } from "./theme"

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
      <FluentProvider theme={appTheme}>
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
    status: () => container?.querySelector('[role="status"]')?.textContent ?? null,
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

  it.each([
    ["Ask", "/api/answers", "Finding an answer…", "Answer ready."],
    ["Escalate to IT", "/api/tickets", "Opening your ticket…", `Ticket ${TICKET.number} opened.`],
  ])(
    "keeps %s pending status accessible with reduced-motion slot rules",
    async (label, path, pending, ready) => {
      let release: ((response: Response) => void) | undefined
      setFetchImpl((request) =>
        new URL(request.url).pathname === path
          ? new Promise<Response>((resolve) => {
              release = resolve
            })
          : jsonResponse(PROFILE),
      )
      const s = renderApp()
      await flush()
      const status = container?.querySelector('[role="status"]')
      expect(container?.querySelector(".fui-Spinner")).toBeNull()
      await s.type("Password reset")
      await s.click(label)
      expect(release).toBeTypeOf("function")
      expect(s.status()).toBe(pending)
      expect(status?.getAttribute("aria-live")).toBe("polite")
      expect(container?.querySelector(".fui-Spinner")?.getAttribute("aria-hidden")).toBe("true")
      for (const slot of ["spinner", "spinnerTail"]) {
        const element = container?.querySelector(`.fui-Spinner__${slot}`)
        expect(element).toBeTruthy()
        const rules = [...document.styleSheets]
          .flatMap((sheet) => [...sheet.cssRules])
          .filter(
            (rule): rule is CSSMediaRule =>
              rule instanceof CSSMediaRule &&
              rule.conditionText.includes("prefers-reduced-motion: reduce"),
          )
          .flatMap((rule) => [...rule.cssRules])
          .filter(
            (rule): rule is CSSStyleRule =>
              rule instanceof CSSStyleRule && !!element?.matches(rule.selectorText),
          )
        expect(rules.some((rule) => rule.style.getPropertyValue("animation-name") === "none")).toBe(
          true,
        )
        expect(
          rules.some((rule) => rule.style.getPropertyValue("animation-duration") === "0s"),
        ).toBe(true)
      }
      await act(async () => release?.(jsonResponse(path === "/api/answers" ? ANSWER : TICKET)))
      await flush()
      expect(s.status()).toBe(ready)
      expect(container?.querySelector('[role="status"]')).toBe(status)
      expect(container?.querySelector(".fui-Spinner")).toBeNull()
    },
  )

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

  it("labels the question and keeps answer, sources, then escalation in reading order", async () => {
    const s = renderApp()
    await flush()
    const area = container?.querySelector("textarea")
    expect(area?.labels?.[0]?.textContent).toBe("Your IT question")
    expect(container?.querySelector("h1")?.textContent).toBe("IT Support Assistant")
    await s.type("How do I reset my password?")
    await s.click("Ask")
    const headings = [...(container?.querySelectorAll("h2, h3") ?? [])].map(
      (heading) => heading.textContent,
    )
    expect(headings).toEqual(["Ask a question", "Answer", "Sources", "Need more help?"])
    expect(s.status()).toContain("Answer ready")
    expect(container?.querySelector('a[href="https://kb.example/reset"]')?.textContent).toContain(
      "Password reset",
    )
    await s.click("Escalate to IT")
    expect(s.status()).toContain(TICKET.number)
  })

  it("keeps the FluentProvider root above the app surface", () => {
    useMiddleware(() => {})
    renderApp({ signedIn: false })
    const provider = document.querySelector(".fui-FluentProvider")
    expect(provider).not.toBeNull()
    expect(document.querySelectorAll(".fui-FluentProvider")).toHaveLength(1)
    expect(provider?.querySelector("textarea")).not.toBeNull()
  })

  it("structures the conversation as a labelled feed of labelled roving articles", () => {
    useMiddleware(() => {})
    renderApp({ signedIn: false })
    const workspace = document.querySelector("main")
    expect(workspace).not.toBeNull()
    expect(workspace?.hasAttribute("tabindex")).toBe(false)
    expect(workspace?.getAttribute("aria-label")).toBe("IT support workspace")
    const feed = workspace?.querySelector('[role="feed"]')
    expect(feed?.getAttribute("aria-label")).toBe("IT support conversation")
    const articles = [...(feed?.querySelectorAll("[data-feed-index]") ?? [])]
    expect(articles).toHaveLength(3)
    articles.forEach((article, index) => {
      // The ask and answer articles are Fluent Cards carrying the explicit
      // role; the escalation unit is the native <article> element, whose
      // article role is implicit.
      expect(article.getAttribute("role") === "article" || article.tagName === "ARTICLE").toBe(true)
      expect(article.getAttribute("tabindex")).toBe("-1")
      expect(article.getAttribute("aria-posinset")).toBe(String(index + 1))
      expect(article.getAttribute("aria-setsize")).toBe("3")
      const labelledby = article.getAttribute("aria-labelledby")
      expect(labelledby).toBeTruthy()
      expect(document.getElementById(labelledby ?? "")?.textContent).toBeTruthy()
    })
    expect(document.getElementById("answer-content")).toBeTruthy()
    expect(feed?.querySelector("textarea")).toBeTruthy()
    // An auth alert is a boundary choice, not a conversation turn: it must
    // stay outside the feed.
    expect(workspace?.querySelectorAll('[role="feed"]')).toHaveLength(1)
  })

  it("roves feed focus between articles with ArrowDown, ArrowUp, Home, and End", async () => {
    useMiddleware(() => {})
    renderApp({ signedIn: false })
    await flush()
    const articles = [
      ...(document
        .querySelector('[role="feed"]')
        ?.querySelectorAll<HTMLElement>("[data-feed-index]") ?? []),
    ]
    expect(articles).toHaveLength(3)
    act(() => articles[0]?.focus())
    expect(document.activeElement).toBe(articles[0])
    await act(async () => {
      articles[0]?.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }))
    })
    expect(document.activeElement).toBe(articles[1])
    await act(async () => {
      articles[1]?.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }))
    })
    expect(document.activeElement).toBe(articles[0])
    await act(async () => {
      articles[0]?.dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true }))
    })
    expect(document.activeElement).toBe(articles[2])
    await act(async () => {
      articles[2]?.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true }))
    })
    expect(document.activeElement).toBe(articles[0])
  })

  it("moves feed focus a page at a time with PageDown and PageUp", async () => {
    useMiddleware(() => {})
    renderApp({ signedIn: false })
    await flush()
    const articles = [
      ...(document
        .querySelector('[role="feed"]')
        ?.querySelectorAll<HTMLElement>("[data-feed-index]") ?? []),
    ]
    act(() => articles[0]?.focus())
    await act(async () => {
      articles[0]?.dispatchEvent(new KeyboardEvent("keydown", { key: "PageDown", bubbles: true }))
    })
    expect(document.activeElement).toBe(articles[1])
    await act(async () => {
      articles[1]?.dispatchEvent(new KeyboardEvent("keydown", { key: "PageUp", bubbles: true }))
    })
    expect(document.activeElement).toBe(articles[0])
  })

  it("roves from a control inside an article and clamps at the feed edges", async () => {
    useMiddleware(() => {})
    renderApp({ signedIn: false })
    await flush()
    const feed = document.querySelector('[role="feed"]')
    const articles = [...(feed?.querySelectorAll<HTMLElement>("[data-feed-index]") ?? [])]
    const askButton = [...(articles[0]?.querySelectorAll("button") ?? [])][0]
    expect(askButton?.textContent).toBe("Ask")
    await act(async () => {
      askButton?.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }))
    })
    expect(document.activeElement).toBe(articles[1])
    await act(async () => {
      articles[0]?.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }))
    })
    expect(document.activeElement).toBe(articles[0])
    await act(async () => {
      articles[2]?.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }))
    })
    expect(document.activeElement).toBe(articles[2])
  })

  it("leaves caret keys inside the question textarea to the editor", async () => {
    useMiddleware(() => {})
    renderApp({ signedIn: false })
    await flush()
    const area = container?.querySelector("textarea") as HTMLTextAreaElement
    act(() => area.focus())
    await act(async () => {
      area.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }))
    })
    expect(document.activeElement).toBe(area)
    await act(async () => {
      area.dispatchEvent(new KeyboardEvent("keydown", { key: "PageDown", bubbles: true }))
    })
    expect(document.activeElement).toBe(area)
  })

  it("Control+Home exits the feed to the focusable element before it", async () => {
    useMiddleware(() => {})
    const s = renderApp({ signedIn: false })
    await flush()
    const articles = [
      ...(document
        .querySelector('[role="feed"]')
        ?.querySelectorAll<HTMLElement>("[data-feed-index]") ?? []),
    ]
    act(() => articles[2]?.focus())
    await act(async () => {
      articles[2]?.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Home", ctrlKey: true, bubbles: true }),
      )
    })
    expect(document.activeElement).toBe(s.button("Sign in"))
  })

  it("Control+End holds focus when nothing focusable follows the feed", async () => {
    useMiddleware(() => {})
    renderApp({ signedIn: false })
    await flush()
    const articles = [
      ...(document
        .querySelector('[role="feed"]')
        ?.querySelectorAll<HTMLElement>("[data-feed-index]") ?? []),
    ]
    act(() => articles[2]?.focus())
    await act(async () => {
      articles[2]?.dispatchEvent(
        new KeyboardEvent("keydown", { key: "End", ctrlKey: true, bubbles: true }),
      )
    })
    expect(document.activeElement).toBe(articles[2])
  })

  it("moves focus to the answer article when an ask completes", async () => {
    useMiddleware(() => {})
    const s = renderApp({ signedIn: false })
    await flush()
    const articles = [
      ...(document
        .querySelector('[role="feed"]')
        ?.querySelectorAll<HTMLElement>("[data-feed-index]") ?? []),
    ]
    await s.type("How do I reset my password?")
    await s.click("Ask")
    expect(document.activeElement).toBe(articles[1])
    expect(s.status()).toContain("Answer ready")
  })

  it("gives feed articles a visible keyboard focus indication", () => {
    useMiddleware(() => {})
    renderApp({ signedIn: false })
    const article = document.querySelector('[role="article"]')
    expect(article).toBeTruthy()
    const focusRules = [...document.styleSheets]
      .flatMap((sheet) => [...sheet.cssRules])
      .filter(
        (rule): rule is CSSStyleRule =>
          rule instanceof CSSStyleRule && rule.selectorText.includes(":focus-visible"),
      )
    // jsdom cannot evaluate :focus-visible at matches() time, so assert
    // the linkage structurally: a :focus-visible rule must target a class
    // the articles actually carry, and that rule must paint a solid
    // outline.
    const articleRules = focusRules.filter((rule) =>
      [...(article?.classList ?? [])].some((cls) =>
        rule.selectorText.startsWith(`.${cls}:focus-visible`),
      ),
    )
    expect(articleRules.length).toBeGreaterThan(0)
    // Griffel's atomic CSS gives each declaration its own class, so collect
    // the declared value per outline property across the article's rules.
    const valueFor = (prop: string) =>
      articleRules
        .find((rule) => rule.style.getPropertyValue(prop) !== "")
        ?.style.getPropertyValue(prop)
    expect(valueFor("outline-width")).toBe("2px")
    expect(valueFor("outline-style")).toBe("solid")
    expect(valueFor("outline-color")).toContain("colorStrokeFocus2")
    expect(valueFor("outline-offset")).toBe("2px")
  })

  it("dedupes identical citations, keeping the first occurrence and its order", async () => {
    useMiddleware(() => {})
    setFetchImpl((request) => {
      if (new URL(request.url).pathname === "/api/answers") {
        return jsonResponse({
          text: "Sources may repeat.",
          citations: [
            { title: "Password reset", url: "https://kb.example/reset" },
            { title: "Account lockout", url: "https://kb.example/lockout" },
            { title: "Password reset", url: "https://kb.example/reset" },
            { title: "MFA setup", url: "https://kb.example/mfa" },
          ],
          chunks: [],
        })
      }
      return jsonResponse(PROFILE)
    })
    const s = renderApp()
    await flush()
    await s.type("How do I reset my password?")
    await s.click("Ask")
    // The knowledge base echoed "Password reset" twice; one row survives,
    // and the survivors keep the order the answer listed them in.
    const rows = [...(container?.querySelectorAll("ul > li") ?? [])]
    expect(
      rows.map((row) => row.querySelector("a")?.getAttribute("href") ?? row.textContent),
    ).toEqual(["https://kb.example/reset", "https://kb.example/lockout", "https://kb.example/mfa"])
  })

  it("keeps same-title citations with different URLs and renders url-less ones as plain text", async () => {
    useMiddleware(() => {})
    setFetchImpl((request) => {
      if (new URL(request.url).pathname === "/api/answers") {
        return jsonResponse({
          text: "Two guides share a title.",
          citations: [
            { title: "VPN guide", url: "https://kb.example/vpn-mac" },
            { title: "VPN guide", url: "https://kb.example/vpn-windows" },
            { title: "Printer policy", url: "" },
          ],
          chunks: [],
        })
      }
      return jsonResponse(PROFILE)
    })
    const s = renderApp()
    await flush()
    await s.type("Why is the VPN slow?")
    await s.click("Ask")
    const rows = [...(container?.querySelectorAll("ul > li") ?? [])]
    expect(rows).toHaveLength(3)
    expect(rows[0]?.querySelector("a")?.getAttribute("href")).toBe("https://kb.example/vpn-mac")
    expect(rows[1]?.querySelector("a")?.getAttribute("href")).toBe("https://kb.example/vpn-windows")
    expect(rows[0]?.textContent).toBe("VPN guide")
    expect(rows[1]?.textContent).toBe("VPN guide")
    expect(rows[2]?.querySelector("a")).toBeNull()
    expect(rows[2]?.textContent).toBe("Printer policy")
  })
})
