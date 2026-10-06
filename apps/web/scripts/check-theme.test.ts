import { describe, expect, it } from "vitest"
import { EXPECTED_THEME, themeRejection } from "./check-theme.mts"

// Hermetic: only the acceptance predicate runs here; the browser probe
// runs where the script is invoked directly, in CI.
describe("the theme gate acceptance predicate", () => {
  const computed = {
    fontFamily: EXPECTED_THEME.fontFamilyBase,
    tokenFontFamilyBase: EXPECTED_THEME.fontFamilyBase,
    tokenBrandBackground: EXPECTED_THEME.brandBackground,
    providerFontFamilyBase: EXPECTED_THEME.fontFamilyBase,
    providerBrandBackground: EXPECTED_THEME.brandBackground,
    controlRadius: EXPECTED_THEME.controlRadius,
    surfaceRadius: EXPECTED_THEME.surfaceRadius,
    neutralBackground: EXPECTED_THEME.neutralBackground,
    neutralForeground: EXPECTED_THEME.neutralForeground,
  }

  it("accepts the computed state that matches the intended tokens", () => {
    expect(themeRejection(computed)).toBe("")
  })

  it("accepts an equivalent font stack regardless of spacing after commas", () => {
    const spaced = {
      ...computed,
      fontFamily: 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    }
    expect(themeRejection(spaced)).toBe("")
  })

  it("rejects a browser-default font and uncomputed tokens with the reason", () => {
    expect(themeRejection({ ...computed, fontFamily: '"Times New Roman", serif' })).toContain(
      "browser-default font",
    )
    expect(themeRejection(undefined)).toContain("computes no theme tokens")
    expect(themeRejection({ ...computed, tokenBrandBackground: "" })).toContain(
      "computes no theme tokens",
    )
    expect(themeRejection({ ...computed, providerBrandBackground: "" })).toContain(
      "computes no theme tokens",
    )
  })

  it("rejects a drifted token and names the offending value", () => {
    const rejection = themeRejection({ ...computed, surfaceRadius: "4px" })
    expect(rejection).toContain("differs from the intended tokens")
    expect(rejection).toContain("4px")
  })
})
