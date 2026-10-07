#!/bin/sh
# Hermetic interleaving regression over the real YAML: the three
# wrappers hold the one shared-target-demo lease throughout; every
# body below is lease-free, and a release holder locks standalone
# runs out, promotion included.
set -eu

cd "$(git rev-parse --show-toplevel)"

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

workflows=.github/workflows

for name in release.yml ingest.yml infra.yml \
    release-candidate.yml ingest-candidate.yml infra-candidate.yml; do
    [ -f "$workflows/$name" ] || fail "$workflows/$name is missing"
done

if python3 -c 'import yaml' >/dev/null 2>&1; then
    python3 - "$workflows" <<'PY' || exit 1
import re
import sys
from pathlib import Path

import yaml

workflows = Path(sys.argv[1])
GROUP = "shared-target-demo"
WRAPPERS = ("release.yml", "ingest.yml", "infra.yml")
BODIES = {
    "release.yml": "release-candidate.yml",
    "ingest.yml": "ingest-candidate.yml",
    "infra.yml": "infra-candidate.yml",
}
EXPECTED_TRIGGERS = {
    "release.yml": {"push", "workflow_dispatch"},
    "ingest.yml": {"pull_request", "schedule", "workflow_dispatch"},
    "infra.yml": {"pull_request", "workflow_dispatch"},
}

failures = []


def check(ok, message):
    if not ok:
        failures.append(message)


def load(name):
    return yaml.safe_load((workflows / name).read_text())


def trigger_names(doc):
    on = doc.get(True) if True in doc else (doc.get("on") or {})
    return {k for k in on if k != "workflow_call"}


def local_uses(doc):
    names = set()
    for job in (doc.get("jobs") or {}).values():
        uses = job.get("uses") if isinstance(job, dict) else None
        if isinstance(uses, str) and uses.startswith("./.github/workflows/"):
            names.add(uses.rsplit("/", 1)[-1])
    return names


def wrapper_lock(name, text=None):
    doc = yaml.safe_load(text) if text is not None else load(name)
    conc = doc.get("concurrency") or {}
    return conc.get("group"), conc.get("cancel-in-progress")


# 1. Wrapper shape: original triggers only, one job, the shared-target
# lease with cancel-in-progress false, calling its reusable body with
# inherited secrets.
for name in WRAPPERS:
    doc = load(name)
    check(
        trigger_names(doc) == EXPECTED_TRIGGERS[name],
        f"{name}: triggers drifted to {sorted(trigger_names(doc))}",
    )
    group, cip = wrapper_lock(name)
    check(
        group == GROUP and cip is False,
        f"{name}: does not hold {GROUP} with cancel-in-progress false",
    )
    jobs = doc.get("jobs") or {}
    check(
        sorted(jobs) == [name.split(".")[0]],
        f"{name}: is not a single-job entrypoint: {sorted(jobs)}",
    )
    job = next(iter(jobs.values()), {})
    check(
        str(job.get("uses", "")).endswith("/" + BODIES[name]),
        f"{name}: does not call {BODIES[name]}",
    )
    check(
        job.get("secrets") == "inherit",
        f"{name}: the body call does not inherit secrets",
    )
    print(f"wrapper {name}: triggers={sorted(trigger_names(doc))} lease={group} cip={cip}")

# 2. Body shape: reusable-only and lease-free at workflow level, so a
# called workflow never acquires a lease its entrypoint already holds
# and no semantic about callee concurrency is depended on.
for name in WRAPPERS:
    body = load(BODIES[name])
    check(
        trigger_names(body) == set(),
        f"{BODIES[name]}: is not reusable-only: {sorted(trigger_names(body))}",
    )
    check(
        "concurrency" not in body,
        f"{BODIES[name]}: declares a workflow-level concurrency block",
    )
    print(f"body {BODIES[name]}: reusable-only, lease-free")

# 3. Closure: no descendant below a wrapper carries a
# workflow-level lease (nested acquisition, same-run cycle), no job
# carries the shared group, and the release body calls the ingest and
# infra bodies directly, never a locked entrypoint.
closure = set()
frontier = list(WRAPPERS)
while frontier:
    name = frontier.pop()
    if name in closure:
        continue
    closure.add(name)
    frontier.extend(sorted(local_uses(load(name))))
for name in sorted(closure - set(WRAPPERS)):
    doc = load(name)
    check(
        "concurrency" not in doc,
        f"{name}: descendant of a locked wrapper carries a workflow-level lease",
    )
    for job_name, job in (doc.get("jobs") or {}).items():
        job_conc = job.get("concurrency") or {} if isinstance(job, dict) else {}
        check(
            job_conc.get("group") != GROUP,
            f"{name}: job {job_name} holds the shared-target group, which "
            "would cycle a run waiting on itself",
        )
check(
    not local_uses(load("release-candidate.yml")) & set(WRAPPERS),
    "the release body calls a locked entrypoint instead of a body",
)
check(
    {"ingest-candidate.yml", "infra-candidate.yml"}
    <= local_uses(load("release-candidate.yml")),
    "the release body does not call the ingest and infra bodies directly",
)
print(f"closure: {len(closure)} workflows, descendants lease-free, no same-run cycle")

# The app workflow keeps no standalone trusted mutation trigger; the
# Power Platform workflow is excluded from this serialization by ruling.
check(
    trigger_names(load("app.yml")) <= {"pull_request"},
    "the app workflow gained a standalone trusted mutation trigger",
)
check(
    GROUP not in (workflows / "power-platform.yml").read_text(),
    "the excluded Power Platform workflow carries the shared group",
)

