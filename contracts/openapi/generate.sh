#!/bin/sh
# Regenerates every artefact derived from contracts/openapi/openapi.yaml.
# Run from the repo root; exits non-zero on any drift-inducing failure.
set -eu

dotnet tool restore
dotnet build ItSupport.slnx

npm run gen:api --prefix apps/web

# The connector payload rides inside the classic solution layout under
# Connectors/ItSupportApi/, where pac solution pack carries it as a
# connector composite file (apiDefinition.swagger.json is the paconn name).
connector=apps/power-platform/solution/Connectors/ItSupportApi/apiDefinition.swagger.json
mkdir -p "$(dirname "$connector")"
dotnet hidi transform -d contracts/openapi/openapi.yaml -v 2.0 -f json --co -o "$connector"
jq --arg h "@environmentVariables('itsupport_ApiHost')" '.host = $h | .schemes = ["https"]' "$connector" > "$connector.tmp"
mv "$connector.tmp" "$connector"
