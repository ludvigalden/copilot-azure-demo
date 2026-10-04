# Local workflow runs

The deployment workflows — the same files Actions runs — also run on
the workstation, against **staging** and nothing else. A change that
touches a workflow, the Terraform roots, or anything the pipelines
build is exercised locally before it is pushed: `infra.yml` reaches
its plan, `app.yml` reaches a local image build and the staging
deploy, `ingest.yml` reaches the staging push, and
`power-platform.yml` reaches its pack. Pushing to `main` is what
applies; this loop is the rehearsal.

The runner executes the workflow files inside containers on the local
Docker daemon, so the first requirement is a running Docker. The
runner itself is a single static binary; the
[install note](https://nektos.github.com/act/installation/) carries
the one-liner for it.

## One-time setup

**1. A staging-scoped credential.** OpenID Connect does not exist
outside Actions, so the local run signs in with a service principal
whose roles touch staging and nothing wider:

```sh
az ad sp create-for-rbac \
  --name <prefix>-act \
  --role Contributor \
  --scopes "$(az group show -n <prefix>-staging-rg --query id -o tsv)"
```

plus three narrow grants the pipelines need to read and write their
own footprint: `Reader` on the main resource group (the shared
services are read there), `Search Service Contributor` on the search
service (the Terraform-owned index definitions are read there), and
`Storage Blob Data Contributor` on the state container — at container
scope, because Terraform lists the blobs in it to find its workspaces,
and a blob-scoped grant cannot list:

```sh
rg_id="$(az group show -n <prefix>-rg --query id -o tsv)"
srch_id="$(az search service show -g <prefix>-rg \
  -n <prefix>-srch --query id -o tsv)"
state_id="<state-account-id>/blobServices/default/containers/tfstate"

az role assignment create --assignee <sp-app-id> --role "Reader" --scope "$rg_id"
az role assignment create --assignee <sp-app-id> \
  --role "Search Service Contributor" --scope "$srch_id"
az role assignment create --assignee <sp-app-id> \
  --role "Storage Blob Data Contributor" --scope "$state_id"
```

The principal's two remaining grants are managed by the staging apply
itself: set `ACT_PRINCIPAL_OBJECT_ID` on the `staging` GitHub
environment to the principal's object ID and its next run grants the
principal `Search Index Data Contributor` (documents, which the
ingester needs) and `Storage Table Data Contributor` on the staging
ticket store.

**2. The credential file.** The command that creates the principal
prints its secret once. Put the four values in `.env.local` (already
git-ignored, never committed, never pasted anywhere):

```dotenv
ARM_CLIENT_ID=<app id from the create output>
ARM_CLIENT_SECRET=<secret from the create output>
ARM_TENANT_ID=<tenant>
ARM_SUBSCRIPTION_ID=<subscription>
# The shared AI account's key: the local identity may read the account
# but not list its keys, and the staging index definition carries the key
# to the search service. Fetch it once as the account owner.
SHARED_AI_ACCOUNT_KEY=<key of the shared account>
```

**3. The local files.** Three small files sit next to the repository
and are un-committable by construction (the deny-by-default
`.gitignore` whitelists nothing that is not source):

- `.vars` — the staging environment's variables, which Actions serves
  from the GitHub environment and the local run serves from this file;
  the entry point passes it automatically:

  ```dotenv
  NAME_PREFIX=<prefix>-staging
  SHARED_PREFIX=<prefix>
  INDEX_NAME=<index>-staging
  CHAT_DEPLOYMENT_NAME=chat-staging
  EMBEDDING_DEPLOYMENT_NAME=embedding-staging
  TFSTATE_RESOURCE_GROUP=<prefix>-tfstate
  TFSTATE_STORAGE_ACCOUNT=<prefix>tfstate
  TFSTATE_KEY=main-staging.tfstate
  AZURE_CLIENT_ID=<cd app id>
  AZURE_TENANT_ID=<tenant>
  AZURE_SUBSCRIPTION_ID=<subscription>
  CD_PRINCIPAL_OBJECT_ID=<cd object id>
  ACT_PRINCIPAL_OBJECT_ID=<act object id>
  ```

- `event.json` — the event payload; its presence marks the run as
  local so production jobs skip themselves:

  ```json
  { "act": true }
  ```

- `.actrc` (optional) — standing flags the runner picks up
  automatically. The entry point pins the runner image itself
  (`catthehacker/ubuntu:act-latest` on the command line), so a
  `.actrc` platform override will not take effect; other standing
  flags still do.

## Running a workflow

```sh
scripts/local-run infra     # init, validate, plan — and stop
scripts/local-run app       # build the image, deploy staging
scripts/local-run ingest    # fill the staging index
scripts/local-run ci        # contracts, tests, lints
scripts/local-run power-platform  # pack the solution, skip the import
```

Extra options after the workflow name are passed through to the
runner (`-j` to select a job, `-v` to mount a volume, and so on).

## What the guards allow

Every mutating step that would leave the staging scope — or touch
production — is gated in the workflow files themselves, so the gate
travels with the pipeline:

- `infra.yml` applies only outside local runs; locally it stops after
  the plan. The production job skips itself entirely.
- `app.yml` pushes the image to the registry only on Actions; a local
  build stays on the machine, and the production deploy never runs
  locally. Run the App workflow locally only after the commit is
  pushed, so the registry already holds the image digest the staging
  deploy points at.
- `ingest.yml` fills the staging index locally and the production
  index only on Actions.
- `power-platform.yml` packs locally; the import is an Actions-only
  step.

## Limits of the local run

The runner is a faithful but not complete Actions host. What differs:

- `services:` containers, `workflow_call` reuse, concurrency groups,
  `permissions` blocks, and `timeout-minutes` are not enforced.
- Environment protection and environment-scoped secrets do not exist;
  the staging variables arrive through the `.vars` file instead.
- Matrix builds and Docker build contexts resolve with known quirks;
  the entry point mounts the Docker socket so image builds use the
  host daemon directly. The repository identity comes from the git
  remote, so a run from a scratch clone of the repository first points
  its origin back at the real repository, or the image name composes
  from the clone's path.
- Anything Actions-specific — the registry token, the cache service,
  OpenID Connect — is absent by design, and the guards keep steps
  that need it from running.
