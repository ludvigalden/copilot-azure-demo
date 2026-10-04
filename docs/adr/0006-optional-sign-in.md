# 0006 - Optional sign-in and guest access

- **Status:** Accepted
- **Date:** 2026-10-04

## Context

Every endpoint except the client configuration required an Entra
bearer token, so a visitor who had not signed in got an HTTP 401 from
the profile lookup, from answers, and from ticket creation alike. The
SPA mirrored that: it forced a redirect sign-in before rendering
anything, so the first thing a visitor saw was a login page rather
than the product. For a demo whose point is to be tried, the wall
sits in front of the value: someone evaluating the assistant without
a directory account — or before consenting to one — could answer no
question and create no ticket.

## Decision

Sign-in is optional. The profile, answers, and ticket-creation
endpoints accept anonymous callers and serve them as a guest: the
guest profile is static, and a guest's ticket records a fixed guest
caller. The caller identity on a ticket always comes from the
validated access token when one is presented and is the guest person
otherwise — never a value from the request body. A signed-in caller's
behavior is unchanged: their token still validates under the API's
Entra configuration, and the profile is still read from the directory
on the caller's behalf. The SPA
renders signed out by default, attaches a bearer token only when an
account is signed in, and offers sign-in as an ordinary action in the
header rather than a gate.

Because the guest-open write endpoints are now reachable without
authentication, they are rate limited: a fixed window per client
address, twenty requests a minute by default, HTTP 429 beyond it,
using the rate limiter built into ASP.NET Core. The deployment sits
behind an ingress that proxies every request, so the forwarded-headers
middleware restores the caller's real address before the limit
partitions on it.

## Consequences

- A visitor can try the assistant end to end — ask, read the cited
  answer, escalate into a ticket, see their (guest) profile — without
  an account, and sign in when they want their identity on the
  record.
- The guest identity is fixed in the service, so a caller can never
  name someone else on a ticket by omitting or forging an identity.
- The abuse bound for the anonymous surface is the per-address rate
  limit: coarse, cheap, and effective against the realistic threat
  (a burst of scripted requests), at the known cost of shared addresses
  pooling into one bucket.
- The OpenAPI contract, the generated client, and the Power Platform
  connector describe the same surface: no security requirement on the
  three endpoints, no 401 responses, and a 429 documented on the two
  guest-open writes.
