#!/bin/sh
# Structural assertions on the release-summary gate in the Release workflow.
set -eu

cd "$(git rev-parse --show-toplevel)"
workflow=.github/workflows/release.yml

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

[ -f "$workflow" ] || fail "$workflow is missing"
app_workflow=.github/workflows/app.yml
[ -f "$app_workflow" ] || fail "$app_workflow is missing"

if python3 -c 'import yaml' >/dev/null 2>&1; then
    python3 - "$workflow" "$app_workflow" <<'PY' || exit 1
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
required_jobs = [
    "changes", "validate", "app-release", "ingest-release",
    "infra-release", "pp-release", "release-summary",
    "deploy-production", "infra-apply", "ingest-production",
    "pp-production",
]
for name in required_jobs:
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
summary_terms = [
    "always()", "!failure()", "!cancelled()",
    "needs.changes.result == 'success'",
]
for term in summary_terms:
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


def evaluate(expr, results, outputs, event="push", main=True, act=False,
            failed=False, cancelled=False):
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
    body = body.replace("!failure()", str(not failed))
    body = body.replace("!cancelled()", str(not cancelled))
    body = body.replace("failure()", str(failed))
    body = body.replace("cancelled()", str(cancelled))
    body = body.replace("!github.event.act", str(not act))
    body = body.replace("github.event_name", repr(event))
    body = body.replace("github.ref", repr("refs/heads/main" if main else "refs/heads/topic"))
    body = body.replace("!=", "\x00")
    body = body.replace("!", " not ").replace("&&", " and ")
    body = body.replace("||", " or ").replace("\x00", "!=")
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
    (i for i, s in enumerate(steps) if "upload-artifact" in str(s.get("uses", ""))),
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
gate_env_terms = [
    "APP_DIGEST", "STAGING_READINESS", "STAGING_SMOKE",
    "STAGING_E2E", "KB_COMMIT", "DOC_COUNT", "PLAN_HASH",
    "PLAN_SAVED", "SOLUTION_VERSION", "PP_STAGING_STATUS",
]
for term in gate_env_terms:
    check(term in gate_env, f"the evidence gate never reads {term}")
gate_run_terms = [
    '[ -n "$APP_DIGEST" ]', '"$STAGING_READINESS" = "true"',
    '"$STAGING_SMOKE" = "pass"', '"$STAGING_E2E" = "pass"',
    '[ -n "$KB_COMMIT" ]', '[ -n "$DOC_COUNT" ]',
    '[ -n "$PLAN_HASH" ]', '"$PLAN_SAVED" = "true"',
    '"$PP_STAGING_STATUS" != "not_configured"', "exit 1",
]
for term in gate_run_terms:
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
production_legs = [
    "deploy-production", "infra-apply", "ingest-production",
    "pp-production",
]
for name in production_legs:
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

# (f) the app workflow's release legs gate on the trusted caller-event
# expression, the dead workflow_call gate is gone, and the pull-request
# build and the reusable-call trigger survive.
with open(sys.argv[2]) as fh:
    app_text = fh.read()
app_doc = yaml.safe_load(app_text)
app_jobs = app_doc.get("jobs") or {}
trusted_gate = ("github.event_name != 'pull_request' "
                "&& github.ref == 'refs/heads/main'")
for leg in ("build-publish", "deploy-staging"):
    leg_if = str((app_jobs.get(leg) or {}).get("if") or "")
    check(
        leg_if == trusted_gate,
        f"the app {leg} condition is not the trusted caller-event gate",
    )
check(
    "== 'workflow_call'" not in app_text,
    "the dead workflow_call gate still appears in app.yml",
)
check(
    "workflow_call" in app_text,
    "the app workflow_call trigger is missing",
)
check(
    "if: github.event_name == 'pull_request'" in app_text,
    "the app pull-request build gate is missing",
)

