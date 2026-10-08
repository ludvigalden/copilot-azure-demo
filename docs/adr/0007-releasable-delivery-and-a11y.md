# 0007 - Releasable delivery and the accessible conversation

- **Status:** Accepted
- **Date:** 2026-10-08

This record consolidates four decisions that govern how a change on
`main` becomes releasable and how the interface behaves while that
happens: the pipeline's shape, the evidence that gates production, the
conditions that decide when a workflow job may mutate the cloud, and
the accessibility ruling behind the conversation surface. ADR 0004 and
its amendments describe these mechanics as they landed; this record
justifies them from the repository alone — the workflows, the local
runner script and the commit history are the implementation it
describes, and a reader needs nothing else.

## The entrypoint/candidate pipeline shape

### Context

One shared Azure target — a staging environment and a production
environment over shared search, AI and container-apps infrastructure —
is written by five component workflows and the release coordinator
alike. Unserialized, two runs interleave: the knowledge-base fill is
not an atomic operation (it merges or uploads index pieces and then
sweeps stale ones), so an overlapping run leaves the index
half-converged; Terraform plans contend for the same state lock; and
container-app updates can arrive out of order. At the same time, the
gate workflow must validate every pull request, the release
coordinator must reuse exactly that validation, and the component
workflows must run both standalone (pull requests, the weekly
schedule, manual dispatch) and inside a release run. A plain shared
concurrency group cannot express "one writer at a time" without also
saying who may write, and job conditions written against the wrong
event shape had already silently disabled a component leg: reusable
workflows inherit the caller's event, so the file a job lives in
proved nothing about what could reach it.

### Decision

The pipeline separates entry points from bodies. Three thin entry
points — `release.yml`, `infra.yml` and `ingest.yml` — keep the event
triggers and own the single `shared-target-demo` concurrency group
with `cancel-in-progress: false`, so the lease spans the whole run:
validation, the staging legs, evaluation, the summary, the approval
wait, the pre-apply recheck and every production leg. Their bodies are
reusable-only `*-candidate.yml` workflows that declare no triggers and
no concurrency at any depth. The release body calls the component
bodies directly, never the locked entry points, so one run acquires
the lease exactly once; a concurrency group inside a called body would
either make a run wait on itself or be silently ignored. Six named
job-level groups (`app-staging`, `ingest-staging`, `pp-staging`,
`infra-staging`, `infra-demo`, `deploy-demo`), all
`cancel-in-progress: false`, remain as defense in depth. The invariant
that survives the split is: a job's authority is decided by its
condition and its lease, never by which file it lives in.

### Consequences

- One run writes the shared target at a time; a standalone ingest or
  infra run queues behind an in-flight release instead of overlapping
  it.
- Cancellation is conservative everywhere (`cancel-in-progress: false`)
  because an in-progress write is never safe to interrupt mid-flight.
- The queue keeps a single pending run per group and replaces it, so
  serialization is guaranteed but a durable backlog is not: a third
  simultaneous entry point can still be dropped.
- The split costs one extra file per component and demands that every
  job condition be explicit, since inheritance means conditions — not
  file membership — carry the semantics.

## The release-evidence chain

### Context

The production legs deploy an image, apply a Terraform plan, fill the
production index and import a solution, all behind one
protected-environment approval. What the approver sees must be what
production consumes. Three failure shapes were live risks: a mutable
image tag can point at different bytes by the time production deploys
it; a Terraform plan can be regenerated after approval, silently
replacing what was reviewed; and a release summary that reports over
skipped legs publishes success for components that never ran. A
conventional fix — publishing evidence as workflow artifacts — does
not hold for a public repository, where workflow artifacts are
reachable anonymously.

### Decision

Every production mutation consumes evidence produced earlier in the
same run and re-verified immediately before mutation.

The image is deployed by its immutable digest, never a tag; promotion
re-captures the staging revision immediately before mutation and fails
closed if the binding is unavailable or the candidate has changed.

The production Terraform plan is computed once per run, hashed, and
saved with its metadata record to the private Terraform state storage
— never as a workflow artifact. The metadata binds the plan schema,
byte hash, creation and expiry timestamps, source commit, run ID and
attempt, plan ID and a release-candidate hash that itself binds
source, selected components and run/attempt before planning. Apply
downloads the saved plan, re-hashes it against the recorded hash,
verifies the metadata byte-for-byte against the approval manifest,
rejects expiry against the same 7,200-second freshness constant that
bounds evaluation evidence, and deletes the consumed blobs on success.
Apply never regenerates a plan under an earlier approval.

