#!/bin/sh
# Release-candidate wiring regression over the real YAML: GitHub drops
# an undeclared dispatch input at the door, and a workflow_call input
# is settable only through the caller's with: block, so the flag can
# reach the reusable body only when infra.yml declares it AND forwards
# it. Every mutation below flips the verdict, so a passing run cannot
# be vacuous.
set -eu

cd "$(git rev-parse --show-toplevel)"

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

workflows=.github/workflows

for name in infra.yml infra-candidate.yml; do
    [ -f "$workflows/$name" ] || fail "$workflows/$name is missing"
done

if python3 -c 'import yaml' >/dev/null 2>&1; then
    python3 - "$workflows" <<'PY' || exit 1
import re
import sys
from pathlib import Path

import yaml

workflows = Path(sys.argv[1])
FORWARD = "${{ inputs.release_candidate }}"

failures = []


def check(ok, message):
    if not ok:
        failures.append(message)


def on_block(doc):
    return doc.get(True) if True in doc else (doc.get("on") or {})


def run_checks(infra_text, candidate_text):
    """Return the failure list for one (caller, callee) text pair."""
    infra = yaml.safe_load(infra_text)
    candidate = yaml.safe_load(candidate_text)
    problems = []

    def check(ok, message):
        if not ok:
            problems.append(message)

    # 1. The dispatch trigger declares the flag: boolean, default
    # false. A bare workflow_dispatch: makes GitHub silently drop the
    # flag before the workflow context ever sees it.
    dispatch = on_block(infra).get("workflow_dispatch")
    inputs = dispatch.get("inputs") if isinstance(dispatch, dict) else None
    flag = inputs.get("release_candidate") if isinstance(inputs, dict) else None
    check(isinstance(flag, dict),
          "infra.yml: workflow_dispatch declares no release_candidate "
          "input (bare trigger drops the flag at the door)")
    if isinstance(flag, dict):
        check(flag.get("type") == "boolean",
              f"infra.yml: release_candidate type is {flag.get('type')!r}, "
              "not boolean")
        check(flag.get("default") is False,
              f"infra.yml: release_candidate default is "
              f"{flag.get('default')!r}, not false")

    # 2. The single job forwards the flag: a workflow_call input is
    # settable only by the caller's with: block, and infra-candidate.yml
    # has no event triggers of its own.
    job = (infra.get("jobs") or {}).get("infra")
    check(isinstance(job, dict), "infra.yml: the infra job is missing")
    with_block = job.get("with") if isinstance(job, dict) else None
    check(isinstance(with_block, dict),
          "infra.yml: the infra job forwards no with: block to "
          "infra-candidate.yml")
    forwarded = with_block.get("release_candidate") if isinstance(with_block, dict) else None
    check(forwarded == FORWARD,
          f"infra.yml: jobs.infra.with.release_candidate is "
          f"{forwarded!r}, not the inputs context forwarding")

    # 3. The callee still declares the input being forwarded to: a
    # with: key the body does not declare is dropped as silently as an
    # undeclared dispatch input.
    call = on_block(candidate).get("workflow_call")
    callee = call.get("inputs", {}).get("release_candidate") if isinstance(call, dict) else None
    check(isinstance(callee, dict),
          "infra-candidate.yml: workflow_call no longer declares "
          "release_candidate")
    if isinstance(callee, dict):
        check(callee.get("type") == "boolean" and callee.get("default") is False,
              "infra-candidate.yml: release_candidate is no longer "
              "boolean default false")

    return problems


def drop_block_under(pattern, indent):
    """Keep the header line matching pattern, drop its more-indented body."""
    def mutate(text):
        kept, skipping = [], False
        for line in text.splitlines(True):
            if skipping:
                if line.strip() == "" or re.match(indent, line):
                    continue
                skipping = False
            if re.match(pattern, line):
                kept.append(line)
                skipping = True
                continue
            kept.append(line)
        return "".join(kept)
    return mutate


