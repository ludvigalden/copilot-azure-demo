#!/bin/sh
# Regenerates every artefact derived from contracts/openapi/openapi.yaml.
# Run from the repo root; exits non-zero on any drift-inducing failure.
set -eu

dotnet tool restore
dotnet build ItSupport.slnx

npm run gen:api --prefix apps/web

connector=apps/power-platform/solution/Connectors/ItSupportApi_openapidefinition.json
dotnet hidi transform -d contracts/openapi/openapi.yaml -v 2.0 -f json --co -o "$connector"
jq --arg h '@environmentVariables("itsupport_ApiHost")' '.host = $h' "$connector" > "$connector.tmp"
mv "$connector.tmp" "$connector"
