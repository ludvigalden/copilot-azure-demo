// The staged-assets gate: fails the release unless every src/href asset
// in the SPA index serves 200 with the content type its extension
// promises. Stdlib-only so CI needs no extra dependency; unit-tested in
// check-assets.test.ts.
import { pathToFileURL } from "node:url"

/**
 * Content types an asset may answer with, keyed by file extension. An
 * extension absent from the table accepts any non-empty content type, so a
 * future asset kind fails closed only when it is missing entirely.
 */
const EXPECTED_CONTENT_TYPES: Record<string, string[]> = {
  js: ["text/javascript", "application/javascript"],
  mjs: ["text/javascript", "application/javascript"],
  css: ["text/css"],
  html: ["text/html"],
  json: ["application/json"],
  png: ["image/png"],
  jpg: ["image/jpeg"],
  jpeg: ["image/jpeg"],
  gif: ["image/gif"],
  svg: ["image/svg+xml"],
  ico: ["image/x-icon", "image/vnd.microsoft.icon"],
  webp: ["image/webp"],
  woff: ["font/woff"],
  woff2: ["font/woff2"],
  ttf: ["font/ttf"],
  eot: ["application/vnd.ms-fontobject"],
}

/** The src/href URLs an HTML page references, absolutized against its address. */
export function extractAssetUrls(html: string, baseUrl: string): string[] {
  const urls: string[] = []
  for (const match of html.matchAll(/(?:src|href)\s*=\s*(?:"([^"]*)"|'([^']*)')/g)) {
    const raw = (match[1] ?? match[2] ?? "").trim()
    if (!raw || raw.startsWith("#")) continue
    if (/^(data|javascript|mailto|tel):/i.test(raw)) continue
    let url: URL
    try {
      url = new URL(raw, baseUrl)
    } catch {
      continue
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") continue
    const resolved = url.href.replace(/#.*$/, "")
    if (!urls.includes(resolved)) urls.push(resolved)
  }
  return urls
}

/** The content types the gate accepts for a URL, or null for any non-empty type. */
export function expectedContentTypes(url: string): string[] | null {
  const path = url.split(/[?#]/)[0]
  const extension = path.slice(path.lastIndexOf(".") + 1).toLowerCase()
  return EXPECTED_CONTENT_TYPES[extension] ?? null
}

function contentType(response: Response): string {
  return (response.headers.get("content-type") ?? "").split(";")[0].trim().toLowerCase()
}

async function main(): Promise<void> {
  const base = process.argv[2]
  if (!base) {
    console.error(`FAIL: usage: node ${process.argv[1]} <base-url>`)
    process.exit(1)
  }
  let page: Response
  try {
    page = await fetch(base, { redirect: "follow" })
  } catch (cause) {
    console.error(`FAIL: could not fetch the page at ${base}:`, cause)
    process.exit(1)
  }
  if (page.status !== 200) {
    console.error(`FAIL: the page at ${base} answered ${page.status}, expected 200`)
    process.exit(1)
  }
  const pageType = contentType(page)
  if (!pageType.startsWith("text/html")) {
    console.error(
      `FAIL: the page at ${base} answered content type "${pageType}", expected text/html`,
    )
    process.exit(1)
  }
  const html = await page.text()
  const urls = extractAssetUrls(html, page.url || base)
  if (urls.length === 0) {
    console.error(
      `FAIL: the page at ${base} references no assets - the SPA entry script is missing`,
    )
    process.exit(1)
  }
  let checked = 0
  for (const url of urls) {
    let response: Response
    try {
      response = await fetch(url, { redirect: "follow" })
    } catch (cause) {
      console.error(`FAIL: could not fetch the asset ${url}:`, cause)
      process.exit(1)
    }
    if (response.status !== 200) {
      console.error(`FAIL: the asset ${url} answered ${response.status}, expected 200`)
      process.exit(1)
    }
    const type = contentType(response)
    const expected = expectedContentTypes(url)
    if (expected && !expected.includes(type)) {
      console.error(
        `FAIL: the asset ${url} answered content type "${type}", expected ${expected.join(" or ")}`,
      )
      process.exit(1)
    }
    checked += 1
    console.log(`ok: ${url} -> ${response.status} ${type}`)
  }
  console.log(`ok: ${checked} asset${checked === 1 ? "" : "s"} checked at ${base}`)
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  await main()
}
