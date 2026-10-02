# IT Support Assistant

Copyright (c) 2026 Ludvig Aldén. All rights reserved; see [LICENSE](LICENSE).

A working demo of an IT-support assistant built on ASP.NET Core,
React and Microsoft Entra ID. Ask it a question and it answers from a
small knowledge base of Markdown articles, citing the source
articles. It can look up your profile and manager in Microsoft
Graph, and it can escalate a conversation into a support ticket — in a
built-in Table Storage store, or in ServiceNow when ServiceNow
credentials are configured.

The repository is deliberately small and readable end to end. Every
technology it uses does a real job:

| Technology | Role |
|---|---|
| TypeScript (React, Fluent UI) | The chat SPA the user talks to. |
| C# / ASP.NET Core | The HTTP API: answers with citations, profile lookup, ticket creation. |
| Python | Chunking the knowledge base into citation-tagged passages, and evaluation of answer quality. |

## Repository layout

| Path | Contents |
|---|---|
| `apps/web/` | The React SPA. |
| `apps/power-platform/` | The Power Platform custom connector definition, generated from the OpenAPI document. |
| `services/api/` | The ASP.NET Core API and its tests. |
| `services/ingest/` | The Python ingester (uv project): chunks `kb/` into passages with citation URLs and prints them as JSON (`--dry-run`). |
| `eval/` | The Python evaluation project (uv project): the golden question set and the quality gate. |
| `contracts/openapi/` | The OpenAPI document, the Spectral ruleset and the generation script. |
| `kb/` | The knowledge base articles. |
| `infra/terraform/bootstrap/` | The one-time Terraform root: the remote state backend and the deployment identity. |
| `infra/terraform/main/` | The Terraform root that owns every Azure resource. |
| `Dockerfile` | The single container image: the SPA built and copied into the API image. |
| `.dockerignore` | The image build context: only what the two build stages read. |
| `.github/workflows/ci.yml` | The pull-request gate: contract regeneration drift check, lints and all test suites. |
| `.github/workflows/app.yml` | Builds the container image into the GitHub Container Registry and updates the container app. |
| `.github/workflows/infra.yml` | Plans the Terraform `main/` root on pull requests and applies it on pushes to `main`. |
| `.github/workflows/ingest.yml` | Runs the knowledge-base ingester on a weekly schedule and on changes. |
| `docs/adr/` | Architecture decision records. |

## Contract-first API

`contracts/openapi/openapi.yaml` is the single source of truth for the
HTTP surface. Three artefacts are generated from it and committed: the
C# server's abstract controllers (NSwag, at build time), the
TypeScript client types (openapi-typescript), and the Power Platform
custom connector definition (`hidi`, swagger 2.0 plus a `jq` host
step). One script regenerates everything:

```sh
sh contracts/openapi/generate.sh
```

CI regenerates on every pull request and fails if the committed
outputs drift from the document, so the contract and its consumers can
never disagree. The document is linted with Spectral:

```sh
npm run lint:openapi --prefix apps/web
```

## Local development

Prerequisites: the .NET 10 SDK, Node 24, `uv` (Python 3.13 is fetched
automatically), `jq` (for regenerating the contract outputs), and
Docker for the Azurite table service (exact command in the local
development section).

```sh
# 1. Load the environment (a copy of .env.example runs on the stand-ins).
cp .env.example .env.local
set -a; . ./.env.local; set +a

# 2. Start the Azurite table service (keep it running).
docker run --rm -p 10002:10002 mcr.microsoft.com/azure-storage/azurite \
    azurite-table --tableHost 0.0.0.0 --inMemoryPersistence --skipApiVersionCheck

# 3. In a second terminal: install and run the API.
npm ci --prefix apps/web
dotnet run --project services/api/ItSupport.Api

# 4. In a third terminal: run the SPA dev server (proxies /api to the API).
npm run dev --prefix apps/web
```

The API serves the built SPA from `apps/web/dist` on
`http://localhost:5080`, so `npm run build --prefix apps/web` plus step
3 alone is enough if you do not want the dev server. Locally there is
no sign-in: a fixed development user stands in for Entra ID, and a
stub answers questions from `kb/` until a real provider is configured.
See ADR 0003 for how configuration selects real versus stand-in
implementations.

## Tests and checks

```sh
dotnet test ItSupport.slnx                        # API tests
npm test --prefix apps/web                        # SPA tests
uv run --directory services/ingest pytest       # ingester tests
uv run --directory eval pytest                  # quality-gate tests
```

Linting: `dotnet format ItSupport.slnx --verify-no-changes`,
`npm run lint --prefix apps/web` (Biome), and `uv run ruff check` /
`uv run ruff format --check` in each Python project.

## Infrastructure and deployment

Two Terraform roots under `infra/terraform/` describe the Azure side.
`bootstrap/` creates the remote Terraform state backend and the
deployment identity the workflows assume; it is applied once,
locally, because nothing else can create the state store the
pipeline depends on. `main/` owns every application resource: Azure
OpenAI with a chat and an embedding model deployment, Azure AI
Search on the free tier holding the Terraform-managed knowledge-base
index, Table Storage for tickets, and one Azure Container App that
scales to zero. Azure access rides managed identities and
least-privilege role assignments; the one credential Terraform
handles is the search index's Azure OpenAI vectorizer key, which it
delivers through a write-only value that never lands in Terraform
state or output. See ADR 0004 for the decisions behind this shape.

Four workflows in `.github/workflows/` run the pipeline:

- `ci.yml` regenerates the contract outputs and fails on drift, and
  runs the tests and lints of the API, the SPA and both Python
  projects; it gates every pull request.
- `infra.yml` plans the `main/` root on pull requests and applies it
  on pushes to `main`, authenticating to Azure over OpenID Connect
  with no stored cloud secrets.
- `app.yml` builds the one container image and stores it in the
  GitHub Container Registry — images live with the repository, not
  in a cloud registry — then points the container app at the new
  image on the `demo` environment.
- `ingest.yml` runs the knowledge-base ingester on a weekly schedule
  and on every change to the articles or the ingester.

The image itself is built by `Dockerfile`: the SPA is built first and
copied into the API image, so one image serves both. Terraform
creates the container app with a placeholder image and leaves the
image to `app.yml`: the app serves the placeholder until that
workflow's first run, and the freshly built image from then on.

None of this is needed to run the application locally; the local
development section above covers the zero-Azure setup.

## Further reading

- [ADR 0001](docs/adr/0001-repository-layout.md) — why the repository is laid out this way.
- [ADR 0002](docs/adr/0002-contract-first-http-api.md) — the contract-first generation chain and the drift gate.
- [ADR 0003](docs/adr/0003-configuration-selects-implementation.md) — how configuration selects real implementations or stand-ins.
- [ADR 0004](docs/adr/0004-infrastructure-and-delivery.md) — the infrastructure and delivery decisions.
