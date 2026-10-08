# IT Support Assistant

Copyright (c) 2026 Ludvig Aldén. All rights reserved; see [LICENSE](LICENSE).

A working demo of an IT-support assistant built on ASP.NET Core,
React and Microsoft Entra ID. Ask it a question and it answers from a
small knowledge base of Markdown articles, citing the source
articles. Signed in on the web, it can look up your profile and
manager in Microsoft Graph, and it can escalate a conversation into a
support ticket — in a built-in Table Storage store, or in ServiceNow
when ServiceNow credentials are configured. The web app is one of two
doors in: the same API also hosts a conversational agent that any Bot
Framework channel can reach — Direct Line for headless clients, with
the Microsoft Teams channel live and the installable app package ready
to publish once the tenant carries Teams. Bot callers are recorded as
guests until single sign-on verifies them
([docs/agent.md](docs/agent.md)).

The deployed demo runs at <https://copilot-azure.demo.ludvigalden.com>.
Signing in requires an account in the demo's Microsoft Entra tenant;
everyone else can continue as a guest.

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
| `apps/teams/` | The Teams app package source: manifest and icons, built and published by scripts in `scripts/`. |
| `apps/power-platform/` | The unmanaged solution source: the custom connector payload (generated from the OpenAPI document), the environment variable definition, and the empty deployment settings the pipeline fills. |
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
| `.github/workflows/release.yml` | The release coordinator entrypoint: a thin locked wrapper — on pushes to `main` it takes the `shared-target-demo` lease and calls the reusable `release-candidate.yml` body, which validates everything through `ci.yml`, calls the component bodies for what changed, and runs the production legs behind the demo environment's approval. |
| `.github/workflows/release-candidate.yml` | The release body: component selection, the staging legs, evaluation, the summary and the approved production legs. Reusable-only, no triggers, no concurrency of its own. |
| `.github/workflows/app.yml` | Builds the container image into the GitHub Container Registry and deploys it to the staging container app when the release coordinator calls it. |
| `.github/workflows/infra.yml` | The infra entrypoint: a thin locked wrapper that plans the Terraform `main/` root on pull requests and on manual dispatch, holding the `shared-target-demo` lease for the whole run; the reusable `infra-candidate.yml` body applies staging when the release coordinator calls it and saves the production plan for the approved apply. |
| `.github/workflows/infra-candidate.yml` | The infra body: static checks, the staging plan and apply, and the saved production plan. Reusable-only, no triggers, no concurrency of its own. |
| `.github/workflows/ingest.yml` | The ingest entrypoint: a thin locked wrapper running on pull requests, the weekly schedule and manual dispatch, holding the `shared-target-demo` lease for the whole run; the reusable `ingest-candidate.yml` body fills the staging index, with the production index filled on the weekly run. |
| `.github/workflows/ingest-candidate.yml` | The ingest body: knowledge-base validation, the staging fill, the retrieval check and the scheduled production fill. Reusable-only, no triggers, no concurrency of its own. |
| `.github/workflows/power-platform.yml` | Packs the unmanaged solution on pull requests and imports it to staging once the staging environment variable is configured. |
| `scripts/` | Small operational entry points: the local workflow runner, the headless bot conversation proof, the Teams package build and publish. |
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
index, Table Storage for tickets, one Azure Container App that
scales to zero and serves production from its own hostname over a
free managed certificate the same root issues and binds, and the
Azure Bot registration that points Bot
Framework channels at the app's `/api/bot/messages` endpoint. The
bot's Microsoft App identity is the application's user-assigned
managed identity — the bot's app ID is that identity's client ID —
so channel messages authenticate without a client secret anywhere.
Azure access rides managed identities and
least-privilege role assignments; the one credential Terraform
handles is the search index's Azure OpenAI vectorizer key, which it
delivers through a write-only value that never lands in Terraform
state or output. See ADR 0004 for the decisions behind this shape.

The same root also describes **staging** under the `staging`
environment: a second apply of `infra/terraform/main/` with its own
prefix, resource group, and state key that consumes the shared search
service, AI Services account, container apps environment, and Entra
application by reference and duplicates the rest (container app,
identity, tickets account, bot). Staging applies before production in
the same workflow runs, so every change answers for itself in staging
first; its knowledge lives in its own index on the shared service.

