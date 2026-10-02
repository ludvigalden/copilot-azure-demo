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