# (g) the release workflow_dispatch declares the intentional release
# input, and it defaults to false.
on_block = doc.get(True) if True in doc else (doc.get("on") or {})
dispatch_block = on_block.get("workflow_dispatch") or {}
dispatch_inputs = dispatch_block.get("inputs") or {}
release_app = dispatch_inputs.get("release_app") or {}
check(
    release_app.get("type") == "boolean",
    "the release_app dispatch input is not a boolean",
)
check(
    release_app.get("default") is False,
    "the release_app dispatch input does not default to false",
)

# (h) the changes job refuses off-main dispatches as its first step,
# adds the app selection only behind an explicit release_app dispatch,
# and keeps the changed-path detection arms untouched.
change_steps = (jobs.get("changes") or {}).get("steps") or []
refusal = change_steps[0] if change_steps else {}
check(
    refusal.get("name") == "Refuse intentional releases off main",
    "the changes job does not refuse off-main dispatches as its first step",
)
refusal_if = str(refusal.get("if") or "")
expected_refusal = ("github.event_name == 'workflow_dispatch' "
                    "&& github.ref != 'refs/heads/main'")
check(
    refusal_if == expected_refusal,
    "the refusal step fires on the wrong event or ref",
)
refusal_run = str(refusal.get("run") or "")
check(
    "exit 1" in refusal_run and "github.ref" in refusal_run,
    "the refusal step does not exit 1 naming the offending ref",
)
filter_step = next((s for s in change_steps if s.get("id") == "filter"), {})
filter_run = str(filter_step.get("run") or "")
for arm in ("services/api/*|apps/web/*|contracts/*|Dockerfile|"
            ".dockerignore|Directory.Packages.props)",
            "dotnet-tools.json|ItSupport.slnx)",
            "kb/*|services/ingest/*)",
            "infra/terraform/main/*)",
            "apps/power-platform/*)"):
    check(arm in filter_run, f"the changed-path arm {arm!r} is missing")
dispatch_guards = [
    '[ "${{ github.event_name }}" = "workflow_dispatch" ]',
    '[ "${{ github.event.inputs.release_app }}" = "true" ]',
]
for term in dispatch_guards:
    check(term in filter_run,
          f"the dispatch guard {term!r} is missing from the filter")

# (i) the manifest and the summary name the trigger and how the app
# was selected.
check(
    "trigger: $trigger" in identity_run,
    "the manifest does not record the trigger",
)
check(
    "selection_source" in identity_run
    and '"dispatch_input"' in identity_run
    and '"push_paths"' in identity_run,
    "the manifest does not record the selection source",
)
summary_step = next(
    (s for s in steps if "Selected components" in str(s.get("run") or "")),
    {},
)
summary_run = str(summary_step.get("run") or "")
check(
    "dispatch_input (intentional release)" in summary_run
    and "push_paths" in summary_run,
    "the summary does not distinguish an intentional dispatch release",
)

# Model GitHub's implicit success() guard, including skipped dependencies.
def job_runs(job, results, outputs, **context):
    expr = str(job.get("if") or "True")
    needs = job.get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    explicit_status = re.search(r"(?:always|failure|cancelled|success)\(\)", expr)
    if not explicit_status and any(not results.get(name, True) for name in needs):
        return False
    return evaluate(expr, results, outputs, **context)


import itertools

