import createClient from "openapi-fetch"
import type { components, paths } from "./schema"

export type AuthConfig = components["schemas"]["AuthConfig"]
export type ClientConfig = components["schemas"]["ClientConfig"]

export const api = createClient<paths>({ baseUrl: "/api" })

/** True only when /api/config returned a non-null auth block. */
export function needsAuth(
  config: ClientConfig | null | undefined,
): config is ClientConfig & { auth: AuthConfig } {
  return config?.auth != null
}

/**
 * Thrown at the client's auth boundary when a signed-in account's token
 * cannot be renewed silently. The request is blocked: it must not travel
 * without credentials and have the API answer as a guest by accident. The
 * UI catches this and offers an explicit re-auth, continue-as-guest or
 * sign-out choice.
 */
export class ReauthRequiredError extends Error {}