The run collects what it built into `release-identity.json` — source
commit, image digest, per-component status (with skipped components
labeled rather than omitted) and the staging verification results —
and uploads it as the `release-identity` artifact with
`if-no-files-found: error`. The summary job publishes only when the
legs it needs succeeded and every selected component carried its
evidence; a selection-empty run is labeled, never published green.

### Consequences

- What is approved is provably what is applied: a late approval runs
  out the freshness constant and stops before Terraform; a tampered or
  regenerated plan fails the re-hash; a reused approval fails the
  manifest comparison.
- A summary cannot publish success over skipped legs, and a manifest
  never exists for a run that selected nothing.
- Evidence stays private: saved plans and their metadata live in the
  state storage behind the deployment identity, not in anonymously
  reachable artifacts.
- The cost is mechanical: more steps per release, and approval
  lifetimes are bounded by the shared freshness constant, so a slow
  review regenerates the work.

## Caller-event gate conventions

### Context

The component bodies are reusable workflows, and a reusable workflow
inherits the caller's event: a job inside a called workflow reads
`github.event_name` from the caller's run. Consequences follow for
every condition a body writes. A component leg that did not account
for inheritance never ran; conversely, a generous condition could let
a pull-request run reach a cloud-mutating job by riding the reusable
path. The local workstation loop runs these same files through the
local runner (`act`), which does not emulate push publishes and has
historically reported exit 0 on failed local runs, so local conditions
need their own marker.

### Decision

Every cloud-mutating job carries one trusted-gate condition:

```yaml
if: github.event_name != 'pull_request' && github.ref == 'refs/heads/main'
```

ANDed with the component-selection outputs that say the job's own
component changed. Every mutating job additionally requires
`!github.event.act`, so the local runner can never fire a mutation
path on a run whose bytes it did not build. Pull-request runs stay
credential-free — the workflow-level default is `contents: read` and
only named jobs elevate — and a manual dispatch of the release entry
point is restricted to `refs/heads/main`.

### Consequences

- Authority follows the condition, not the file: no pull request can
  reach a cloud-mutating job regardless of which workflow calls what.
- The local loop stops where its credentials end, and the act marker
  makes a local run's silence or success untrustworthy for promotion
  decisions.
- The cost is repetition: the gate appears on every mutating job, and
  special events (manual dispatch, the weekly schedule) add conjuncts
  to the same expressions.

## The conversation as an accessible feed

### Context

The SPA lints under the recommended ruleset with a zero-findings
mandate and no escape hatches: no ruleset override, no CI tolerance,
no inline suppressions. Under that mandate an earlier zero-findings fix
had removed the workspace's keyboard focusability instead of fixing
semantics, and the one remaining finding proved irreconcilable within
the authorized set: every honest focusable-region variant tripped the
same rule, and that rule's option schema admits no configurable
exceptions. Removing behavior was the one forbidden route — accessibility
fixes may restructure, never remove.

### Decision

The conversation is one feed following the WAI-ARIA Authoring
Practices feed pattern. A `role="feed"` container holds three
`role="article"` entries — the question, the answer and the escalation
call — each carrying `aria-posinset` and `aria-setsize` and a negative
tabindex for roving focus. Arrow, PageUp/PageDown, Home and End step
between articles while focus is inside the feed; when an answer
arrives, the application moves focus to it; Tab leaves the feed at the
next focusable element outside it. Authentication errors and alerts
render outside the feed so an alert is never read as a feed article.
The rework landed in commit `c356f44` together with the local gate
alignment (`scripts/local-run`) that makes CI's lint byte-exact
locally, so the same findings surface before a push rather than on it.

### Consequences

- The finding was satisfied by fixing semantics — real feed semantics —
  rather than by suppressing a rule, tolerating the finding in CI, or
  removing behavior.
- The feed pattern is the sanctioned recipe for future scrollable
  conversation surfaces, with the focus-management helpers ready in
  `App.tsx`.
- The cost is mechanical: more markup and handlers, an adapted test
  suite, and the discipline that lint fixes may only restructure —
  accessibility behavior is never the thing removed.