matrix_rows = 0
for event, main, act, mask in itertools.product(
    ("push", "workflow_dispatch", "schedule", "pull_request"),
    (False, True), (False, True), range(32),
):
    selected = dict(zip(("app", "kb", "infra", "power_platform", "evaluation"),
                        (bool(mask & (1 << bit)) for bit in range(5))))
    outputs = {("changes", key): value for key, value in selected.items()}
    results = {"changes": True, "validate": True,
              "app-release": selected["app"], "ingest-release": selected["kb"],
              "infra-release": selected["infra"], "pp-release": selected["power_platform"]}
    trusted = event != "pull_request" and main
    answer_selected = selected["app"] or selected["kb"] or selected["evaluation"]
    results["release-eval"] = trusted and not act and answer_selected
    context = dict(event=event, main=main, act=act)
    expected_summary = trusted and not act and any(selected.values())
    for name, expected in (
        ("app-release", trusted and selected["app"]),
        ("ingest-release", trusted and selected["kb"]),
        ("infra-release", trusted and selected["infra"]),
        ("pp-release", trusted and selected["power_platform"]),
        ("release-eval", trusted and not act and answer_selected),
        ("release-summary", expected_summary),
    ):
        actual = job_runs(jobs[name], results, outputs, **context)
        check(actual == expected, f"matrix {name} {context} selection={mask}: expected={expected} actual={actual}")
        matrix_rows += 1
    results["release-summary"] = expected_summary
    for name, component in (("deploy-production", "app"), ("ingest-production", "kb"),
                            ("infra-apply", "infra"), ("pp-production", "power_platform")):
        expected = expected_summary and selected[component]
        actual = job_runs(jobs[name], results, outputs, **context)
        check(actual == expected, f"matrix {name} {context} selection={mask}: expected={expected} actual={actual}")
        matrix_rows += 1
    for name in ("build-publish", "deploy-staging"):
        actual = evaluate(str(app_jobs[name]["if"]), {}, {}, **context)
        check(actual == trusted, f"app matrix {name} {context}: expected={trusted} actual={actual}")
        matrix_rows += 1
print(f"caller-event/ref/ACT/selection matrix: {matrix_rows} expected/actual comparisons")

# Prove the matrix kills a lost trust gate and the KB-only implicit-skip regression.
mutated = dict(jobs["release-eval"])
mutated["if"] = str(mutated["if"]).replace("github.event_name != 'pull_request'", "True")
control_outputs = {("changes", "kb"): True}
control_results = {"changes": True, "validate": True, "release-summary": True,
                  "ingest-release": True, "app-release": False}
check(job_runs(mutated, control_results, control_outputs, event="pull_request"),
      "trust-gate mutation was not detected by the PR negative control")
mutated = dict(jobs["ingest-production"])
mutated["if"] = str(mutated["if"]).replace("!cancelled() && !failure() &&", "")
check(not job_runs(mutated, control_results, control_outputs),
      "implicit-skip mutation was not detected by the KB-only negative control")
check(job_runs(jobs["ingest-production"], control_results, control_outputs),
      "KB-only production is skipped behind a successful summary")
for name in ("release-eval", "release-summary", "ingest-production"):
    check(not job_runs(jobs[name], control_results, control_outputs, cancelled=True),
          f"{name} admitted a cancelled release")
    check(not job_runs(jobs[name], control_results, control_outputs, failed=True),
          f"{name} admitted a failed release")

# Run the actual fail-closed shell with valid evidence and one-field removals.
import os
import subprocess
import tempfile

base = {name: "" for name in gate_step["env"]}
base.update(APP_SELECTED="true", KB_SELECTED="true", INFRA_SELECTED="true", PP_SELECTED="true",
              APP_DIGEST="sha256:" + "a" * 64, STAGING_READINESS="true", STAGING_SMOKE="pass",
              STAGING_E2E="pass", STAGING_ASSETS="pass", STAGING_THEME="pass", EVAL_PASSED="true",
              KB_COMMIT="b" * 40, DOC_COUNT="15", PLAN_HASH="c" * 64, PLAN_SAVED="true",
              SOLUTION_VERSION="1.0.0", PP_STAGING_STATUS="pass")
