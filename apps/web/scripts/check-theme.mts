// Fails the release unless the staged page, rendered in headless Chrome,
// computes the Fluent theme's custom properties on a real button. Speaks
// DevTools over Node's WebSocket; a missing browser fails loudly, never
// green on absent tooling.
import { execFileSync, spawn } from "node:child_process"
import { mkdtempSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { pathToFileURL } from "node:url"

// google-chrome is preinstalled on the ubuntu-latest runner image; the
// fallbacks cover local runs and derivative images. Absence fails loudly.
const BROWSERS = ["google-chrome", "chromium-browser", "chromium"]

type ProbeState = {
  fontFamily: string
  tokenFontFamilyBase: string
  tokenBrandBackground: string
  providerFontFamilyBase: string
  providerBrandBackground: string
  controlRadius: string
  surfaceRadius: string
  neutralBackground: string
  neutralForeground: string
}

type Evaluated = {
  value?: unknown
  exceptionDetails?: { exception?: { description?: string } }
}

function fail(message: string): never {
  throw new Error(`FAIL: ${message}`)
}

function findBrowser(): string {
  for (const candidate of BROWSERS) {
    try {
      const path = execFileSync("which", [candidate], { encoding: "utf8" }).trim()
      if (path) return path
    } catch {
      // not on PATH; fall through to the next candidate
    }
  }
  fail(`no Chrome-family browser found on PATH (tried ${BROWSERS.join(", ")})`)
}

/** Polls the DevTools HTTP endpoint for the page target that loaded the base URL. */
async function findPageTarget(port: number, base: string): Promise<{ webSocketDebuggerUrl: string }> {
  const deadline = Date.now() + 20_000
  for (;;) {
    try {
      const list = await fetch(`http://127.0.0.1:${port}/json/list`)
      if (list.ok) {
        const targets = (await list.json()) as Array<{
          type: string
          url: string
          webSocketDebuggerUrl: string
        }>
        const page = targets.find((target) => target.type === "page" && target.url.startsWith(base))
        if (page?.webSocketDebuggerUrl) return page
      }
    } catch {
      // the endpoint is not serving yet; keep polling
    }
    if (Date.now() > deadline) fail("the DevTools page target never appeared")
    await new Promise((resolve) => setTimeout(resolve, 200))
  }
}

function connect(url: string): Promise<WebSocket> {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(url)
    socket.addEventListener("open", () => resolve(socket))
    socket.addEventListener("error", () => reject(new Error(`could not connect to ${url}`)))
  })
}

let nextMessageId = 1

/** One CDP round trip, resolved with the raw reply. */
function command(socket: WebSocket, method: string, params: object): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const id = nextMessageId++
    const onMessage = (event: MessageEvent) => {
      const message = JSON.parse(String(event.data)) as {
        id?: number
        error?: { message: string }
        result?: { result?: unknown; exceptionDetails?: Evaluated["exceptionDetails"] }
        exceptionDetails?: Evaluated["exceptionDetails"]
      }
      if (message.id !== id) return
      socket.removeEventListener("message", onMessage)
      if (message.error) reject(new Error(message.error.message))
      else resolve(message.result)
    }
    socket.addEventListener("message", onMessage)
    socket.send(
      JSON.stringify({ id, method, params }),
    )
  })
}

async function evaluate(socket: WebSocket, expression: string): Promise<Evaluated> {
  const reply = await command(socket, "Runtime.evaluate", {
    expression, awaitPromise: true, returnByValue: true,
  }) as { result?: { value?: unknown }; exceptionDetails?: Evaluated["exceptionDetails"] }
  return { value: reply.result?.value, exceptionDetails: reply.exceptionDetails }
}

// Pending is held at the network boundary via CDP request-stage interception;
// the app's captured fetch instance is not replaceable after bootstrap.
const PENDING_PROBE = `(() => new Promise((resolve, reject) => {
  const deadline = Date.now() + 5000
  const tick = () => {
    const root = document.querySelector(".fui-Spinner")
    if (root) {
      setTimeout(() => resolve({
        reduced: matchMedia("(prefers-reduced-motion: reduce)").matches,
        status: document.querySelector('[role="status"]').textContent,
        hidden: root.getAttribute("aria-hidden"),
        slots: [...root.querySelectorAll("span")].flatMap((slot) => [null, "::before", "::after"].map((pseudo) => {
          const style = getComputedStyle(slot, pseudo)
          return { pseudo, name: style.animationName, duration: style.animationDuration, iterations: style.animationIterationCount, transition: style.transitionDuration }
        })),
        animations: root.getAnimations({ subtree: true }).map((animation) => {
          const timing = animation.effect.getTiming()
          return { duration: timing.duration, iterations: String(timing.iterations) }
        }),
      }), 250)
      return
    }
    if (Date.now() >= deadline) return reject(new Error("pending Spinner did not mount"))
    setTimeout(tick, 50)
  }
  tick()
}))()`

