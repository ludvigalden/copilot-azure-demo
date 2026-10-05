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
# in the local Docker store.
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
trap 'rm -rf "$root"' EXIT INT TERM

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