Six workflows in `.github/workflows/` run the pipeline, across nine
files. Five own one component each; `release.yml` coordinates them on
pushes to `main`. Three files — `release.yml`, `ingest.yml` and
`infra.yml` — are thin entrypoints: each keeps its event triggers, owns
the one `shared-target-demo` concurrency group that serializes every
write to the shared demo target for its whole run, and calls a
reusable-only `*-candidate.yml` body that carries no triggers and no
concurrency of its own:

- `ci.yml` regenerates the contract outputs and fails on drift, and
  runs the tests and lints of the API, the SPA and both Python
  projects; it gates every pull request.
- `infra.yml` plans the `main/` root for both environments on pull
  requests, applies staging first when the release coordinator calls
  it, and saves the production plan for the approved apply,
  authenticating to Azure over OpenID Connect with no stored cloud
  secrets.
- `app.yml` builds the one container image and stores it in the
  GitHub Container Registry — images live with the repository, not
  in a cloud registry — then points the staging container app at
  that image and verifies it there, when the release coordinator
  calls it.
- `ingest.yml` runs the knowledge-base ingester on a weekly schedule
  and on every change to the articles or the ingester; the staging
  index is filled first, the release coordinator checks retrieval
  against it, and the weekly run fills the production index.
- `power-platform.yml` packs the unmanaged solution from
  `apps/power-platform/` on every pull request, and — once the
  staging environment variable is configured — imports and publishes
  it to staging with federated authentication. The setup checklist is
  [docs/power-platform-setup.md](docs/power-platform-setup.md).
- `release.yml` runs on every push to `main`: it detects which
  components changed, validates everything through `ci.yml`, and
  calls the component workflows for what changed. What the release
  built — image digest, knowledge-base commit, plan hash, solution
  version — is collected into a `release-identity` artifact, and the
  production legs that follow sit behind the demo environment's
  approval: the container app update to the verified digest, the
  apply of the saved Terraform plan, the production index fill, and
  the solution import with its API host resolved.

The image itself is built by `Dockerfile`: the SPA is built first and
copied into the API image, so one image serves both. Terraform
creates the container app with a placeholder image and leaves the
image to `app.yml`: the app serves the placeholder until that
workflow's first run, and the freshly built image from then on.

The workflows also run on the workstation against staging before a
change is pushed: [docs/local-loop.md](docs/local-loop.md) carries
the setup and the one command per workflow, and every mutating step
(apply, registry push, production deploy) is gated so a local run
stops where its credentials end.

None of this is needed to run the application locally; the local
development section above covers the zero-Azure setup.

## Further reading

- [ADR 0001](docs/adr/0001-repository-layout.md) — why the repository is laid out this way.
- [ADR 0002](docs/adr/0002-contract-first-http-api.md) — the contract-first generation chain and the drift gate.
- [ADR 0003](docs/adr/0003-configuration-selects-implementation.md) — how configuration selects real implementations or stand-ins.
- [ADR 0004](docs/adr/0004-infrastructure-and-delivery.md) — the infrastructure and delivery decisions.
- [ADR 0005](docs/adr/0005-agent-in-code.md) — why the conversational agent
  is ordinary code, with the Power Platform agent banked.
- [ADR 0007](docs/adr/0007-releasable-delivery-and-a11y.md) — the
  pipeline's entrypoint/candidate shape, its release-evidence chain,
  caller-event gates, and the accessible conversation feed.
- [The conversational agent](docs/agent.md) — the bot, its intents, its
  channels, and the headless proof.
- [Local workflow runs](docs/local-loop.md) — running the
  deployment workflows on the workstation against staging before a
  push.
- [Copilot Studio agent design](docs/copilot-studio-agent.md) — the
  banked Power Platform agent's design: its escalation topic and its
  agent flow.
- [Power Platform setup checklist](docs/power-platform-setup.md) —
  the one-time environment setup and verification for the banked
  path.