# 4. The holder's locked window from the release body's job DAG:
# topological levels from needs. The deepest level is the promotion
# legs under the protected demo environment — approval wait, pre-apply
# recheck and mutation sit in the lease.
body = load("release-candidate.yml")
jobs = body.get("jobs") or {}
check(
    {"release-eval", "release-summary"} <= set(jobs),
    "the release body lost the evaluation or summary stage",
)
levels = {}


def level_of(name):
    if name not in levels:
        needs = jobs[name].get("needs") or []
        needs = [needs] if isinstance(needs, str) else list(needs)
        levels[name] = 1 + max((level_of(n) for n in needs if n in jobs), default=-1)
    return levels[name]


for name in jobs:
    level_of(name)
deepest = max(levels.values())
stages = sorted(name for name, lvl in levels.items() if lvl == deepest)
check(
    {"deploy-production", "infra-apply", "ingest-production", "pp-production"}
    <= set(stages),
    f"the deepest stage is not the promotion legs: {stages}",
)
check(
    levels["release-eval"] < levels["release-summary"] < deepest,
    "evaluation and the summary do not precede promotion",
)
for name in stages:
    env = jobs[name].get("environment")
    env_name = env.get("name") if isinstance(env, dict) else env
    check(
        env_name == "demo",
        f"promotion job {name} does not run under the protected demo environment",
    )
print(f"holder window: {deepest + 1} derived stages, promotion = {stages}")

# 5. The interleaving itself, under documented semantics only: cip
# false makes an arrival wait while a holder is active; removing the
# group makes entrants overlap; flipping the flag makes an arrival
# cancel the holder instead.
scenarios = 0


def outcome(holder_group, holder_cip, entrant_group, entrant_cip):
    if entrant_group != GROUP or holder_group != GROUP:
        return "overlap-enter"
    if holder_cip or entrant_cip:
        return "holder-cancelled"
    return "wait"


holder_group, holder_cip = wrapper_lock("release.yml")
for lvl in range(deepest + 1):
    stage = "validate/staging/eval/summary/approval/promotion ladder step"
    for entrant in ("ingest.yml", "infra.yml"):
        entrant_group, entrant_cip = wrapper_lock(entrant)
        verdict = outcome(holder_group, holder_cip, entrant_group, entrant_cip)
        check(
            verdict == "wait",
            f"while the release holds stage {lvl}, standalone {entrant} "
            f"was {verdict} instead of waiting",
        )
        print(f"scenario stage {lvl} {entrant}: expected=wait actual={verdict}")
        scenarios += 1

# After the holder completes, the queued weekly fill enters and a
# second manual ingest and a manual infra plan each wait in turn.
entrant_group, entrant_cip = wrapper_lock("ingest.yml")
verdict = outcome(holder_group, holder_cip, entrant_group, entrant_cip)
check(
    verdict == "wait",
    "without an active holder the standalone ingest cannot even enter",
)
verdict = outcome(GROUP, False, entrant_group, entrant_cip)
check(
    verdict == "wait",
    "a second manual ingest overlapped the running weekly fill",
)
print("scenario post-release queue: weekly fill enters, duplicates wait")
scenarios += 1

# 6. Mutation discrimination: every baseline wait must flip when the
# group is removed (overlap) or the flag flipped (cancellation), or the
# checker above would be vacuous.
def strip_concurrency(text):
    kept, skipping = [], False
    for line in text.splitlines(True):
        if re.match(r"^concurrency:", line):
            skipping = True
            continue
        if skipping:
            if line[:1] in (" ", "\t"):
                continue
            skipping = False
        kept.append(line)
    return "".join(kept)


mutations = 0
for name in WRAPPERS:
    text = (workflows / name).read_text()
    if "cancel-in-progress: false" not in text:
        failures.append(f"{name}: cancel-in-progress: false pin missing")
        continue
    for label, mutated in (
        ("group-removed", strip_concurrency(text)),
        ("cancel-flipped", text.replace(
            "cancel-in-progress: false", "cancel-in-progress: true")),
    ):
        m_group, m_cip = wrapper_lock(name, mutated)
        if name == "release.yml":
            verdict = outcome(m_group, m_cip, *wrapper_lock("ingest.yml"))
        else:
            verdict = outcome(holder_group, holder_cip, m_group, m_cip)
        check(
            verdict != "wait",
            f"mutating {name} ({label}) was not detected: the checker "
            f"still reports serialization ({verdict})",
        )
        print(f"mutation {name} {label}: expected=flip actual={verdict}")
        mutations += 1

print(f"shared-target interleaving: {scenarios} arrival verdicts, "
      f"{mutations} mutation controls, {len(closure)}-workflow closure")

if failures:
    for message in failures:
        print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)
print("ok: a release holder locks every standalone ingest and infra "
      "entry out of the shared target until promotion completes, and "
      "every wrapper mutation flips the verdict")
PY
else
    # PyYAML is unavailable, so the interleaving derivation and the
    # mutation controls cannot run. A degraded textual verdict must
    # never stand in for them: a run whose validator could not execute
    # is a failure, not a pass with commentary. Skip loudly and let the
    # caller decide whether a skip is acceptable in this environment.
    echo "skip: PyYAML is unavailable; the shared-target interleaving gates cannot run" >&2
    exit 77
fi
