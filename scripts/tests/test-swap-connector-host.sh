#!/bin/sh
# Hermetic coverage for scripts/swap-connector-host.py: the swap must
# find exactly one connector swagger definition in the solution source
# tree, fail closed with an actionable message on zero or multiple
# matches, refuse non-swagger shapes and malformed hosts, and preserve
# every byte of the document apart from the host value. The zero- and
# multiple-found cases are the negative controls for the exactly-one
# guard: removing that guard must flip them, not soften them.
set -eu

cd "$(git rev-parse --show-toplevel)"

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

script=scripts/swap-connector-host.py
[ -f "$script" ] || fail "$script is missing"

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

# The fixture mirrors the real artifact's authoring form: a swagger 2.0
# dict whose host is the itsup_ApiHost environment-variable token,
# written with the same two-space indent and trailing newline as the
# committed definition.
write_definition() {
    dst=$1
    mkdir -p "$(dirname "$dst")"
    python3 - "$dst" <<'PY'
import json
import sys

doc = {
    "swagger": "2.0",
    "info": {"title": "ItSupportApi", "version": "1.0.0"},
    "host": "@environmentVariables('itsup_ApiHost')",
    "basePath": "/api",
    "schemes": ["https"],
    "paths": {
        "/profile": {"get": {"responses": {"200": {"description": "ok"}}}}
    },
    "tags": [{"name": "portal", "description": "the portal and the agent."}],
}
with open(sys.argv[1], "w") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\n")
PY
}

connector_name=itsup_ItSupportApi_openapidefinition.json

# 1. Success: exactly one definition, host swapped, every other byte
# preserved.
one="$scratch/one"
write_definition "$one/Connector/$connector_name"
cp "$one/Connector/$connector_name" "$scratch/one.before"
out="$(python3 "$script" --host staging.example.internal \
    --solution-folder "$one")" \
    || fail "the swap failed on the single-definition happy path"
case "$out" in
    *"host swapped in"*"$connector_name"*) ;;
    *) fail "the swap did not report the file it rewrote: $out" ;;
esac
python3 - "$scratch/one.before" "$one/Connector/$connector_name" <<'PY' \
    || fail "the swap changed bytes outside the host value"
import json
import sys

old = open(sys.argv[1], "rb").read()
new = open(sys.argv[2], "rb").read()
token = b'"@environmentVariables(\'itsup_ApiHost\')"'
assert old.count(token) == 1, "fixture must carry the host token exactly once"
expected = old.replace(token, b'"staging.example.internal"')
assert new == expected, "bytes outside the host value changed"
before, after = json.loads(old), json.loads(new)
before.pop("host"), after.pop("host")
assert before == after, "the document changed beyond the host field"
assert json.loads(new)["host"] == "staging.example.internal"
print("ok: only the host value changed; the rest is byte-identical")
PY

# 2. Zero found: a Connector folder with no *_openapidefinition.json
# file must fail with the glob, the count, and a remedy.
zero="$scratch/zero"
mkdir -p "$zero/Connector"
printf '{}\n' > "$zero/Connector/itsup_ItSupportApi_connectionparameters.json"
cp "$zero/Connector/itsup_ItSupportApi_connectionparameters.json" "$scratch/conn.before"
if python3 "$script" --host staging.example.internal \
    --solution-folder "$zero" >"$scratch/zero.out" 2>"$scratch/zero.err"; then
    fail "the swap succeeded with zero connector definitions"
fi
err="$(cat "$scratch/zero.err")"
case "$err" in
    *"expected exactly one connector definition"*) ;;
    *) fail "zero-found error lacks the exactly-one expectation: $err" ;;
esac
case "$err" in
    *"Connector/*_openapidefinition.json"*) ;;
    *) fail "zero-found error lacks the glob it scanned: $err" ;;
esac
case "$err" in
    *"found 0"*) ;;
    *) fail "zero-found error lacks the count: $err" ;;
esac
case "$err" in
    *"check that the solution folder"*) ;;
    *) fail "zero-found error lacks the remedy: $err" ;;
esac
cmp -s "$zero/Connector/itsup_ItSupportApi_connectionparameters.json" \
    "$scratch/conn.before" || fail "zero-found run touched an unmatched file"
