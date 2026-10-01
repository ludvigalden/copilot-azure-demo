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
