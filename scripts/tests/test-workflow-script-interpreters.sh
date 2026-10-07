#!/bin/sh
# Workflows must run repo shell scripts under the interpreter the
# script declares. On GitHub's ubuntu runners `sh` is dash, which
# rejects bash-only constructs before a script's own logic ever runs:
# release-candidate.yml invoked scripts/verify-bot-conversation.sh
# with `sh`, dash aborted it at `set -euo pipefail` (exit 2, run
# 37456420261, step "Prove the knowledge answer end to end"), and the
# Direct Line proof had never executed in CI.
#
# This suite fails when any .github/workflows/*.yml invokes a repo
# .sh script via `sh` while the script is not POSIX sh — a bash
# shebang, or bash-only constructs in its body — or when the
# invocation names a script that does not exist. A `#!/bin/sh` target
# without bashisms (contracts/openapi/generate.sh) stays legal, so the
# rule is conditional: `sh` is allowed exactly when the target is.
#
# Two halves, both deterministic and offline: a synthetic red/green
# proof in a throwaway tree (the guard proving itself, the pattern of
# test-local-run.sh), then the scan of this repo's real workflows
# (the gate). No network, no runner, no cloud.
set -eu

cd "$(git rev-parse --show-toplevel)"

# scan <repo-root>: print one FAIL line per violation to stderr and
# return nonzero when any workflow under <repo-root>/.github/workflows
# invokes a repo .sh script via `sh` that is not POSIX sh. Reads the
# working tree through git grep, so uncommitted edits are seen. Only a
# clean scan counts: git grep matching nothing (exit 1) is an empty
# result, but a git grep error (exit >= 2) fails the scan instead of
# passing as one.
scan() {
    _repo=$1
    _hits=$(git -C "$_repo" grep -nE \
        '(^|[^A-Za-z0-9_])sh[[:space:]]+(\./)?[A-Za-z0-9_./-]+\.sh([^A-Za-z0-9_./-]|$)' \
        -- .github/workflows) && _rc=0 || _rc=$?
    case $_rc in
    0 | 1) ;;
    *)
        printf 'FAIL scan failed: git grep exited %s over %s/.github/workflows\n' \
            "$_rc" "$_repo" >&2
        return 1
        ;;
    esac
    _fail=0
    while IFS= read -r _hit; do
        [ -n "$_hit" ] || continue
        _where=${_hit%%:*}
        _rest=${_hit#*:}
        _lineno=${_rest%%:*}
        _text=${_rest#*:}
        _path=$(printf '%s\n' "$_text" | sed -n \
            's/.*[^A-Za-z0-9_]sh[[:space:]]\{1,\}\([A-Za-z0-9_./-]\{1,\}\.sh\).*/\1/p')
        _path=${_path#./}
        [ -n "$_path" ] || continue
        if [ ! -f "$_repo/$_path" ]; then
            printf 'FAIL %s:%s invokes %s via sh, but no such script exists\n' \
                "$_where" "$_lineno" "$_path" >&2
            _fail=1
            continue
        fi
        IFS= read -r _first < "$_repo/$_path" || _first=
        case $_first in
        *bash*)
            printf 'FAIL %s:%s runs %s via sh, but it declares %s\n' \
                "$_where" "$_lineno" "$_path" "$_first" >&2
            _fail=1
            continue
            ;;
        esac
        # Strip comments, flatten whitespace, then look for the
        # high-signal bash-only tokens. A POSIX target touches none.
        _body=$(sed 's/#.*$//' "$_repo/$_path" | tr '\n\t' '  ')
        case $_body in
        *"[["* | *"pipefail"* | *"&>>"* | *" local "* | *" declare "*)
            printf 'FAIL %s:%s runs %s via sh, but it uses bash-only constructs\n' \
                "$_where" "$_lineno" "$_path" >&2
            _fail=1
            ;;
        esac
    done <<EOF
$_hits
EOF
    return "$_fail"
}

# --- The synthetic proof: a throwaway repo exercising every verdict. ---

fixture=$(mktemp -d /tmp/workflow-interpreters-test.XXXXXX)
trap 'rm -rf "$fixture"' EXIT INT TERM

mkdir -p "$fixture/.github/workflows" "$fixture/scripts"
printf '#!/usr/bin/env bash\nset -euo pipefail\ntrue\n' \
    > "$fixture/scripts/bash-target.sh"
printf '#!/bin/sh\nset -eu\ntrue\n' > "$fixture/scripts/posix-target.sh"
printf 'name: Broken\non: push\njobs:\n  x:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n          sh scripts/bash-target.sh\n' \
    > "$fixture/.github/workflows/broken.yml"
printf 'name: Ghost\non: push\njobs:\n  x:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n          sh scripts/ghost.sh\n' \
    > "$fixture/.github/workflows/ghost.yml"
printf 'name: Legal\non: push\njobs:\n  x:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n          sh scripts/posix-target.sh\n' \
    > "$fixture/.github/workflows/legal.yml"
git -C "$fixture" init -q -b main
git -C "$fixture" add .github scripts
git -C "$fixture" -c user.name=test -c user.email=test@example.invalid \
    -c commit.gpgsign=false commit -qm fixture

# Red: the bash-shebang target, the missing target, and only those,
# must be flagged. The legal POSIX invocation must not be.
if red=$(scan "$fixture" 2>&1); then
    printf 'fail: broken fixture passed the scan\n' >&2
    exit 1
fi
case $red in
*bash-target.sh*) ;;
*) printf 'fail: diagnostics omit the bash-shebang target:\n%s\n' "$red" >&2
    exit 1 ;;
esac
case $red in
*ghost.sh*) ;;
*) printf 'fail: diagnostics omit the missing target:\n%s\n' "$red" >&2
    exit 1 ;;
esac
case $red in
*posix-target.sh*)
    printf 'fail: the legal POSIX invocation was flagged:\n%s\n' "$red" >&2
    exit 1 ;;
esac

# Green: repair the two broken invocations in place; nothing remains.
printf 'name: Broken\non: push\njobs:\n  x:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n          bash scripts/bash-target.sh\n' \
    > "$fixture/.github/workflows/broken.yml"
printf 'name: Ghost\non: push\njobs:\n  x:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n          sh scripts/posix-target.sh\n' \
    > "$fixture/.github/workflows/ghost.yml"
if green=$(scan "$fixture" 2>&1); then
    :
else
    printf 'fail: repaired fixture still fails:\n%s\n' "$green" >&2
    exit 1
fi

# --- The gate: this repo's real workflows must be clean. ---

scan "$(pwd)"
printf 'workflow-script-interpreters: every sh-invoked repo script is POSIX sh; ok\n'
