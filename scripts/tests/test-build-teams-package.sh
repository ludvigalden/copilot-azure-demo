#!/bin/sh
# Hermetic regression tests for scripts/build-teams-package.sh.
#
# The builder treats schema validation as mandatory: a clean first run
# into an absent build directory must validate and package, while an
# unreachable official schema, a missing python jsonschema module, a
# manifest that fails the schema, and a structurally broken manifest
# must all end the build nonzero with no package left behind. A failed
# run must never leave a deliverable that could be mistaken for one the
# same run validated, so the stale package from an earlier build is
# removed before the schema is fetched and a failure is expected to
# leave the build directory without it.
#
# The builder under test is a copy invoked from a throwaway tree, and
# the schema fetch is answered by a stub curl on a temporary PATH, so
# the repository's own dist/ is never touched and nothing is fetched
# from the network. Hermetic: no network, no cloud, no CI wiring.
set -eu

cd "$(git rev-parse --show-toplevel)"
builder=scripts/build-teams-package.sh
pkg_name=itsupport-teams.zip

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

root="$(mktemp -d /tmp/teams-package-test.XXXXXX)"
trap 'rm -rf "$root"' EXIT INT TERM

# The sandbox tree: the builder resolves its repository root from $0,
# so copying it under scripts/ makes the sandbox the root every output
# path it computes is resolved against.
mkdir -p "$root/scripts" "$root/apps/teams/icons" "$root/bin" \
    "$root/empty-pythonpath"
cp "$builder" "$root/scripts/build-teams-package.sh"
cp apps/teams/manifest.json "$root/apps/teams/manifest.json"
cp apps/teams/icons/color.png apps/teams/icons/outline.png \
    "$root/apps/teams/icons/"

# The stub curl: fails with $CURL_STUB_RC when that is nonzero,
# otherwise writes $CURL_STUB_BODY to the -o target.
cat > "$root/bin/curl" <<'EOF'
#!/bin/sh
rc=${CURL_STUB_RC:-0}
case $rc in
    ''|*[!0-9]*) rc=0 ;;
esac
if [ "$rc" -ne 0 ]; then
    echo "curl: (stub) connection failed" >&2
    exit "$rc"
fi
out=
prev=
for arg in "$@"; do
    if [ "$prev" = "-o" ]; then
        out=$arg
    fi
    prev=$arg
done
if [ -z "$out" ]; then
    echo "curl: stub: no -o target" >&2
    exit 2
fi
printf '%s' "${CURL_STUB_BODY:-}" > "$out"
EOF
chmod +x "$root/bin/curl"

# A schema any object satisfies, and one the repository manifest fails.
accepting_schema='{"title":"stub teams manifest schema","type":"object"}'
rejecting_schema='{"title":"stub teams manifest schema","type":"object",
    "required":["copazStubNeverPresentField"]}'

# Reset the sandbox sources to a clean pre-build state.
reset_tree() {
    rm -rf "$root/apps/teams/dist"
    cp apps/teams/manifest.json "$root/apps/teams/manifest.json"
}

# Run the copied builder in the sandbox. $1: schema body the stub
# serves, $2: stub curl exit code. $stub_pythonpath must be set by the
# caller; the default empty directory leaves the real site packages in
# place, so only the case that stubs jsonschema passes another path.
run_builder() {
    (cd "$root" &&
        PATH="$root/bin:$PATH" \
        PYTHONPATH="$stub_pythonpath" \
        CURL_STUB_BODY="$1" \
        CURL_STUB_RC="$2" \
        ./scripts/build-teams-package.sh) >"$root/out.log" 2>&1
}

# (a) A clean first run into an absent build directory succeeds, and
# the fresh dist carries the fetched schema: the directory the schema
# lands in was created before the download, not after it.
reset_tree
stub_pythonpath="$root/empty-pythonpath"
rc=0
run_builder "$accepting_schema" 0 || rc=$?
if [ "$rc" -ne 0 ]; then
    fail "clean first run exited $rc, want 0; log:""$(sed 's/^/    /' "$root/out.log")"
fi
[ -f "$root/apps/teams/dist/schema.json" ] \
    || fail "the schema was not written into the fresh build directory"
grep -qF "validated against the official v1.16 schema" "$root/out.log" \
    || fail "the success line is missing from a clean run"
