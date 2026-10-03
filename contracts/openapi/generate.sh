#!/bin/sh
# Regenerates every artefact derived from contracts/openapi/openapi.yaml.
# Run from the repo root; exits non-zero on any drift-inducing failure.
set -eu

dotnet tool restore
dotnet build ItSupport.slnx

npm run gen:api --prefix apps/web

# The connector payload rides inside the solution layout under
# Connector/, the composite file set the solution importer reads
# (openapidefinition, connectionparameters, policytemplateinstances,
# iconblob), named after the connector with special characters encoded.
connector=apps/power-platform/solution/Connector/itsup_ItSupportApi_openapidefinition.json
mkdir -p "$(dirname "$connector")"
dotnet hidi transform -d contracts/openapi/openapi.yaml -v 2.0 -f json --co -o "$connector"
jq --arg h "@environmentVariables('itsup_ApiHost')" '.host = $h | .schemes = ["https"]' "$connector" > "$connector.tmp"
mv "$connector.tmp" "$connector"