def drop_with_block(text):
    return text.replace(
        "    with:\n      release_candidate: ${{ inputs.release_candidate }}\n",
        "")


def mistype_dispatch_input(text):
    return text.replace("        type: boolean\n", "        type: string\n")


drop_dispatch_inputs = drop_block_under(r"^  workflow_dispatch:\s*$", r"^    ")
drop_callee_input = drop_block_under(r"^      release_candidate:\s*$", r"^        ")

infra_text = (workflows / "infra.yml").read_text()
candidate_text = (workflows / "infra-candidate.yml").read_text()

# Mutation anchors: each control below is a textual edit of the real
# files, so its literal must still be present or the control is a
# silent no-op.
for name, text, pin in (
    ("infra.yml", infra_text,
     "    with:\n      release_candidate: ${{ inputs.release_candidate }}\n"),
    ("infra.yml", infra_text, "        type: boolean\n"),
    ("infra.yml", infra_text, "  workflow_dispatch:\n"),
    ("infra-candidate.yml", candidate_text, "      release_candidate:\n"),
):
    check(pin in text, f"{name}: mutation anchor missing: {pin.strip()}")

for message in run_checks(infra_text, candidate_text):
    failures.append(message)

infra_doc = yaml.safe_load(infra_text)
dispatch = on_block(infra_doc).get("workflow_dispatch")
inputs = dispatch.get("inputs") if isinstance(dispatch, dict) else None
flag = inputs.get("release_candidate") if isinstance(inputs, dict) else None
if isinstance(flag, dict):
    print(f"dispatch input: release_candidate type={flag.get('type')} "
          f"default={flag.get('default')}")
else:
    print("dispatch input: release_candidate MISSING")
job = (infra_doc.get("jobs") or {}).get("infra") or {}
forwarded = (job.get("with") or {}).get("release_candidate")
print(f"forwarding: jobs.infra.with.release_candidate = {forwarded}")
call = on_block(yaml.safe_load(candidate_text)).get("workflow_call")
callee_flag = (call.get("inputs", {}) if isinstance(call, dict) else {}).get(
    "release_candidate")
callee_ok = isinstance(callee_flag, dict) and callee_flag.get("type") == "boolean"
print(f"callee: infra-candidate.yml workflow_call declares release_candidate "
      f"= {callee_ok}")

# Mutation discrimination: each wiring regression must flip the verdict.
mutations = 0
for label, infra_mut, cand_mut, expected in (
    ("with-block-removed", drop_with_block(infra_text), candidate_text,
     "forwards no with"),
    ("dispatch-input-undeleted", drop_dispatch_inputs(infra_text),
     candidate_text, "declares no release_candidate"),
    ("dispatch-input-mistyped", mistype_dispatch_input(infra_text),
     candidate_text, "type is"),
    ("callee-input-undeleted", infra_text, drop_callee_input(candidate_text),
     "no longer declares"),
):
    problems = run_checks(infra_mut, cand_mut)
    detected = any(expected in message for message in problems)
    check(problems != [] and detected,
          f"mutating {label} was not detected: the checker still passes "
          "or misses the site")
    print(f"mutation {label}: expected=flip detected={detected}")
    mutations += 1

print(f"release-candidate wiring: dispatch input declared and forwarded, "
      f"{mutations} mutation controls")

if failures:
    for message in failures:
        print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)
print("ok: a dispatch can carry release_candidate through infra.yml into "
      "infra-candidate.yml, and every wiring mutation flips the verdict")
PY
else
    # PyYAML is unavailable, so neither the wiring derivation nor the
    # mutation controls can run. A degraded textual verdict must never
    # stand in for them: a run whose validator could not execute is a
    # failure, not a pass with commentary. Skip loudly and let the
    # caller decide whether a skip is acceptable in this environment.
    echo "skip: PyYAML is unavailable; the release-candidate wiring gates cannot run" >&2
    exit 77
fi
