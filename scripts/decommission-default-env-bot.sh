#!/usr/bin/env bash
# Delete the retired Copilot Studio bot and its connections from the
# tenant's Default environment. The Azure-native agent (services/api +
# the Terraform-registered bot) replaced it; this script executes the
# recorded deletion list from the refactor plan:
#
#   1. the "IT Support Assistant" bot row in the Default environment's
#      Dataverse (removes the agent and its topics),
#   2. the AI Search knowledge connection and any connection to the
#      copaz-api application created for the bot.
#
# copaz-pp and the connector-import pipeline are NOT touched.
#
# Run as demo@ (owner of the environment):
#
#   az login                                      # as demo@
#   scripts/decommission-default-env-bot.sh       # lists what would go
#   scripts/decommission-default-env-bot.sh --delete   # performs it
#
# Without --delete the script only lists its targets and verifies the
# access; nothing is deleted.
set -euo pipefail

environment_id="882f3b3b-3219-e312-8b2c-2218691f414c"
bot_id="5b75e053-0bbf-f111-aaad-6045bde1c8a8"
bot_name="IT Support Assistant"

mode="list"
[ "${1:-}" = "--delete" ] && mode="delete"

command -v az >/dev/null || { echo "az is required" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }
command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }

az account show >/dev/null 2>&1 || { echo "run az login first" >&2; exit 1; }

org_url="${DEFAULT_ENV_DATAVERSE_URL:-}"
if [ -z "$org_url" ]; then
  echo "resolving the Default environment's Dataverse org URL..."
  org_url="$(az rest --method GET \
    --resource https://management.azure.com \
    --url "https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/environments/$environment_id?api-version=2023-06-01" \
    --query "properties.linkedEnvironmentMetadata.instanceUrl" -o tsv 2>/dev/null || true)"
fi
if [ -z "$org_url" ]; then
  echo "could not resolve the org URL through the BAP API." >&2
  echo "set it explicitly: DEFAULT_ENV_DATAVERSE_URL=https://orgXXXXXXXX.crm.dynamics.com" >&2
  echo "(read it from the browser address bar of make.powerapps.com while in" >&2
  echo " the Default environment)" >&2
  exit 1
fi
org_url="${org_url%/}"
echo "environment: $environment_id"
echo "dataverse:   $org_url"

dataverse_token="$(az account get-access-token --resource "$org_url" --query accessToken -o tsv)"

dv() {
  curl --silent --show-error \
    -H "Authorization: Bearer $dataverse_token" \
    -H "OData-MaxVersion: 4.0" \
    -H "OData-Version: 4.0" \
    "$@"
}

echo
echo "=== bot: $bot_name ($bot_id) ==="
http="$(dv -o "$org_url/api/data/v9.2/bots($bot_id)" -w '%{http_code}' --output /dev/null || true)"
if [ "$http" = "404" ]; then
  echo "already absent (404); nothing to do"
elif [ "$http" != "200" ]; then
  echo "unexpected HTTP $http reading the bot row" >&2
  exit 1
else
  if [ "$mode" = "delete" ]; then
    http="$(dv -X DELETE -o "$org_url/api/data/v9.2/bots($bot_id)" -w '%{http_code}' --output /dev/null)"
    if [ "$http" = "204" ]; then
      echo "deleted the bot row (HTTP 204)"
    else
      echo "delete returned HTTP $http" >&2
      exit 1
    fi
  else
    echo "present; run with --delete to remove it"
  fi
fi

echo
echo "=== connections in the environment ==="
powerapps_token="$(az account get-access-token --resource https://service.powerapps.com/ --query accessToken -o tsv)"
connections="$(curl --silent --show-error \
  -H "Authorization: Bearer $powerapps_token" \
  "https://api.powerapps.com/providers/Microsoft.Power.Apps/environments/$environment_id/connections?api-version=2016-11-01")"

targets="$(printf '%s' "$connections" | jq -r '
  .value[]
  | select(
      ((.displayName // "") | test("AI Search|copaz|It Support|IT Support"; "i"))
      or ((.apiParameters // {} | tostring) | test("copaz"; "i"))
      or ((.id // "") | test("copaz"; "i"))
    )
  | "\(.name)\t\(.displayName // "(unnamed)")\t\(.connectedOn // "never connected")"')"

if [ -z "$targets" ]; then
  echo "no AI Search / copaz-api connections remain in this environment"
else
  printf '%s\n' "$targets" | while IFS="$(printf '\t')" read -r name display connected; do
    echo "connection: $display ($name, connected: $connected)"
    if [ "$mode" = "delete" ]; then
      http="$(curl --silent --show-error \
        -X DELETE \
        -H "Authorization: Bearer $powerapps_token" \
        -o /dev/null -w '%{http_code}' \
        "https://api.powerapps.com/providers/Microsoft.Power.Apps/environments/$environment_id/connections/$name?api-version=2016-11-01")"
      if [ "$http" = "200" ] || [ "$http" = "204" ] || [ "$http" = "404" ]; then
        echo "  deleted (HTTP $http)"
      else
        echo "  delete returned HTTP $http (it may be shared; delete it in make.powerapps.com -> Connections)" >&2
      fi
    else
      echo "  run with --delete to remove it"
    fi
  done
fi

if [ "$mode" = "delete" ]; then
  echo
  echo "=== verification ==="
  http="$(dv -o "$org_url/api/data/v9.2/bots($bot_id)" -w '%{http_code}' --output /dev/null || true)"
  echo "bot row: HTTP $http (404 expected)"
  [ "$http" = "404" ] || { echo "bot row still present" >&2; exit 1; }
  echo "verified: the Default environment holds no such bot"
  echo "note: any cloud flows the agent used appear in make.powerautomate.com"
  echo " (Default environment) if Studio did not remove them with the agent."
fi
