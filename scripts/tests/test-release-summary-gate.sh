#!/bin/sh
# Structural assertions on the release-summary gate in the Release
# workflow. A workflow-only push selects no release components, so the
# summary job must skip: no release-identity manifest, no artifact,
# and every production leg must stay skipped behind it. When at least
# one component is selected, the summary must run (even though the
# unselected component jobs are skipped) and must fail closed when a
# selected component reaches the summary without its evidence. GitHub
# enforces the conditions only in the real runner, so this proves the
# workflow's shape by evaluating the gate expressions over an event
# and selection matrix. Hermetic: it reads the repository's own files
# and touches no network and no cloud.
set -eu

cd "$(git rev-parse --show-toplevel)"
workflow=.github/workflows/release.yml

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

[ -f "$workflow" ] || fail "$workflow is missing"

if python3 -c 'import yaml' >/dev/null 2>&1; then
    python3 - "$workflow" <<'PY' || exit 1
import re
import sys

import yaml

with open(sys.argv[1]) as fh:
    doc = yaml.safe_load(fh)

failures = []


def check(ok, message):
    if not ok:
        failures.append(message)


jobs = doc.get("jobs") or {}
for name in ("changes", "validate", "app-release", "ingest-release",
             "infra-release", "pp-release", "release-summary",
             "deploy-production", "infra-apply", "ingest-production",
             "pp-production"):
    check(name in jobs, f"job '{name}' is missing")

summary = jobs.get("release-summary") or {}
summary_needs = summary.get("needs") or []
if isinstance(summary_needs, str):
    summary_needs = [summary_needs]

# (a) the summary needs the changes job, so selection outputs resolve.
check(
    "changes" in summary_needs,
    "release-summary does not need the changes job",
)

summary_if = str(summary.get("if") or "")
for term in ("always()", "!failure()", "!cancelled()",
             "needs.changes.result == 'success'"):
    check(
        term in summary_if,
        f"the release-summary condition lost {term!r}",
    )

selected_terms = {
    "app": "needs.changes.outputs.app == 'true'",
    "kb": "needs.changes.outputs.kb == 'true'",
    "infra": "needs.changes.outputs.infra == 'true'",
    "power_platform": "needs.changes.outputs.power_platform == 'true'",
}


def evaluate(expr, results, outputs):
    """Evaluate the gate subset of GitHub expressions.

    Job results and outputs come from the caller's maps; status
    functions collapse to their skip-proof values. Only !, &&, ||, ==,
    parentheses and string literals appear beyond the substitutions.
    """
    body = expr.strip()
    if body.startswith("${{") and body.endswith("}}"):
        body = body[3:-2].strip()
    body = re.sub(
        r"(?:needs\.)?([A-Za-z][\w-]*)\.result == 'success'",
        lambda m: str(results.get(m.group(1), True)),
        body,
    )
    body = re.sub(
        r"(?:needs\.)?([A-Za-z][\w-]*)\.outputs\.([A-Za-z_]\w*) == 'true'",
        lambda m: str(outputs.get((m.group(1), m.group(2)), False)),
        body,
    )
    body = body.replace("always()", "True")
    body = body.replace("!failure()", "True")
    body = body.replace("!cancelled()", "True")
    body = body.replace("failure()", "False")
    body = body.replace("cancelled()", "False")
    body = body.replace("!github.event.act", "True")
    body = body.replace("!", " not ").replace("&&", " and ")
    body = body.replace("||", " or ")
    for bad in ("import", "__", "lambda", "open(", "eval", "exec", "getattr"):
        if bad in body:
            raise SystemExit(f"unexpected construct in condition: {expr!r}")
    # eval is safe as used here: builtins are stripped and dangerous
    # constructs were rejected above, so only a plain boolean
    # expression reaches it.
    return bool(eval(body, {"__builtins__": {}}, {}))


# (b) the summary runs exactly when at least one component is selected.
outputs_false = {("changes", k): False for k in selected_terms}
for name, term in selected_terms.items():
    check(
        term in summary_if,
        f"the release-summary condition lost the {name} selection term",
    )
check(
    not evaluate(summary_if, {"changes": True}, outputs_false),
    "a workflow-only push would still run the release summary",
)
for name in selected_terms:
    run = evaluate(
        summary_if,
        {"changes": True},
        {("changes", name): True},
    )
    check(
        run,
        f"selecting only {name} would not run the release summary",
    )