with tempfile.TemporaryDirectory() as directory:
    def shell(env):
        return subprocess.run(["/bin/sh", "-eu", "-c", gate_run], cwd=directory,
                              env={**os.environ, **env}, capture_output=True).returncode
    check(shell(base) == 0, "valid evidence rejected")
    for key in ("APP_DIGEST", "STAGING_READINESS", "STAGING_SMOKE", "STAGING_E2E", "STAGING_ASSETS",
                "STAGING_THEME", "EVAL_PASSED", "KB_COMMIT", "DOC_COUNT", "PLAN_HASH", "PLAN_SAVED",
                "SOLUTION_VERSION"):
        changed = {**base, key: ""}
        check(shell(changed) == 1, f"negative control admitted missing {key}")
        print(f"negative control missing {key}: expected=1 actual={shell(changed)}")
    for key, value in (("APP_DIGEST", "sha256:sha256:" + "a" * 64), ("DOC_COUNT", "-1"),
                        ("KB_COMMIT", "main"), ("PLAN_HASH", "malformed")):
        check(shell({**base, key: value}) == 1, f"negative control admitted malformed {key}")

    import json
    from pathlib import Path

    candidate = {"image": "ghcr.io/fixture@sha256:" + "a" * 64, "revision": "ready",
                "dataset_sha256": "d" * 64, "run_id": "fixture:1", "kb_source_commit": "b" * 40,
                "index_snapshot_sha256": "e" * 64, "chat_model": {"name": "fixture", "version": "1"},
                "answer_prompt_sha256": "f" * 64, "judge_prompt_sha256": "0" * 64}
    evidence = {"release_evidence": {"candidate": candidate, "started_at": "start",
                "completed_at": "finish", "expires_at": "expiry", "budgets": {"total_attempt_cap": 150},
                "usage": {"judge_tokens_reserved": 100}}, "run": {"usage": {"judge_calls": 15}},
                "gate": {"passed": True}}
    path = Path(directory)
    (path / "evidence").mkdir()
    (path / "evidence/evaluation.json").write_text(json.dumps(evidence))
    identity_step = next(s for s in steps if s.get("name") == "Write the release identity")
    identity_env = {name: "" for name in identity_step["env"]}
    identity_env.update(base, SOURCE_COMMIT="b" * 40, IMAGE_REPO="ghcr.io/fixture",
                        RUN_URL="https://github.com/fixture/actions/runs/1", TRIGGER="push",
                        GITHUB_STEP_SUMMARY=str(path / "summary"))
    for selection in ("APP_SELECTED", "KB_SELECTED", "EVAL_SELECTED", "PP_SELECTED"):
        env = {**identity_env, **{key: "false" for key in
              ("APP_SELECTED", "KB_SELECTED", "EVAL_SELECTED", "INFRA_SELECTED", "PP_SELECTED")},
              selection: "true"}
        if selection == "PP_SELECTED":
            env.update(EVAL_PASSED="", SOLUTION_VERSION="", PP_STAGING_STATUS="not_configured")
        result = subprocess.run(["/bin/sh", "-eu", "-c", identity_step["run"]], cwd=directory,
                                env={**os.environ, **env}, capture_output=True)
        check(result.returncode == 0, f"approval artifact failed for {selection}")
        if result.returncode:
            continue
        manifest = json.loads((path / "release-identity.json").read_text())
        check("sha256:sha256:" not in manifest.get("image", ""), "approval digest doubled its algorithm")
        check(manifest["proof_pointer"].endswith("#artifacts"), "approval proof pointer missing")
        if selection == "PP_SELECTED":
            check(manifest["component_status"]["power_platform"] == "skipped_not_configured",
                  "unconfigured PP claimed approval readiness")
        else:
            check(manifest["evaluation"]["candidate"] == candidate, "approval candidate changed")
            check(manifest["evaluation"]["gate"]["passed"], "approval omitted gate verdict")
            check(manifest["evaluation"]["budgets"] == evidence["release_evidence"]["budgets"],
                  "approval omitted budget evidence")
        check(all(value != "released" for value in manifest["component_status"].values()),
              "preapproval manifest claimed release")
    print("approval artifact controls: app, KB, evaluation and unconfigured PP passed")

if failures:
    for message in failures:
        print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)
