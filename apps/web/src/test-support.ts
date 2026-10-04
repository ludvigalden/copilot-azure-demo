/**
 * Loaded FIRST by App.test.tsx, before the app graph: openapi-fetch's
 * createClient captures globalThis.Request and globalThis.fetch at client
 * creation (module init of ./api/client), so the absolutizing Request and the
 * recording fetch must be in place before that first import. They are plain
 * globals for the whole file run; per-test routing swaps `setFetchImpl`.
 */
export const API_ORIGIN = "https://demo.local"

class AbsoluteRequest extends Request {
  constructor(input: RequestInfo | URL, init?: RequestInit) {
    const resolved =
      typeof input === "string" && input.startsWith("/") ? new URL(input, API_ORIGIN).href : input
    super(resolved, init)
  }
}

export type FetchLogEntry = { url: string; authorization: string | null }

let impl: (request: Request) => Response | Promise<Response> = () => jsonResponse({}, 404)
const log: FetchLogEntry[] = []

async function recordingFetch(input: RequestInfo | URL): Promise<Response> {
  const request = input as Request
  log.push({
    url: request.url,
    authorization: request.headers.get("authorization"),
  })
  return impl(request)
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  })
}

globalThis.Request = AbsoluteRequest as typeof Request
globalThis.fetch = recordingFetch as typeof fetch

export function setFetchImpl(next: (request: Request) => Response | Promise<Response>): void {
  impl = next
}

export function fetchLog(): readonly FetchLogEntry[] {
  return log
}

export function clearFetchLog(): void {
  log.length = 0
}
