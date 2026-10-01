# 0001 - Repository layout

- **Status:** Accepted
- **Date:** 2026-10-01

## Context

The repository holds several distinct units that ship and run in
different ways: a React single-page app, a Microsoft Copilot Studio
solution, an ASP.NET Core API, a Python ingestion job, an evaluation
suite, one shared OpenAPI document, a small knowledge base, and
Terraform for Azure and Entra ID. Each unit needs an unambiguous home,
and a reader should be able to tell from the top-level directory alone
what runs where and who talks to it.

## Decision

The top level is divided by role:

| Path | Contents | Why here |
|---|---|---|
| `apps/web/` | The React SPA | A person interacts with it directly. |
| `apps/power-platform/` | The unpacked Copilot Studio solution | Imported into Power Platform; a person builds and publishes it. |
| `services/api/` | The ASP.NET Core API | A server-side unit: other programs (the SPA, the Copilot Studio agent) talk to it over the network. |
| `services/ingest/` | The Python ingester | A server-side unit: a CD job runs it in Azure; nothing calls it over the network. |
| `contracts/openapi/` | The OpenAPI document, its lint ruleset and the generation script | One language-neutral contract with three consumers; it is a reviewed source document, not a library, so it gets its own root rather than living under any one language's tree. |
| `kb/` | The knowledge base articles | A tiny corpus read by the ingester and cited by URL; it is content, not code, and belongs at the top where a non-developer can find it. |
| `eval/` | The evaluation project and golden set | Statistical quality measurement is a distinct activity from deterministic testing and gets its own project. |
| `infra/terraform/bootstrap/` | One-time bootstrap: state backend and identities | Applied once, locally, by a human. |
| `infra/terraform/main/` | The main Terraform root | Applied by CD on every change. |
| `docs/adr/` | Architecture decision records | Numbered and dated, one file per decision. |

`apps/` versus `services/` follows a single question: does a person
interact with it directly (`apps/`), or does it run server-side where
other programs consume it (`services/`)? The API is the network service;
the ingester runs as a deployment job rather than listening on a port,
but it still runs in Azure on the server side, so it belongs with the
API rather than with anything user-facing.

Tests live next to the code they cover: a sibling `*.Tests` project in
the .NET solution, and a `tests/` directory inside each Python project.
This is the convention of both ecosystems, and it keeps a unit and its
tests in one reviewable place.

## Consequences

- A newcomer can map every top-level directory to a runtime
  destination without reading the CI configuration.
- The knowledge base sits beside the code even though it is prose;
  its citation URLs point at these files in the repository, so the
  layout is part of the product.
- Two Terraform roots mean two state files and one explicit
  bootstrap step; the split buys isolation between the one-time
  platform setup and the continuously applied workload.
