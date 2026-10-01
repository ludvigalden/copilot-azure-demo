import { describe, expect, it } from "vitest"
import type { components } from "./api/schema"
import { needsAuth } from "./auth"

type AuthConfig = components["schemas"]["AuthConfig"]

const auth: AuthConfig = {
  clientId: "client",
  tenantId: "tenant",
  scope: "api://client/access_as_user",
}

describe("needsAuth", () => {
  it("is false when the API reports no sign-in configuration", () => {
    expect(needsAuth({ auth: null })).toBe(false)
  })

  it("is false when /config returned nothing usable", () => {
    expect(needsAuth(undefined)).toBe(false)
    expect(needsAuth(null)).toBe(false)
  })

  it("is true when the API returned Entra configuration", () => {
    expect(needsAuth({ auth })).toBe(true)
  })
})