type PendingState = {
  reduced: boolean
  status: string
  hidden: string
  slots: Array<{ pseudo: string | null; name: string; duration: string; iterations: string; transition: string }>
  animations: Array<{ duration: number | string; iterations: string }>
}

async function pendingState(socket: WebSocket): Promise<PendingState> {
  const evaluated = await evaluate(socket, PENDING_PROBE)
  if (evaluated.exceptionDetails) throw new Error(evaluated.exceptionDetails.exception?.description)
  const state = evaluated.value as PendingState
  if (state.hidden !== "true" || !["Finding an answer…", "Opening your ticket…"].includes(state.status)) {
    throw new Error(`pending announcement missing: ${JSON.stringify(state)}`)
  }
  return state
}

/** Holds API mutations at the browser boundary; returns paused request IDs. */
async function intercept(socket: WebSocket): Promise<Map<string, string>> {
  const held = new Map<string, string>()
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(String(event.data)) as {
      method?: string
      params?: { requestId?: string; request?: { url?: string } }
    }
    if (message.method !== "Fetch.requestPaused") return
    const url = message.params?.request?.url ?? ""
    const requestId = message.params?.requestId
    if (requestId === undefined || !url.includes("/api/")) return
    held.set(new URL(url).pathname, requestId)
    console.log(`held: ${url}`)
  })
  await command(socket, "Fetch.enable", {
    patterns: [{ urlPattern: "*://*/api/*", requestStage: "Request" }],
  })
  return held
}

/** Reads the computed theme from a rendered button and provider. */
const PROBE = `(() => new Promise((resolve, reject) => {
  const deadline = Date.now() + 45000
  const tick = () => {
    const provider = document.querySelector(".fui-FluentProvider")
    const button = [...document.querySelectorAll("button")].find(
      (b) => (b.textContent || "").trim() === "Ask",
    )
    if (provider && button) {
      const style = window.getComputedStyle(button)
      const providerStyle = window.getComputedStyle(provider)
      resolve({
        fontFamily: style.fontFamily,
        tokenFontFamilyBase: style.getPropertyValue("--fontFamilyBase").trim(),
        tokenBrandBackground: style.getPropertyValue("--colorBrandBackground").trim(),
        providerFontFamilyBase: providerStyle.getPropertyValue("--fontFamilyBase").trim(),
        providerBrandBackground: providerStyle.getPropertyValue("--colorBrandBackground").trim(),
        controlRadius: style.borderTopLeftRadius,
        surfaceRadius: window.getComputedStyle(document.querySelector(".fui-Card")).borderTopLeftRadius,
        neutralBackground: providerStyle.backgroundColor,
        neutralForeground: providerStyle.getPropertyValue("--colorNeutralForeground1").trim(),
      })
      return
    }
    if (Date.now() >= deadline) {
      reject(new Error("the app surface never mounted: no themed provider with an Ask button"))
      return
    }
    setTimeout(tick, 250)
  }
  tick()
}))()`

