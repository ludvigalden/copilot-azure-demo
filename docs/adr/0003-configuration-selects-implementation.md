# 0003 - Configuration selects the implementation

- **Status:** Accepted
- **Date:** 2026-10-01

## Context

The same code base must run in two very different settings. Locally,
there is no Azure tenant, no Microsoft Entra ID app registration and no
ServiceNow instance, yet the API must start, serve the SPA and create
tickets against a local store. With real credentials, the same API
must use Entra ID, Microsoft Graph and ServiceNow. Choosing between the
two modes with command line flags or build configurations would duplicate the deployment
matrix into tooling, and the flag combination nobody tests is the one
that breaks in production.

## Decision

The presence of a configuration value selects the real implementation;
its absence selects a development-only stand-in, and outside the
Development environment a missing value is a startup error rather than
a silent fallback:

- `AzureAd:ClientId` set: real Entra ID JWT authentication and the
  Microsoft Graph profile lookup. Absent in Development: a fixed dev
  principal and a stub user directory. Absent elsewhere: the API
  refuses to start.
- `ServiceNow:InstanceUrl` set: tickets are created in ServiceNow
  through its REST API with basic authentication. Absent: tickets go
  to the built-in Table Storage store.
- The stub answer provider runs only in Development while no real
  answer provider is configured; any other combination refuses to
  start.

A verbatim copy of `.env.example` therefore produces a fully working
local setup on the stand-ins: the Entra and ServiceNow entries are
present but commented out.

`.env.local` carries the real local values and is git-ignored. It is
loaded by sourcing it from the repository root with the shell's
`set -a; . ./.env.local; set +a`, before starting the API. ASP.NET
Core reads `__`-separated environment variables as its `:`-separated
configuration keys natively, so no dotenv library is added to the
project.

## Consequences

- No flags, no build configurations, no feature-toggle plumbing;
  the environment is the only selector.
- The stand-ins cannot leak into production: outside Development the
  same absence that selects a stand-in fails the startup instead.
- Every value that varies between environments is listed in
  `.env.example` with a placeholder and a one-line comment, and the
  commented-out entries document exactly which integrations are
  optional.
- The source-it-yourself route assumes a POSIX shell; this is the
  documented local-development path.
