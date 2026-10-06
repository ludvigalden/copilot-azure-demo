#!/bin/sh
# The local loop must end where the runner ends: a workflow that fails
# leaves scripts/local-run with a nonzero status, and a workflow that
# passes leaves it at zero. The real workflows touch Azure, so this
# runs synthetic ones under the same names through the entry point
# itself, in a throwaway checkout with dummy credentials and steps that
# never leave the container. Nothing is fetched: the runner image must
# already be cached by earlier local runs. The runner's image pull
# behavior is honest in the entry point's own header: act pulls by
# default, so a locally built image is used only when it already sits
# in the local Docker store. The lint case needs no runner: it runs
# scripts/local-run lint in a second throwaway tree with a clean probe
# and a deliberately broken one, reusing the real checkout's installed
# dependencies through a symlink.
set -eu

cd "$(git rev-parse --show-toplevel)"
entry=scripts/local-run
image="$(sed -n 's/.*-P ubuntu-latest=\([^ ]*\).*/\1/p' "$entry")"

command -v act >/dev/null 2>&1 || {
    echo "skip: act is not installed" >&2
    exit 77
}
: "${DOCKER_BIN:=docker}"
command -v "$DOCKER_BIN" >/dev/null 2>&1 || {
    echo "skip: docker is not installed ($DOCKER_BIN not found)" >&2
    exit 77
}
"$DOCKER_BIN" image inspect "$image" >/dev/null 2>&1 || {
    echo "skip: runner image $image is not cached; run a local loop first" >&2
    exit 77
}

root="$(mktemp -d /tmp/local-run-test.XXXXXX)"
lint_root="$(mktemp -d /tmp/local-run-lint-test.XXXXXX)"
trap 'rm -rf "$root" "$lint_root"' EXIT INT TERM

mkdir -p "$root/scripts" "$root/.github/workflows"
cp "$entry" "$root/scripts/local-run"

# The failing case: a single step that exits 1, under the name of the
# workflow whose local run is the usual red signal.
cat > "$root/.github/workflows/ci.yml" <<'YAML'
name: CI
on: pull_request
jobs:
  probe:
    runs-on: ubuntu-latest
    steps:
      - name: deliberate failure
        run: echo "deliberate failure" && exit 1
YAML

# The passing case: the same shape, exiting 0 — proof the loop does not
# fail everything.
cat > "$root/.github/workflows/ingest.yml" <<'YAML'
name: Ingest
on: workflow_dispatch
jobs:
  probe:
    runs-on: ubuntu-latest
    steps:
      - name: deliberate success
        run: echo "all good"
YAML

cat > "$root/.env.local" <<'EOF'
ARM_CLIENT_ID=00000000-0000-0000-0000-000000000000
ARM_CLIENT_SECRET=not-a-real-secret
ARM_TENANT_ID=00000000-0000-0000-0000-000000000000
ARM_SUBSCRIPTION_ID=00000000-0000-0000-0000-000000000000
EOF
printf '{ "act": true }\n' > "$root/event.json"

git -C "$root" init -q -b main
git -C "$root" add scripts/local-run .github/workflows/ci.yml \
    .github/workflows/ingest.yml event.json .env.local
git -C "$root" -c user.name=test -c user.email=test@example.invalid \
    -c commit.gpgsign=false commit -qm probe

fail_rc=0
(cd "$root" && sh scripts/local-run ci) >"$root/fail.log" 2>&1 || fail_rc=$?
green_rc=0
(cd "$root" && sh scripts/local-run ingest) >"$root/green.log" 2>&1 || green_rc=$?

if [ "$fail_rc" -eq 0 ]; then
    echo "FAIL: the failing workflow exited 0 through $entry" >&2
    tail -n 5 "$root/fail.log" >&2
    exit 1
fi
if ! grep -q "^local-run: act exited with status $fail_rc\$" "$root/fail.log"; then
    echo "FAIL: the status line is missing from the failing run's output" >&2
    tail -n 5 "$root/fail.log" >&2
    exit 1
fi
if [ "$(tail -n 1 "$root/fail.log")" \
    != "local-run: act exited with status $fail_rc" ]; then
    echo "FAIL: the verdict line is not the failing run's last output line" >&2
    tail -n 5 "$root/fail.log" >&2
    exit 1
fi
if [ "$green_rc" -ne 0 ]; then
    echo "FAIL: the passing workflow exited $green_rc through $entry" >&2
    tail -n 5 "$root/green.log" >&2
    exit 1
fi
if ! grep -q "^local-run: act exited with status 0\$" "$root/green.log"; then
    echo "FAIL: the passing run's output lacks its status-0 verdict line" >&2
    tail -n 5 "$root/green.log" >&2
    exit 1
fi

echo "ok: failing run exited $fail_rc, passing run exited 0"

# The lint case: scripts/local-run lint runs the exact command CI runs,
# with no .env.local and no act, and keeps the same verdict discipline.
# The throwaway tree carries its own sources and biome config; the
# dependencies (biome itself) are reused from the real checkout through
# a symlink rather than reinstalled, and the lint script is derived
# from the real package.json so the probe cannot drift from CI.
command -v npm >/dev/null 2>&1 || {
    echo "skip: npm is not installed" >&2
    exit 77
}
command -v node >/dev/null 2>&1 || {
    echo "skip: node is not installed" >&2
    exit 77
}
[ -x apps/web/node_modules/.bin/biome ] || {
    echo "skip: apps/web dependencies are not installed" >&2
    exit 77
}