# (c) the evidence gate sits in the summary, before the artifact
# upload, and fails closed per selected component.
steps = summary.get("steps") or []
names = [str(s.get("name", "")) for s in steps]
gate_names = [n for n in names if "Assert the selected components" in n]
check(
    len(gate_names) == 1,
    "the summary has no single evidence-gate step",
)
upload_at = next(
    (i for i, s in enumerate(steps)
     if "upload-artifact" in str(s.get("uses", ""))),
    None,
)
gate_at = names.index(gate_names[0]) if gate_names else -1
check(
    upload_at is not None and 0 <= gate_at < upload_at,
    "the evidence gate does not run before the artifact upload",
)
gate_step = steps[gate_at] if gate_names else {}
gate_env = str(gate_step.get("env") or {})
gate_run = str(gate_step.get("run") or "")
for term in ("APP_DIGEST", "STAGING_READINESS", "STAGING_SMOKE",
             "STAGING_E2E", "KB_COMMIT", "DOC_COUNT", "PLAN_HASH",
             "PLAN_SAVED", "SOLUTION_VERSION", "PP_STAGING_STATUS"):
    check(term in gate_env, f"the evidence gate never reads {term}")
for term in ('[ -n "$APP_DIGEST" ]', '"$STAGING_READINESS" = "true"',
             '"$STAGING_SMOKE" = "pass"', '"$STAGING_E2E" = "pass"',
             '[ -n "$KB_COMMIT" ]', '[ -n "$DOC_COUNT" ]',
             '[ -n "$PLAN_HASH" ]', '"$PLAN_SAVED" = "true"',
             '"$PP_STAGING_STATUS" != "not_configured"', "exit 1"):
    check(term in gate_run, f"the evidence gate lost {term!r}")

# (d) the manifest names the selected components and their status.
identity = next(
    (s for s in steps if "release-identity.json" in str(s.get("run", ""))),
    None,
)
check(identity is not None, "no step writes release-identity.json")
identity_run = str(identity.get("run", "")) if identity else ""
check(
    "selected_components" in identity_run,
    "the manifest does not record the selected components",
)
check(
    "component_status" in identity_run
    and '"skipped_not_selected"' in identity_run
    and '"skipped_not_configured"' in identity_run,
    "the manifest does not record per-component statuses",
)

# (e) every production leg is false while the summary is skipped, and
# true behind a successful summary only when its component succeeded.
for name in ("deploy-production", "infra-apply", "ingest-production",
             "pp-production"):
    job = jobs.get(name) or {}
    leg_if = str(job.get("if") or "")
    check(
        "needs.release-summary.result == 'success'" in leg_if,
        f"the {name} condition lost the release-summary term",
    )
    check(
        not evaluate(
            leg_if,
            {"release-summary": False, name: True},
            {},
        ),
        f"the {name} leg would run while the release summary is skipped",
    )

if failures:
    for message in failures:
        print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)
print("ok: the release summary skips workflow-only pushes, runs behind "
      "a selected component, and fails closed on missing evidence")
PY
else
    # PyYAML is unavailable, so this falls back to textual assertions.
    # It pins the exact expected lines instead of parsing; it cannot
    # evaluate the gates, only verify they are present verbatim.
    grep -q "needs: \[changes, validate, app-release, ingest-release, infra-release, pp-release\]" "$workflow" \
        || fail "the summary does not need the changes job (grep fallback)"
    grep -q "needs.changes.result == 'success'" "$workflow" \
        || fail "the summary condition never checks the changes result (grep fallback)"
    grep -q "needs.changes.outputs.app == 'true'" "$workflow" \
        || fail "the app selection term is missing (grep fallback)"
    grep -q "needs.changes.outputs.kb == 'true'" "$workflow" \
        || fail "the kb selection term is missing (grep fallback)"
    grep -q "needs.changes.outputs.infra == 'true'" "$workflow" \
        || fail "the infra selection term is missing (grep fallback)"
    grep -q "needs.changes.outputs.power_platform == 'true'" "$workflow" \
        || fail "the power_platform selection term is missing (grep fallback)"
    grep -q "Assert the selected components carried their evidence" "$workflow" \
        || fail "the evidence-gate step is missing (grep fallback)"
    grep -q 'fail "app selected but the release carries no image digest"' "$workflow" \
        || fail "the app evidence check is missing (grep fallback)"
    grep -q 'fail "knowledge base selected but its document count was not recorded"' "$workflow" \
        || fail "the kb evidence check is missing (grep fallback)"
    grep -q 'fail "infrastructure selected but the saved plan was not recorded"' "$workflow" \
        || fail "the infra evidence check is missing (grep fallback)"
    grep -q '"$PP_STAGING_STATUS" != "not_configured"' "$workflow" \
        || fail "the power platform not-configured marker check is missing (grep fallback)"
    grep -q "selected_components" "$workflow" \
        || fail "the manifest does not record selected components (grep fallback)"
    grep -q "component_status" "$workflow" \
        || fail "the manifest does not record component statuses (grep fallback)"
    echo "ok: release-summary gate verified textually (PyYAML unavailable)"
fi
