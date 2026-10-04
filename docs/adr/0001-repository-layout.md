# 0001 - Repository layout

- **Status:** Accepted
- **Date:** 2026-10-01

## Context

The repository holds several distinct units that ship and run in
different ways: a React single-page app, an ASP.NET Core API, a
Power Platform custom connector definition, a Python ingestion job, an
evaluation suite, one shared OpenAPI document, and a small knowledge
base. Each unit needs an unambiguous home, and a reader should be able
to tell from the top-level directory alone what runs where and who
talks to it.

## Decision

The top level is divided by role:

| Path | Contents | Why here |
|---|---|---|
| `apps/web/` | The React SPA | A person interacts with it directly. |
| `apps/teams/` | The Teams app package source: the manifest and icons for the installable app | A person installs it in a Teams client; the package is built and published by scripts, never by hand. |
| `apps/power-platform/` | The custom connector definition, generated from the OpenAPI document | Imported into Power Platform, where a maker builds on it. |
| `services/api/` | The ASP.NET Core API | A server-side unit: other programs (the SPA, a client of the custom connector) talk to it over the network. |
| `services/ingest/` | The Python ingester | A server-side unit: a batch job that chunks `kb/`; nothing calls it over the network. |
| `contracts/openapi/` | The OpenAPI document, its lint ruleset and the generation script | One language-neutral contract with three consumers; it is a reviewed source document, not a library, so it gets its own root rather than living under any one language's tree. |
| `kb/` | The knowledge base articles | A tiny corpus read by the ingester and cited by URL; it is content, not code, and belongs at the top where a non-developer can find it. |
| `eval/` | The evaluation project and golden set | Statistical quality measurement is a distinct activity from deterministic testing and gets its own project. |
| `infra/terraform/bootstrap/` | The one-time Terraform root: the remote state backend and the deployment identity | A root cannot create the state store its own state lives in, so this root is applied once by hand and everything after it is applied by the pipeline. |
| `infra/terraform/main/` | Every Azure resource the application needs | The declarative description of the running system; the pipeline plans and applies it. |
| `Dockerfile` | The one container image | The SPA and the API ship together: a change is reviewed as one unit and deployed as one unit. |
| `.dockerignore` | The image build context boundary | The context carries only what the two build stages read, so nothing else in the repository reaches the image. |
| `.github/workflows/ci.yml` | The pull-request gate: contract drift and tests | The repository's checks run identically for every change, from one committed definition. |
| `.github/workflows/app.yml` | The image build and the container app update | Delivery is triggered by a merge, not by hand; the workflow is the only writer of the image. |
| `.github/workflows/infra.yml` | Terraform plan and apply of the main root | Infrastructure changes are reviewed as plans before they reach Azure. |
| `.github/workflows/ingest.yml` | The scheduled knowledge-base ingestion | The corpus and its processing cadence are repository content, not manual steps. |
| `scripts/` | Small operational entry points: the local workflow runner, the headless bot conversation proof, the Teams package build and publish, the retirement scripts for the banked artifacts | One command per recurring operation; each script is the documented way to do that operation, so no step lives only in someone's shell history. |
| `docs/adr/` | Architecture decision records | Numbered and dated, one file per decision. |

`apps/` versus `services/` follows a single question: does a person
interact with it directly (`apps/`), or does it run server-side where
other programs consume it (`services/`)? The API is the network service;
the ingester runs as a batch job rather than listening on a port,
but it is still a server-side unit, so it belongs with the API rather
than with anything user-facing.

Tests live next to the code they cover: a sibling `*.Tests` project in
the .NET solution, and a `tests/` directory inside each Python project.
This is the convention of both ecosystems, and it keeps a unit and its
tests in one reviewable place.

## Consequences

- A newcomer can map every top-level directory to the role it plays
  without reading the CI configuration.
- The knowledge base sits beside the code even though it is prose;
  its citation URLs point at these files in the repository, so the
  layout is part of the product.