mkdir -p "$lint_root/scripts" "$lint_root/apps/web/src"
cp "$entry" "$lint_root/scripts/local-run"
cp apps/web/biome.json "$lint_root/apps/web/biome.json"
node -e '
  const fs = require("fs")
  const pkg = JSON.parse(fs.readFileSync("apps/web/package.json", "utf8"))
  const probe = { name: "web-lint-probe", private: true, scripts: { lint: pkg.scripts.lint } }
  fs.writeFileSync(process.argv[1], JSON.stringify(probe, null, 2) + "\n")
' "$lint_root/apps/web/package.json"
ln -s "$PWD/apps/web/node_modules" "$lint_root/apps/web/node_modules"

# The python probes: local-run lint also runs CI's ruff format check
# in each Python project, so the throwaway tree carries both
# projects' manifests and a format-clean probe, reusing each real
# checkout's virtualenv through a symlink the same way the web probe
# reuses node_modules. UV_NO_SYNC keeps that reuse read-only: without
# it uv would sync the probe tree and try to build its copy of the
# ingest package into the shared virtualenv.
command -v uv >/dev/null 2>&1 || {
    echo "skip: uv is not installed" >&2
    exit 77
}
for project in eval services/ingest; do
    [ -x "$project/.venv/bin/ruff" ] || {
        echo "skip: $project is not synced (ruff missing from its venv)" >&2
        exit 77
    }
done
for project in eval services/ingest; do
    mkdir -p "$lint_root/$project"
    cp "$project/pyproject.toml" "$project/uv.lock" "$lint_root/$project/"
    ln -s "$PWD/$project/.venv" "$lint_root/$project/.venv"
    printf 'answer = 42\n' >"$lint_root/$project/probe.py"
done
git -C "$lint_root" init -q -b main

# The green case: a source file that satisfies every enabled rule.
cat > "$lint_root/apps/web/src/probe.ts" <<'EOF'
// A source file that satisfies every enabled rule.
export const answer = "all good"
EOF

lint_green_rc=0
(cd "$lint_root" && UV_NO_SYNC=1 sh scripts/local-run lint) \
    >"$lint_root/green.log" 2>"$lint_root/green.err" \
    || lint_green_rc=$?

# The red case: the same probe with a string single-quoted and a
# semicolon added, both of which the config's formatter rejects
# (it requires double quotes and omits semicolons).
cat > "$lint_root/apps/web/src/probe.ts" <<'EOF'
// A source file that deliberately breaks the formatting rules.
export const answer = 'deliberate violation';
EOF

lint_fail_rc=0
(cd "$lint_root" && UV_NO_SYNC=1 sh scripts/local-run lint) \
    >"$lint_root/fail.log" 2>"$lint_root/fail.err" \
    || lint_fail_rc=$?

if [ "$lint_green_rc" -ne 0 ]; then
    echo "FAIL: the lint command exited $lint_green_rc on a clean probe" >&2
    tail -n 5 "$lint_root/green.err" >&2
    exit 1
fi
if [ "$(tail -n 1 "$lint_root/green.log")" != "local-run: lint exited with status 0" ]; then
    echo "FAIL: the green lint run's output lacks its verdict as the last line" >&2
    tail -n 5 "$lint_root/green.log" >&2
    exit 1
fi
if [ "$lint_fail_rc" -eq 0 ]; then
    echo "FAIL: the lint command exited 0 on a deliberately broken probe" >&2
    tail -n 5 "$lint_root/fail.err" >&2
    exit 1
fi
if ! grep -q "^local-run: lint exited with status $lint_fail_rc\$" "$lint_root/fail.log"; then
    echo "FAIL: the status line is missing from the failing lint run's output" >&2
    tail -n 5 "$lint_root/fail.log" >&2
    exit 1
fi
if [ "$(tail -n 1 "$lint_root/fail.log")" \
    != "local-run: lint exited with status $lint_fail_rc" ]; then
    echo "FAIL: the verdict line is not the failing lint run's last output line" >&2
    tail -n 5 "$lint_root/fail.log" >&2
    exit 1
fi

echo "ok: lint green run exited 0, lint red run exited $lint_fail_rc"

# The python red cases: the same probe with an assignment ruff's
# formatter would respace, in one project at a time, fails the run
# the same way even with the web probe clean — proof that each
# project's format check actually runs.
cat > "$lint_root/apps/web/src/probe.ts" <<'EOF'
// A source file that satisfies every enabled rule.
export const answer = "all good"
EOF
for broken in eval services/ingest; do
    label=$(printf '%s' "$broken" | tr / -)
    for project in eval services/ingest; do
        if [ "$project" = "$broken" ]; then
            printf 'answer=42\n' >"$lint_root/$project/probe.py"
        else
            printf 'answer = 42\n' >"$lint_root/$project/probe.py"
        fi
    done
    py_fail_rc=0
    (cd "$lint_root" && UV_NO_SYNC=1 sh scripts/local-run lint) \
        >"$lint_root/pyfail-$label.log" 2>"$lint_root/pyfail-$label.err" \
        || py_fail_rc=$?
    if [ "$py_fail_rc" -eq 0 ]; then
        echo "FAIL: the lint command exited 0 with a misformatted $broken probe" >&2
        tail -n 5 "$lint_root/pyfail-$label.err" >&2
        exit 1
    fi
    if ! grep -q "^local-run: lint exited with status $py_fail_rc\$" \
        "$lint_root/pyfail-$label.log"; then
        echo "FAIL: the status line is missing from the failing $broken run" >&2
        tail -n 5 "$lint_root/pyfail-$label.log" >&2
        exit 1
    fi
    if [ "$(tail -n 1 "$lint_root/pyfail-$label.log")" \
        != "local-run: lint exited with status $py_fail_rc" ]; then
        echo "FAIL: the verdict line is not the $broken run's last output line" >&2
        tail -n 5 "$lint_root/pyfail-$label.log" >&2
        exit 1
    fi
    echo "ok: lint red run with a misformatted $broken probe exited $py_fail_rc"
done