async function main(): Promise<void> {
  const base = process.argv[2] ?? process.env.SMOKE_BASE_URL
  if (!base) fail(`usage: node ${process.argv[1]} <base-url>`)
  const browser = findBrowser()
  const profile = mkdtempSync(join(tmpdir(), "theme-gate-"))
  const chrome = spawn(
    browser,
    [
      "--headless",
      "--no-sandbox",
      "--disable-gpu",
      "--disable-dev-shm-usage",
      "--remote-debugging-port=0",
      `--user-data-dir=${profile}`,
      base,
    ],
    { stdio: ["ignore", "ignore", "pipe"] },
  )
  try {
    const port = await new Promise<number>((resolve, reject) => {
      let buffered = ""
      const timer = setTimeout(() => {
        reject(new Error("the browser never opened a DevTools endpoint"))
      }, 20_000)
      chrome.stderr?.on("data", (chunk: Buffer) => {
        buffered += chunk.toString()
        const match = /DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)\//.exec(buffered)
        const port = match?.[1]
        if (port !== undefined) {
          clearTimeout(timer)
          resolve(Number(port))
        }
      })
      chrome.on("exit", (code) => {
        clearTimeout(timer)
        reject(new Error(`the browser exited with code ${code} before opening DevTools`))
      })
    })
    const target = await findPageTarget(port, base)
    const socket = await connect(target.webSocketDebuggerUrl)
    // The first navigation tears down the provisional context between the
    // target appearing and the probe attaching; the session itself survives,
    // so re-attach the probe to the committed context and continue.
    let evaluated: Evaluated | undefined
    for (let attempt = 0; evaluated === undefined; attempt += 1) {
      try {
        evaluated = await evaluate(socket, PROBE)
      } catch (cause) {
        if (attempt >= 4 || !/context/i.test(String(cause))) throw cause
        await new Promise((resolve) => setTimeout(resolve, 500))
      }
    }
    if (evaluated.exceptionDetails) {
      fail(evaluated.exceptionDetails.exception?.description ?? "the in-page probe threw")
    }
    const state = evaluated.value as Partial<ProbeState> | undefined
    if (!state?.tokenFontFamilyBase || !state?.tokenBrandBackground) {
      fail(
        `the rendered button computes no theme tokens (fontFamilyBase "${state?.tokenFontFamilyBase}", brandBackground "${state?.tokenBrandBackground}")`,
      )
    }
    if (/times new roman/i.test(state.fontFamily ?? "")) {
      fail(`the rendered button still uses a browser-default font: "${state.fontFamily}"`)
    }
    if (!state?.providerFontFamilyBase || !state?.providerBrandBackground) {
      fail(
        `the provider root itself computes no theme tokens (fontFamilyBase "${state?.providerFontFamilyBase}", brandBackground "${state?.providerBrandBackground}")`,
      )
    }
    const expectedFont = 'system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif'
    if (
      state.tokenFontFamilyBase.replace(/,\s*/g, ",") !== expectedFont ||
      state.providerFontFamilyBase.replace(/,\s*/g, ",") !== expectedFont ||
      !/^system-ui(?:,|$)/.test(state.fontFamily ?? "") ||
      state.tokenBrandBackground !== "#0f6cbd" ||
      state.providerBrandBackground !== "#0f6cbd" ||
      state.controlRadius !== "6px" ||
      state.surfaceRadius !== "12px" ||
      state.neutralBackground !== "rgb(243, 246, 250)" ||
      state.neutralForeground !== "#202c3a"
    ) {
      fail(`the rendered app theme differs from the intended tokens: ${JSON.stringify(state)}`)
    }
    console.log(`ok: themed surface rendered ${JSON.stringify(state)}`)
    await command(socket, "Emulation.setEmulatedMedia", {
      features: [{ name: "prefers-reduced-motion", value: "no-preference" }],
    })
    const held = await intercept(socket)
    await evaluate(socket, `document.querySelector("textarea").focus()`)
    await command(socket, "Input.insertText", { text: "Pending motion check" })
    await new Promise((resolve) => setTimeout(resolve, 100))
    await evaluate(socket, `[...document.querySelectorAll("button")].find(b => b.textContent.trim() === "Ask").click()`)
    const normal = await pendingState(socket)
    if (normal.reduced || !normal.animations.some((animation) => animation.iterations === "Infinity" && Number(animation.duration) > 0)) {
      throw new Error(`normal Spinner motion missing: ${JSON.stringify(normal)}`)
    }
    await command(socket, "Emulation.setEmulatedMedia", {
      features: [{ name: "prefers-reduced-motion", value: "reduce" }],
    })
    const reducedAsk = await pendingState(socket)
    const answerRequest = held.get("/api/answers")
    if (!answerRequest) throw new Error("the answer request was not held")
    await command(socket, "Fetch.fulfillRequest", {
      requestId: answerRequest,
      responseCode: 200,
      responseHeaders: [{ name: "Content-Type", value: "application/json" }],
      body: Buffer.from(JSON.stringify({ text: "Local motion fixture", citations: [], chunks: [] })).toString("base64"),
    })
    const settled = await evaluate(socket, `new Promise((resolve, reject) => {
      const deadline = Date.now() + 5000
      const tick = () => {
        if (!document.querySelector(".fui-Spinner") && document.querySelector('[role="status"]').textContent === "Answer ready.") return resolve(true)
        if (Date.now() >= deadline) return reject(new Error("held answer did not settle"))
        setTimeout(tick, 50)
      }
      tick()
    })`)
    if (settled.exceptionDetails || settled.value !== true) throw new Error("held answer did not settle")
    await evaluate(socket, `[...document.querySelectorAll("button")].find(b => b.textContent.trim() === "Escalate to IT").click()`)
    const reducedTicket = await pendingState(socket)
    if (reducedAsk.status !== "Finding an answer…" || reducedTicket.status !== "Opening your ticket…" || !held.has("/api/tickets")) {
      throw new Error("both pending request branches must be observed separately")
    }
    for (const reduced of [reducedAsk, reducedTicket]) {
      if (!reduced.reduced || reduced.animations.length > 0 || reduced.slots.some((slot) => slot.name !== "none" || slot.duration !== "0s" || slot.transition !== "0s")) {
        throw new Error(`reduced-motion Spinner still animates: ${JSON.stringify(reduced)}`)
      }
    }
    console.log(`ok: pending motion rendered ${JSON.stringify({ normal, reducedAsk, reducedTicket })}`)
    socket.close()
  } finally {
    chrome.kill("SIGKILL")
    rmSync(profile, { recursive: true, force: true })
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  await main()
}
