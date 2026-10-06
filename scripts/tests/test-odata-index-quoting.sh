#!/bin/sh
# Azure AI Search OData system functions take string literals in single
# quotes: an index is addressed as indexes('kb'), and an unquoted
# indexes(kb) is a parse error the service answers with Not Found (CI
# run 37468347088, ingest-release/staging-check, the document-count
# step).
#
# This suite fails when any .github/workflows/*.yml builds an
# indexes( URL whose index name is not single-quoted: the guard
# demands a single quote as the next non-space character after every
# indexes( occurrence. It is a textual rule, not a YAML parse, because
# the invocation lives inside run: shell text. The check inspects the
# first indexes( on each line; the workflows keep one URL build per
# line.
#
# Two halves, both deterministic and offline: a synthetic red/green
# proof in a throwaway tree (the guard proving itself, the pattern of
# test-workflow-script-interpreters.sh), then the scan of this repo's
# real workflows (the gate). No network, no runner, no cloud.
set -eu

cd "$(git rev-parse --show-toplevel)"

# scan <repo-root>: print one FAIL line per violation to stderr and
# return nonzero when any workflow under <repo-root>/.github/workflows
# carries an indexes( whose index name is not single-quoted. Reads the
# working tree through git grep, so uncommitted edits are seen.
scan() {
    _repo=$1
    _hits=$(git -C "$_repo" grep -nF 'indexes(' -- .github/workflows || true)
    _fail=0
    while IFS= read -r _hit; do
        [ -n "$_hit" ] || continue
        _where=${_hit%%:*}
        _rest=${_hit#*:}
        _lineno=${_rest%%:*}
        _text=${_rest#*:}
        _tail=${_text#*"indexes("}
        _tail=${_tail#"${_tail%%[![:space:]]*}"}
        case $_tail in
        "'"*) continue ;;
        esac
        printf 'FAIL %s:%s builds an unquoted OData index name: indexes(%s\n' \
            "$_where" "$_lineno" "$_tail" >&2
        _fail=1
    done <<EOF
$_hits
EOF
    return "$_fail"
}

# --- The synthetic proof: a throwaway repo exercising every verdict. ---

fixture=$(mktemp -d /tmp/odata-index-quoting-test.XXXXXX)
trap 'rm -rf "$fixture"' EXIT INT TERM

mkdir -p "$fixture/.github/workflows"
cat > "$fixture/.github/workflows/broken.yml" <<'YAML'
name: Broken
on: push
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - run: |
          url="$(printf 'https://%s-srch.search.windows.net/indexes(%s)/docs/$count?api-version=2024-07-01' "$p" "$n")"
          url2="https://x-srch.search.windows.net/indexes(${{ env.INDEX_NAME }}/docs"
YAML
cat > "$fixture/.github/workflows/legal.yml" <<'YAML'
name: Legal
on: push
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - run: |
          url="$(printf 'https://%s-srch.search.windows.net/indexes('\''%s'\'')/docs/$count?api-version=2024-07-01' "$p" "$n")"
          url2="https://x-srch.search.windows.net/indexes('${{ env.INDEX_NAME }}')/docs"
          # the indexes collection serves the knowledge-base documents
YAML
git -C "$fixture" init -q -b main
git -C "$fixture" add .github
git -C "$fixture" -c user.name=test -c user.email=test@example.invalid \
    -c commit.gpgsign=false commit -qm fixture

# Red: the unquoted %s placeholder and the unquoted env expression,
# and only those, must be flagged. The legal quoted forms and the
# paren-less mention of indexes must not be.
if red=$(scan "$fixture" 2>&1); then
    printf 'fail: broken fixture passed the scan\n' >&2
    exit 1
fi
case $red in
*broken.yml:8*) ;;
*) printf 'fail: diagnostics omit the unquoted %%s case:\n%s\n' "$red" >&2
    exit 1 ;;
esac
case $red in
*broken.yml:9*) ;;
*) printf 'fail: diagnostics omit the unquoted env case:\n%s\n' "$red" >&2
    exit 1 ;;
esac
case $red in
*legal.yml*)
    printf 'fail: a legal line was flagged:\n%s\n' "$red" >&2
    exit 1 ;;
esac

# Green: quote both index names in place; nothing remains.
cat > "$fixture/.github/workflows/broken.yml" <<'YAML'
name: Broken
on: push
jobs:
  x:
    runs-on: ubuntu-latest
    steps:
      - run: |
          url="$(printf 'https://%s-srch.search.windows.net/indexes('\''%s'\'')/docs/$count?api-version=2024-07-01' "$p" "$n")"
          url2="https://x-srch.search.windows.net/indexes('${{ env.INDEX_NAME }}')/docs"
YAML
if green=$(scan "$fixture" 2>&1); then
    :
else
    printf 'fail: repaired fixture still fails:\n%s\n' "$green" >&2
    exit 1
fi

# --- The gate: this repo's real workflows must be clean. ---

scan "$(pwd)"
printf 'odata-index-quoting: every indexes( URL in .github/workflows quotes its index name; ok\n'
