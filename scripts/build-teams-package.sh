#!/usr/bin/env bash
# Build and validate the Teams app package from apps/teams/.
#
# Produces apps/teams/dist/itsupport-teams.zip (manifest at the archive
# root, icons alongside) after checking the manifest's JSON shape, the
# icon dimensions Teams requires (color 192x192, outline 32x32), and
# the manifest against the published JSON schema itself. The schema
# fetch and validation are mandatory: an unreachable schema, a missing
# python jsonschema module, or a manifest that fails the schema ends
# the build nonzero, and the build directory carries no package from a
# failed run. The zip is a disposable build artifact: the .gitignore
# keeps dist/ out of the repository.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
src="$root/apps/teams"
out="$src/dist"
pkg="$out/itsupport-teams.zip"
schema_url="https://developer.microsoft.com/en-us/json-schemas/teams/v1.16/MicrosoftTeams.Schema.json"

command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }
command -v zip >/dev/null || { echo "zip is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

jq -e . "$src/manifest.json" >/dev/null || { echo "manifest.json is not valid JSON" >&2; exit 1; }
echo "manifest.json: valid JSON"

python3 - "$src/manifest.json" "$src/icons/color.png" "$src/icons/outline.png" <<'PY'
import json
import struct
import sys

manifest_path, color_path, outline_path = sys.argv[1:4]
manifest = json.load(open(manifest_path))

required = ["manifestVersion", "version", "id", "packageName", "developer",
            "name", "description", "icons", "accentColor"]
missing = [k for k in required if k not in manifest]
if missing:
    sys.exit("manifest.json is missing required fields: " + ", ".join(missing))

def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        sys.exit(path + " is not a PNG file")
    return struct.unpack(">I", head[16:20])[0], struct.unpack(">I", head[20:24])[0]

color = png_size(color_path)
outline = png_size(outline_path)
if color != (192, 192):
    sys.exit("color icon must be 192x192, got %dx%d" % color)
if outline != (32, 32):
    sys.exit("outline icon must be 32x32, got %dx%d" % outline)

bot_ids = [b["botId"] for b in manifest.get("bots", [])]
if len(bot_ids) != 1 or not bot_ids[0]:
    sys.exit("exactly one bots[] entry with a botId is required")
print("manifest structure: ok (icons %dx%d / %dx%d, 1 bot)" % (color + outline))
PY

# The build directory must exist before the schema lands in it, and the
# package from any earlier build must go before validation: a failed
# run leaves no deliverable behind that could be mistaken for one this
# run validated.
mkdir -p "$out"
rm -f "$pkg"

schema_rc=0
curl --fail --silent --show-error --max-time 20 "$schema_url" \
    -o "$out/schema.json" || schema_rc=$?
if [ "$schema_rc" -ne 0 ]; then
    echo "manifest schema: the official schema is unreachable (curl exit $schema_rc): $schema_url" >&2
    exit 1
fi

if python3 - "$src/manifest.json" "$out/schema.json" <<'PY'
import json
import sys

try:
    import jsonschema
except ImportError:
    sys.exit(2)
jsonschema.validate(json.load(open(sys.argv[1])), json.load(open(sys.argv[2])))
PY
then echo "manifest schema: validated against the official v1.16 schema"
else
    rc=$?
    if [ "$rc" -eq 2 ]; then
        echo "manifest schema: the python jsonschema module is missing, cannot validate" >&2
    else
        echo "manifest schema: VALIDATION FAILED against the official schema" >&2
    fi
    exit 1
fi

(cd "$src" && zip -q -r -X "$pkg" manifest.json icons)
unzip -l "$pkg"
echo "package built: $pkg"