echo "ok: zero definitions fail closed with glob, count, and remedy"

# 3. Multiple found: two definitions must fail naming both files, and
# neither may be rewritten. This is the guard's negative control.
multi="$scratch/multi"
write_definition "$multi/Connector/aaa_extra_openapidefinition.json"
write_definition "$multi/Connector/zzz_main_openapidefinition.json"
cp "$multi/Connector/aaa_extra_openapidefinition.json" "$scratch/aaa.before"
cp "$multi/Connector/zzz_main_openapidefinition.json" "$scratch/zzz.before"
if python3 "$script" --host staging.example.internal \
    --solution-folder "$multi" >"$scratch/multi.out" 2>"$scratch/multi.err"; then
    fail "the swap succeeded with two connector definitions"
fi
err="$(cat "$scratch/multi.err")"
case "$err" in
    *"found 2"*) ;;
    *) fail "multiple-found error lacks the count: $err" ;;
esac
case "$err" in
    *"aaa_extra_openapidefinition.json"*"zzz_main_openapidefinition.json"*) ;;
    *) fail "multiple-found error does not name both files: $err" ;;
esac
case "$err" in
    *"remove or rename the stale extra files"*) ;;
    *) fail "multiple-found error lacks the remedy: $err" ;;
esac
cmp -s "$multi/Connector/aaa_extra_openapidefinition.json" "$scratch/aaa.before" \
    || fail "multiple-found run rewrote a matched file"
cmp -s "$multi/Connector/zzz_main_openapidefinition.json" "$scratch/zzz.before" \
    || fail "multiple-found run rewrote a matched file"
echo "ok: two definitions fail closed naming both, neither rewritten"

# 4. Wrong form: a file matching the glob that is not a swagger dict
# with a host field must fail actionably, with no fallback path.
form="$scratch/form"
mkdir -p "$form/Connector"
printf '[]\n' > "$form/Connector/not_swagger_openapidefinition.json"
if python3 "$script" --host staging.example.internal \
    --solution-folder "$form" >"$scratch/form.out" 2>"$scratch/form.err"; then
    fail "the swap accepted a non-dict definition"
fi
case "$(cat "$scratch/form.err")" in
    *"not_swagger_openapidefinition.json"*"swagger document carrying a host field"*) ;;
    *) fail "non-dict error is not actionable: $(cat "$scratch/form.err")" ;;
esac
printf '{"swagger": "2.0"}\n' > "$form/Connector/not_swagger_openapidefinition.json"
if python3 "$script" --host staging.example.internal \
    --solution-folder "$form" >"$scratch/form.out" 2>"$scratch/form.err"; then
    fail "the swap accepted a definition without a host field"
fi
case "$(cat "$scratch/form.err")" in
    *"swagger document carrying a host field"*) ;;
    *) fail "hostless error is not actionable: $(cat "$scratch/form.err")" ;;
esac
echo "ok: non-swagger shapes fail actionably with no fallback"

# 5. Host validation: an empty host or one carrying a scheme or path
# must fail before any file is read.
hostcase="$scratch/hostcase"
write_definition "$hostcase/Connector/$connector_name"
cp "$hostcase/Connector/$connector_name" "$scratch/host.before"
if python3 "$script" --host "" \
    --solution-folder "$hostcase" >"$scratch/host.out" 2>"$scratch/host.err"; then
    fail "the swap accepted an empty host"
fi
if python3 "$script" --host "https://staging.example.internal" \
    --solution-folder "$hostcase" >"$scratch/host.out" 2>"$scratch/host.err"; then
    fail "the swap accepted a host with a scheme"
fi
case "$(cat "$scratch/host.err")" in
    *"bare hostname"*) ;;
    *) fail "host rejection is not actionable: $(cat "$scratch/host.err")" ;;
esac
cmp -s "$hostcase/Connector/$connector_name" "$scratch/host.before" \
    || fail "a rejected host still rewrote the definition"
echo "ok: malformed hosts fail closed before any rewrite"

echo "ok: swap-connector-host finds exactly one definition, swaps only the host value, \
and fails closed on zero, multiple, wrong-form, and malformed-host inputs"
