// The real-browser theme gate: loads the staged page in headless Chrome and
// fails the release unless the rendered surface computes the Fluent theme's
// custom properties on a real button, with no browser-default font left
// behind. It speaks the DevTools protocol over Node's built-in WebSocket, so
// CI needs no extra dependency to run a red gate, and a missing browser
// fails loudly: the gate never reports green on absent tooling.
// Run: node apps/web/scripts/check-theme.mts <base-url>
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
}

type Evaluated = {
  value?: unknown
  exceptionDetails?: { exception?: { description?: string } }
}

function fail(message: string): never {
  console.error(`FAIL: ${message}`)
  process.exit(1)
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

/** One Runtime.evaluate round trip, resolved with the raw CDP reply. */
function evaluate(socket: WebSocket, expression: string): Promise<Evaluated> {
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
      // awaitPromise+returnByValue lands the settled value at
      // result.result.value (RemoteObject.value); unwrap both levels so
      // callers read .value as the probe object itself. A rejected probe
      // promise reports through result.exceptionDetails (the top-level
      // exceptionDetails stays unset), with value serialized as an empty
      // object - so the rejection must be read before the value is used.
      const remote = message.result?.result as { value?: unknown } | undefined
      if (message.error) reject(new Error(message.error.message))
      else
        resolve({
          value: remote?.value,
          exceptionDetails: message.result?.exceptionDetails ?? message.exceptionDetails,
        })
    }
    socket.addEventListener("message", onMessage)
    socket.send(
      JSON.stringify({
        id,
        method: "Runtime.evaluate",
        params: { expression, awaitPromise: true, returnByValue: true },
      }),
    )
  })
}

/**
 * Runs inside the page: waits for the app surface to mount, then reads the
 * computed theme state off a real button and the provider root. Computed
 * styles are the point - inheritance and the cascade must resolve, which a
 * style-attribute or provider-presence check cannot prove.
 */
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
    console.log(
      `ok: themed surface rendered (font "${state.fontFamily}", fontFamilyBase "${state.tokenFontFamilyBase}", brandBackground "${state.tokenBrandBackground}")`,
    )
  } finally {
    chrome.kill("SIGKILL")
    rmSync(profile, { recursive: true, force: true })
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  await main()
}