[ -f "$root/apps/teams/dist/$pkg_name" ] \
    || fail "no package was built by a clean run"

# (b) An unreachable schema fails the build, and the package an earlier
# build left behind is gone: nothing survives that could be read as a
# validated deliverable.
reset_tree
mkdir -p "$root/apps/teams/dist"
printf 'stale package from an earlier build' \
    > "$root/apps/teams/dist/$pkg_name"
rc=0
run_builder "$accepting_schema" 7 || rc=$?
[ "$rc" -ne 0 ] || fail "an unreachable schema must fail the build"
[ ! -e "$root/apps/teams/dist/$pkg_name" ] \
    || fail "a stale package survived a run whose schema never validated"
grep -qF "unreachable (curl exit 7)" "$root/out.log" \
    || fail "the failure does not say the schema is unreachable"
if grep -qF "validated against the official" "$root/out.log"; then
    fail "a run whose schema was never fetched claims to have validated"
fi

# (c) A missing python jsonschema module fails the build: the stub
# module on PYTHONPATH raises ImportError exactly as an absent
# installation would, and the error names the validator, not the schema.
reset_tree
mkdir -p "$root/no-jsonschema"
printf 'raise ImportError("stub: jsonschema is not installed")\n' \
    > "$root/no-jsonschema/jsonschema.py"
stub_pythonpath="$root/no-jsonschema"
rc=0
run_builder "$accepting_schema" 0 || rc=$?
stub_pythonpath="$root/empty-pythonpath"
[ "$rc" -ne 0 ] || fail "a missing validator must fail the build"
[ ! -e "$root/apps/teams/dist/$pkg_name" ] \
    || fail "a package was built although no validator was available"
grep -qF "jsonschema module is missing" "$root/out.log" \
    || fail "the failure does not name the missing validator"
if grep -qF "VALIDATION FAILED" "$root/out.log"; then
    fail "a missing validator is misreported as a schema failure"
fi

# (d1) A manifest that passes the structural checks but fails the
# schema must fail the build, and the failure must be reported as a
# schema failure, not as an unreachable schema.
reset_tree
rc=0
run_builder "$rejecting_schema" 0 || rc=$?
[ "$rc" -ne 0 ] || fail "a manifest that fails the schema must fail the build"
[ ! -e "$root/apps/teams/dist/$pkg_name" ] \
    || fail "a package was built from a manifest that failed the schema"
grep -qF "VALIDATION FAILED" "$root/out.log" \
    || fail "a schema failure is not reported as one"
if grep -qF "unreachable" "$root/out.log"; then
    fail "a schema failure is misreported as an unreachable schema"
fi

# (d2) A structurally broken manifest fails the build before any schema
# is fetched.
reset_tree
jq 'del(.version)' apps/teams/manifest.json \
    > "$root/apps/teams/manifest.json"
rc=0
run_builder "$accepting_schema" 0 || rc=$?
[ "$rc" -ne 0 ] || fail "a structurally broken manifest must fail the build"
[ ! -e "$root/apps/teams/dist/$pkg_name" ] \
    || fail "a package was built from a broken manifest"
grep -qF "missing required fields" "$root/out.log" \
    || fail "the structural failure does not name the missing fields"
if grep -qF "validated against the official" "$root/out.log"; then
    fail "a structurally broken manifest was sent to schema validation"
fi

# (e) The successful package has exactly the documented shape: the
# manifest at the archive root, both icons alongside, and nothing
# beyond the icons directory entry zip -r records for it.
reset_tree
rc=0
run_builder "$accepting_schema" 0 || rc=$?
[ "$rc" -eq 0 ] || fail "the final clean run exited $rc, want 0"
python3 - "$root/apps/teams/dist/$pkg_name" <<'PY' || \
    fail "the package contents do not match the documented shape"
import sys
import zipfile

names = sorted(zipfile.ZipFile(sys.argv[1]).namelist())
expected = [
    "icons/",
    "icons/color.png",
    "icons/outline.png",
    "manifest.json",
]
if names != expected:
    sys.exit("zip holds %r, want %r" % (names, expected))
PY

echo "ok: clean first run validates and packages; unreachable schema, missing validator, schema failure and broken manifest all exit nonzero with no package left behind"
