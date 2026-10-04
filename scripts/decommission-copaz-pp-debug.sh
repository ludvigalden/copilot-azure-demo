#!/usr/bin/env bash
# Delete the two unmanaged debug artifacts left in copaz-pp by the
# phase-5 connector work — the empty emptySolution and the publisher it
# carried (DefaultPublisherdavidjend3651). Banked-path hygiene: the
# solution holds nothing, nothing imports it, and the green
# connector-import pipeline does not touch either artifact.
#
# Run as demo@ (owner of copaz-pp):
#
#   az login                                        # as demo@
#   scripts/decommission-copaz-pp-debug.sh          # lists what would go
#   scripts/decommission-copaz-pp-debug.sh --delete # performs it
#
# Without --delete the script only lists its targets and verifies the
# access; nothing is deleted.
set -euo pipefail

org_url="${COPAZ_PP_DATAVERSE_URL:-https://org449080d5.crm17.dynamics.com}"
org_url="${org_url%/}"
solution_unique_name="emptySolution"
publisher_unique_name="DefaultPublisherdavidjend3651"

mode="list"
[ "${1:-}" = "--delete" ] && mode="delete"

command -v az >/dev/null || { echo "az is required" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }
command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }

az account show >/dev/null 2>&1 || { echo "run az login first" >&2; exit 1; }

token="$(az account get-access-token --resource "$org_url" --query accessToken -o tsv)"

dv() {
  curl --silent --show-error \
    -H "Authorization: Bearer $token" \
    -H "OData-MaxVersion: 4.0" \
    -H "OData-Version: 4.0" \
    -H "If-None-Match: null" \
    "$@"
}

lookup_id() {
  local entity="$1" filter="$2"
  printf '%s' "$(dv --get --data-urlencode "\$filter=$filter" \
    "$org_url/api/data/v9.2/$entity" | jq -r ".value[0].$3 // empty")"
}

echo "environment: copaz-pp ($org_url)"

echo
echo "=== solution: $solution_unique_name ==="
solution_id="$(lookup_id "solutions" "uniquename eq '$solution_unique_name'" solutionid)"
if [ -z "$solution_id" ]; then
  echo "already absent; nothing to do"
else
  friendly="$(dv -o "$org_url/api/data/v9.2/solutions($solution_id)" | jq -r '.friendlyname // ""')"
  echo "found: $solution_unique_name \"$friendly\" ($solution_id)"
  if [ "$mode" = "delete" ]; then
    http="$(dv -X DELETE -o "$org_url/api/data/v9.2/solutions($solution_id)" -w '%{http_code}' --output /dev/null)"
    if [ "$http" = "204" ]; then
      echo "deleted (HTTP 204)"
    else
      echo "delete returned HTTP $http" >&2
      exit 1
    fi
  else
    echo "run with --delete to remove it"
  fi
fi

echo
echo "=== publisher: $publisher_unique_name ==="
publisher_id="$(lookup_id "publishers" "uniquename eq '$publisher_unique_name'" publisherid)"
if [ -z "$publisher_id" ]; then
  echo "already absent; nothing to do"
else
  friendly="$(dv -o "$org_url/api/data/v9.2/publishers($publisher_id)" | jq -r '.friendlyname // ""')"
  echo "found: $publisher_unique_name \"$friendly\" ($publisher_id)"
  if [ "$mode" = "delete" ]; then
    http="$(dv -X DELETE -o "$org_url/api/data/v9.2/publishers($publisher_id)" -w '%{http_code}' --output /dev/null)"
    if [ "$http" = "204" ]; then
      echo "deleted (HTTP 204)"
    else
      echo "delete returned HTTP $http (a publisher in use cannot be deleted;" >&2
      echo " make.powerapps.com -> Solutions -> manage publishers shows what holds it)" >&2
      exit 1
    fi
  else
    echo "run with --delete to remove it"
  fi
fi

if [ "$mode" = "delete" ]; then
  echo
  echo "=== verification ==="
  [ -z "$(lookup_id "solutions" "uniquename eq '$solution_unique_name'" solutionid)" ] \
    || { echo "solution still present" >&2; exit 1; }
  [ -z "$(lookup_id "publishers" "uniquename eq '$publisher_unique_name'" publisherid)" ] \
    || { echo "publisher still present" >&2; exit 1; }
  echo "verified: copaz-pp holds neither artifact"
fi
