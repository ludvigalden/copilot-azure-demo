import { describe, expect, it } from "vitest"
import { expectedContentTypes, extractAssetUrls } from "./check-assets.mts"

// Hermetic: only the extraction and content-type logic runs here; the
// network fetches run where the script is invoked directly, in CI.
describe("the staged-assets gate", () => {
  it("extracts, absolutizes, and deduplicates the asset references of an index", () => {
    const html = [
      "<html><head>",
      '<script type="module" crossorigin src="/assets/index-BqX3.js"></script>',
      '<link rel="stylesheet" href="assets/theme-9f2.css">',
      "<link rel='stylesheet' href='assets/solo-1.css'>",
      '<link rel="icon" href="https://cdn.example.com/favicon.ico">',
      '<script src="/assets/index-BqX3.js"></script>',
      "</head><body></body></html>",
    ].join("")
    expect(extractAssetUrls(html, "https://staging.example.com/app/")).toEqual([
      "https://staging.example.com/assets/index-BqX3.js",
      "https://staging.example.com/app/assets/theme-9f2.css",
      "https://staging.example.com/app/assets/solo-1.css",
      "https://cdn.example.com/favicon.ico",
    ])
  })

  it("skips data URLs, fragments, and non-http schemes", () => {
    const html = [
      '<a href="#top">top</a>',
      '<img src="data:image/png;base64,AAAA">',
      '<a href="javascript:void(0)">no</a>',
      '<a href="mailto:it@example.com">mail</a>',
      "<a href=''>empty</a>",
      '<img src="/real.png">',
    ].join("")
    expect(extractAssetUrls(html, "https://staging.example.com/")).toEqual([
      "https://staging.example.com/real.png",
    ])
  })

  it("promises the content type the extension implies, and any type when unknown", () => {
    expect(expectedContentTypes("https://x.example/assets/index-BqX3.js")).toEqual([
      "text/javascript",
      "application/javascript",
    ])
    expect(expectedContentTypes("https://x.example/assets/theme.css")).toEqual(["text/css"])
    expect(expectedContentTypes("https://x.example/index.html")).toEqual(["text/html"])
    expect(expectedContentTypes("https://x.example/logo.svg")).toEqual(["image/svg+xml"])
    expect(expectedContentTypes("https://x.example/photo.webp")).toEqual(["image/webp"])
    expect(expectedContentTypes("https://x.example/health")).toBeNull()
  })
})
