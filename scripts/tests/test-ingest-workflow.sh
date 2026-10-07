#!/bin/sh
# Structural assertions on the Ingest workflow: the thin locked wrapper
# and the reusable candidate body it calls.
set -eu

cd "$(git rev-parse --show-toplevel)"
workflow=.github/workflows/ingest.yml
body=.github/workflows/ingest-candidate.yml
infra_body=.github/workflows/infra-candidate.yml

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

[ -f "$workflow" ] || fail "$workflow is missing"
[ -f "$body" ] || fail "$body is missing"
[ -f "$infra_body" ] || fail "$infra_body is missing"

if python3 -c 'import yaml' >/dev/null 2>&1; then
    python3 - "$workflow" "$body" "$infra_body" <<'PY' || exit 1
import re
import sys

import yaml

with open(sys.argv[1]) as fh:
    doc = yaml.safe_load(fh)
with open(sys.argv[2]) as fh:
    body_doc = yaml.safe_load(fh)

failures = []


def check(ok, message):
    if not ok:
        failures.append(message)


# The entrypoint wrapper owns the one shared-target lease for every
# trigger it declares, so a pull request, the weekly fill and a manual
# run all queue behind an in-flight release instead of overlapping it.
concurrency = doc.get("concurrency")
check(isinstance(concurrency, dict), "no wrapper workflow-level concurrency block")
if isinstance(concurrency, dict):
    check(
        concurrency.get("group") == "shared-target-demo",
        "wrapper concurrency group is not the shared target "
        f"(got {concurrency.get('group')!r})",
    )
    check(
        concurrency.get("cancel-in-progress") is False,
        "wrapper cancel-in-progress is not explicitly false; a cancelled "
        "mid-ingest run can leave an index half converged",
    )

wrapper_on = doc.get("on", doc.get(True))
check(
    set(wrapper_on) == {"pull_request", "schedule", "workflow_dispatch"},
    f"wrapper triggers drifted: {sorted(wrapper_on)}",
)
wrapper_jobs = doc.get("jobs") or {}
check(
    sorted(wrapper_jobs) == ["ingest"],
    f"the wrapper is not a single-job entrypoint: {sorted(wrapper_jobs)}",
)
wrapper_job = wrapper_jobs.get("ingest") or {}
check(
    str(wrapper_job.get("uses", "")).endswith("/ingest-candidate.yml"),
    "the wrapper does not call the ingest candidate body",
)
check(
    wrapper_job.get("secrets") == "inherit",
    "the wrapper call does not inherit secrets",
)

# The candidate body carries no workflow-level concurrency: a called
# workflow must never acquire a lease the entrypoint already holds,
# and neither ignoring nor honoring a nested group is a documented
# semantic this design may depend on.
check(
    "concurrency" not in body_doc,
    "the candidate body still declares a workflow-level concurrency block",
)
on = body_doc.get("on", body_doc.get(True))
check(
    set(on) == {"workflow_call"},
    f"the candidate body is not reusable-only: {sorted(on)}",
)
check(
    on["workflow_call"]["inputs"]["release_candidate"]["default"] is False,
    "release candidate defaults on",
)
outputs = on["workflow_call"].get("outputs") or {}
for name in ("kb_commit", "index_name", "doc_count"):
    check(name in outputs, f"the candidate body lost the {name} output")
check(
    (body_doc.get("jobs", {}).get("push-staging") or {})
    .get("concurrency", {})
    .get("group")
    == "ingest-staging",
    "the staging job lost its own component lock",
)

jobs = body_doc.get("jobs") or {}
for name in ("validate-kb", "push-staging", "staging-check", "push"):
    check(name in jobs, f"job '{name}' is missing")

push = jobs.get("push") or {}
needs = push.get("needs") or []
if isinstance(needs, str):
    needs = [needs]
check(
    "push-staging" in needs,
    "the prod 'push' job does not need the staging job",
)
check(
    "validate-kb" in needs,
    "the prod 'push' job does not need the validate-kb job",
)

staging = jobs.get("push-staging") or {}
staging_needs = staging.get("needs") or []
if isinstance(staging_needs, str):
    staging_needs = [staging_needs]
check(
    "validate-kb" in staging_needs,
    "the staging job does not need the validate-kb job",
)

staging_check = jobs.get("staging-check") or {}
sc_needs = staging_check.get("needs") or []
if isinstance(sc_needs, str):
    sc_needs = [sc_needs]
check(
    "push-staging" in sc_needs,
    "staging-check does not need the staging push, so the release "
    "retrieval check could report on an index nothing filled",
)

for name in ("push-staging", "staging-check", "push"):
    job = jobs.get(name) or {}
    check(
        job.get("continue-on-error") is not True,
        f"the {name} job carries continue-on-error, which would let a "
        "failed job report success to everything that needs it",
    )

# A status function in the prod condition could run production past a
# failed staging job; the skip case is handled below by evaluating the
# event gates.
prod_if = push.get("if")
check(prod_if is not None, "the prod 'push' job has no explicit gate")
prod_if = str(prod_if) if prod_if is not None else ""
for forbidden in ("always(", "failure(", "cancelled("):
    check(
        forbidden not in prod_if,
        f"the prod gate contains {forbidden!r}, which can run past a "
        "failed staging job",
    )

