// Creates a ticket through the generated API client and prints it.
// Point it at a running API with SMOKE_BASE_URL (default: local dev API).
import createClient from "openapi-fetch"
import type { paths } from "../src/api/schema.d.ts"

const baseUrl = process.env.SMOKE_BASE_URL ?? "http://localhost:5080/api"
const client = createClient<paths>({ baseUrl })

try {
  const { data, error, response } = await client.POST("/tickets", {
    body: { shortDescription: "Smoke test: ticket creation through the generated client" },
  })

  if (error !== undefined || data === undefined) {
    console.error(`Ticket creation failed (HTTP ${response.status}).`)
    process.exit(1)
  }

  console.log(JSON.stringify(data, null, 2))
} catch (cause) {
  console.error(`Could not reach the API at ${baseUrl}:`, cause)
  process.exit(1)
}