print("ok: the release summary skips workflow-only pushes, runs behind "
      "a selected component, fails closed on missing evidence, and the "
      "app release legs gate on the trusted caller event")
PY
else
    # PyYAML is unavailable, so this falls back to textual assertions.
    # It pins the exact expected lines instead of parsing; it cannot
    # evaluate the gates, only verify they are present verbatim.
    grep -q "needs: \[changes, validate, app-release, ingest-release, infra-release, pp-release, release-eval\]" "$workflow" \
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
    # shellcheck disable=SC2016
    grep -q '"$PP_STAGING_STATUS" != "not_configured"' "$workflow" \
        || fail "the power platform not-configured marker check is missing (grep fallback)"
    grep -q "selected_components" "$workflow" \
        || fail "the manifest does not record selected components (grep fallback)"
    grep -q "component_status" "$workflow" \
        || fail "the manifest does not record component statuses (grep fallback)"
    # The app workflow's release legs gate on the trusted caller-event
    # expression; the dead workflow_call gate is gone.
    [ "$(grep -c "github.event_name != 'pull_request' && github.ref == 'refs/heads/main'" "$app_workflow")" -ge 2 ] \
        || fail "an app release leg lost the trusted caller-event gate (grep fallback)"
    grep -q "== 'workflow_call'" "$app_workflow" \
        && fail "the dead workflow_call gate still appears in app.yml (grep fallback)"
    grep -q "workflow_call" "$app_workflow" \
        || fail "the app workflow_call trigger is missing (grep fallback)"
    grep -q "if: github.event_name == 'pull_request'" "$app_workflow" \
        || fail "the app pull-request build gate is missing (grep fallback)"
    # The dispatch input, the refusal step, the guarded selection, the
    # untouched path arms, and the manifest markers.
    grep -q "release_app:" "$workflow" \
        || fail "the release_app dispatch input is missing (grep fallback)"
    grep -q "type: boolean" "$workflow" \
        || fail "the release_app input is not declared boolean (grep fallback)"
    grep -q "default: false" "$workflow" \
        || fail "the release_app input does not default to false (grep fallback)"
    grep -q "Refuse intentional releases off main" "$workflow" \
        || fail "the off-main refusal step is missing (grep fallback)"
    grep -q "github.event_name == 'workflow_dispatch' && github.ref != 'refs/heads/main'" "$workflow" \
        || fail "the refusal condition is missing (grep fallback)"
    # shellcheck disable=SC2016
    grep -q '"${{ github.event_name }}" = "workflow_dispatch"' "$workflow" \
        || fail "the dispatch guard on the event name is missing (grep fallback)"
    # shellcheck disable=SC2016
    grep -q '"${{ github.event.inputs.release_app }}" = "true"' "$workflow" \
        || fail "the release_app selection guard is missing (grep fallback)"
    grep -q "services/ingest/\*)" "$workflow" \
        || fail "the kb changed-path arm is missing (grep fallback)"
    grep -q "infra/terraform/main/\*)" "$workflow" \
        || fail "the infra changed-path arm is missing (grep fallback)"
    grep -q "apps/power-platform/\*)" "$workflow" \
        || fail "the power platform changed-path arm is missing (grep fallback)"
    grep -q "dotnet-tools.json|ItSupport.slnx)" "$workflow" \
        || fail "the dotnet-tools changed-path arm is missing (grep fallback)"
    grep -q "trigger: \$trigger" "$workflow" \
        || fail "the manifest does not record the trigger (grep fallback)"
    grep -q "selection_source" "$workflow" \
        || fail "the manifest does not record the selection source (grep fallback)"
    grep -q '"dispatch_input"' "$workflow" \
        || fail "the dispatch_input marker is missing (grep fallback)"
    grep -q '"push_paths"' "$workflow" \
        || fail "the push_paths marker is missing (grep fallback)"
    echo "ok: release-summary gate verified textually (PyYAML unavailable)"
fi