# The negated act term is what keeps a local act run out of the
# production environment. The event matrix below cannot see its
# absence, because staging runs in exactly the contexts where an
# unguarded prod would.
check(
    re.search(r"!\s*github\.event\.act\b", prod_if) is not None,
    "the prod gate carries no negated github.event.act term, so a "
    "local act run could reach production",
)

staging_if = (jobs.get("push-staging") or {}).get("if")
staging_if = str(staging_if) if staging_if is not None else None


def evaluate(expr, event, act, main=True, candidate=False):
    """Evaluate the event-gate subset of GitHub expressions.

    The file's conditions use only github.event_name string comparison,
    the boolean github.event.act, and !, &&, ||, ==, !=.
    """
    body = expr.strip()
    if body.startswith("${{") and body.endswith("}}"):
        body = body[3:-2].strip()
    body = body.replace("!=", "\x00")
    body = body.replace("&&", " and ").replace("||", " or ")
    body = body.replace("!", " not ").replace("==", " == ")
    body = body.replace("\x00", "!=")
    body = body.replace("github.event_name", repr(event))
    body = body.replace("github.event.act", str(act))
    body = body.replace("github.ref", repr("refs/heads/main" if main else "refs/heads/other"))
    body = body.replace("inputs.release_candidate", str(candidate))
    for bad in ("import", "__", "lambda", "open(", "eval", "exec"):
        if bad in body:
            raise SystemExit(f"unexpected construct in condition: {expr!r}")
    # eval is safe as used here: builtins are stripped and the dangerous
    # constructs were rejected above, so only a plain boolean expression
    # reaches it.
    return bool(eval(body, {"__builtins__": {}}, {}))


for event in ("push", "pull_request", "schedule", "workflow_dispatch"):
    for act in (False, True):
        staging_runs = (
            True if staging_if is None else evaluate(staging_if, event, act)
        )
        prod_runs = evaluate(prod_if, event, act)
        check(
            not (prod_runs and not staging_runs),
            f"in event context (event={event}, act={act}) production "
            "would run while the staging job is skipped",
        )

# Reusable calls retain their caller event; the input selects the cloud leg.
for workflow_name, candidate_file in (
    ("ingest", sys.argv[2]),
    ("infra", sys.argv[3]),
):
    with open(candidate_file) as fh:
        workflow_doc = yaml.safe_load(fh)
    on = workflow_doc.get("on", workflow_doc.get(True))
    check(on["workflow_call"]["inputs"]["release_candidate"]["default"] is False,
          f"{workflow_name}: release candidate defaults on")
    check(
        set(on) == {"workflow_call"},
        f"{workflow_name}: candidate body is not reusable-only",
    )
    check(
        "concurrency" not in workflow_doc,
        f"{workflow_name}: candidate body declares workflow-level concurrency",
    )
    for event in ("push", "schedule", "workflow_dispatch", "pull_request"):
        for main in (False, True):
            for candidate in (False, True):
                for act in (False, True):
                    leg = "push-staging" if workflow_name == "ingest" else "terraform-staging"
                    expr = str(workflow_doc["jobs"][leg]["if"])
                    actual = evaluate(expr, event, act, main, candidate)
                    direct = event in (("schedule", "workflow_dispatch") if workflow_name == "ingest" else ("workflow_dispatch",))
                    expected = event != "pull_request" and main and (candidate or direct)
                    check(actual == expected, f"matrix {workflow_name}/{event}/main={main}/selected={candidate}/act={act}: expected={expected} actual={actual}")
                    print(f"matrix {workflow_name}/{event}/main={main}/selected={candidate}/act={act}: expected={expected} actual={actual}")
                    if workflow_name == "ingest":
                        actual = evaluate(prod_if, event, act, main, candidate)
                        expected = event == "schedule" and main and not act
                        check(actual == expected, "scheduled production gate mismatch")
check("scheduled-eval" in needs, "production lacks scheduled quality dependency")
check(jobs["scheduled-eval"]["needs"] == "staging-check", "scheduled quality lacks retrieval dependency")
for job_name in ("push-staging", "push"):
    steps = jobs[job_name]["steps"]
    check(any("setup-uv" in str(step.get("uses", "")) for step in steps), "fresh ingestion job lacks uv")
    check(any("uv sync --locked" in str(step.get("run", "")) for step in steps), "fresh ingestion job lacks locked sync")
    check(any('--ref "$GITHUB_SHA"' in str(step.get("run", "")) for step in steps), "mutable KB citation ref")

# A step cannot make the job run, but no step in the prod job may carry
# a status function that would ignore the job's own gating either.
for step in push.get("steps") or []:
    step_if = step.get("if")
    if step_if is None:
        continue
    for forbidden in ("always(", "failure(", "cancelled("):
        check(
            forbidden not in str(step_if),
            f"a step of the prod job carries {forbidden!r}",
        )

if failures:
    for message in failures:
        print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)
print("ok: the locked wrapper serializes standalone ingestion against a "
      "release, the candidate body is lease-free, staging-check verifies "
      "the retrieval, and production stays behind a successful "
      "staging push")
PY
else
    # PyYAML is unavailable, so the structural gates cannot run. A
    # degraded textual verdict must never stand in for them: a run
    # whose validator could not execute is a failure, not a pass with
    # commentary. Skip loudly and let the caller decide whether a skip
    # is acceptable in this environment.
    echo "skip: PyYAML is unavailable; the ingest workflow structural gates cannot run" >&2
    exit 77
fi
