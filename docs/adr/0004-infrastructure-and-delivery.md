# 0004 - Infrastructure and delivery

- **Status:** Accepted
- **Date:** 2026-10-02

## Context

The application runs in Azure, and the repository carries its whole
delivery: the resources exist because text in this repository
describes them, and a merge to `main` can rebuild both the resources
and the image. Three constraints shape how. The deployment sits idle
most of the time, so idle cost and idle surface should be near zero.
No long-lived cloud credential should exist to store, rotate or
leak — not in the repository, not in the workflow runner. And the
first provisioning run faces a circular dependency: the tool that
describes the resources needs a state store, which is itself a
resource.

## Decision

Two Terraform roots split provisioning at the circular dependency.
`infra/terraform/bootstrap/` runs once, locally: it creates the state
storage account and the deployment identity, then migrates its own
state into the account it just created. `infra/terraform/main/` owns
every application resource — the Azure OpenAI account with its chat
and embedding model deployments, the Azure AI Search service and the
knowledge-base index, the Table Storage account behind the ticket
store, the container app and its environment, and the Entra
applications for sign-in — and the pipeline applies it from then on.
One local apply stands between an empty subscription and a
pipeline-runnable environment; everything after it is reviewed text.

The workflows authenticate to Azure with OpenID Connect. The
bootstrap root creates one Entra application with a federated
credential trusting a single repository environment, and grants it
exactly the rights the main root needs: contributor and role
administration on the one application resource group — which the
bootstrap root creates so the grants can be scoped to it — access to
the one state container, and directory rights scoped to the
applications it creates itself. Each run exchanges its own job token
for Azure tokens; no cloud secret is stored in the repository, in
the runner, or anywhere else.

The container image is stored in the GitHub Container Registry. The
workflows already authenticate to GitHub, so the image push reuses
the repository's own token, and no second registry credential
exists. One image carries the SPA and the API together: a change is
reviewed as one unit and ships as one unit.

The application runs on a single Azure Container App on the
Consumption plan, scaling from zero replicas when idle to one under
load. An idle deployment costs no compute; a request pays a cold
start instead. Terraform creates the app with a placeholder image
and then never modifies the image — pointing the app at a new image
is the delivery workflow's job, on every run.

The knowledge-base index fits inside the Azure AI Search free tier,
so the search service costs nothing. The service accepts Entra
identities on its data plane, so the application identity and the
deployment identity read and write the index through role
assignments rather than query keys.

One integration uses a key: the index's vectorizer calls the
embedding deployment with the Azure OpenAI account's key. Terraform
passes it through a write-only attribute that is sent to the search
service's API and nowhere else — the key is absent from Terraform
state, from plan output and from every output value. Every storage
account in the deployment runs with shared-key access disabled, and
the application's Azure access rides managed identities rather than
keys.

## Consequences

- The environment is rebuildable from the repository alone: after
  the one local bootstrap apply, the roots describe every resource
  and the workflows are the only writer.
- A pull request that changes infrastructure produces a plan and
  nothing else; the apply belongs to `main`, so the distance between
  reviewed and running state is visible in a plan before it is
  committed.
- The container app serves a placeholder image until the delivery
  workflow's first run; from that run on, the workflow replaces the
  image on every change it covers.
- Scaling to zero trades cold-start latency for an idle cost of
  nothing, which matches how a demo is actually used.
- The free tier bounds the search service (one service per
  subscription, a 50 MB index); the paid tier stays a one-line
  variable change if the corpus outgrows it.

## Amendment — 2026-10-05

Pull requests perform credential-free static validation and a non-publishing
image build, not a cloud plan. Reusable workflows inherit the caller event;
`release_candidate` explicitly selects their trusted main-branch cloud legs.
The protected `demo` environment and its reviewer remain unchanged.

The release coordinator builds one image, labels its immutable source, and
stages its canonical digest. Before approval, it runs readiness, asset MIME,
browser theme and bot conversation checks for a selected app, then evaluates
all 15 golden questions for selected app, KB or evaluation changes. Weekly
production ingestion also requires staging retrieval and the same evaluator.
These jobs use the existing judge key and OIDC Search access; no PR runs live AI.

`eval/release.py` captures the ready revision, OCI source, immutable KB tree and
exact chunks, index definition and snapshot, model deployments and versions,
dataset and provider/judge prompt hashes, and workflow run attempt. Capture must
match before and after evaluation. Promotion checks expiry and re-captures
staging immediately before mutation; an unavailable binding or changed candidate
fails closed. Approval artifacts retain the full candidate and results, identify
performed and unavailable checks, and mark selected components awaiting approval.
Production uses that image digest and hash-checks the privately saved Terraform
plan rather than building or planning again.

Each question requires groundedness at least 4, mean relevance at least 4, and
an expected citation among the first 3. Attempts are capped at 4 answer and 6
judge calls per question, 150 total. Answer reservations are capped at 15,360
output tokens with an enforced 256-token API completion limit. Judge requests
reserve input bytes plus 300 output tokens, at most 8,000 input bytes per
request and 150,000 reserved/actual combined prompt and completion tokens per
run. A monotonic 900-second deadline and 20-minute job timeout bound execution;
evidence expires two hours after its start. Malformed or partial evidence fails.

CI still owns the staged image build; locally built staging deployment remains
unavailable as described in `docs/local-loop.md`. Delegated Graph/OBO proof is
separate manual evidence, not a release claim. Concurrency serializes active
runs but replaces pending runs rather than guaranteeing a durable queue.
Workflow consolidation, local-image deployment, rollback, previews, blue-green,
versioned indexes and managed downstream Power Platform remain separate work.
