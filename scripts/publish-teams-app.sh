#!/usr/bin/env bash
# Publish the Teams app package to the tenant app catalog through the
# Graph API — the scripted, zero-portal-click route.
#
# Run as a Global Administrator with an az login in the demo tenant:
#
#   az login            # as demo@ (Global Administrator)
#   scripts/publish-teams-app.sh
#
# The script self-provisions the one application permission the upload
# needs (TeamsAppManagement.ReadWrite.All on Microsoft Graph, granted to
# the repository's tool principal whose credentials live in .env.local),
# exchanges them for a client-credentials token, and POSTs the package to
# /appCatalogs/teamsApps. Both provisioning steps are idempotent.
#
# A 403 reading "Microsoft Teams hasn't been provisioned on the tenant"
# means the tenant holds no Microsoft Teams service; publishing anything
# (scripted or manual) is blocked until the tenant has a Teams-capable
# Office 365 subscription. See apps/teams/README.md for the recorded
# spike evidence.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

command -v az >/dev/null || { echo "az is required" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }
command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }
[ -f .env.local ] || { echo ".env.local with ARM_CLIENT_ID/ARM_CLIENT_SECRET is required (never committed)" >&2; exit 1; }

az account show >/dev/null 2>&1 || { echo "run az login first" >&2; exit 1; }
tenant_id="$(az account show --query tenantId -o tsv)"
echo "tenant: $tenant_id"
echo "signed in as: $(az account show --query user.name -o tsv)"

client_id="$(grep -E '^ARM_CLIENT_ID=' .env.local | cut -d= -f2- | tr -d '\r')"
client_secret="$(grep -E '^ARM_CLIENT_SECRET=' .env.local | cut -d= -f2- | tr -d '\r')"
if [ -z "$client_id" ] || [ -z "$client_secret" ]; then
  echo ".env.local must carry ARM_CLIENT_ID and ARM_CLIENT_SECRET" >&2
  exit 1
fi

./scripts/build-teams-package.sh >/dev/null
pkg="apps/teams/dist/itsupport-teams.zip"
echo "package: $pkg"

graph_sp_id="$(az ad sp show --id 00000003-0000-0000-c000-000000000000 --query id -o tsv)"
role_id="$(az ad sp show --id 00000003-0000-0000-c000-000000000000 \
  --query "appRoles[?value=='TeamsAppManagement.ReadWrite.All'].id | [0]" -o tsv)"
tool_sp_id="$(az ad sp show --id "$client_id" --query id -o tsv)"
echo "graph service principal: $graph_sp_id"
echo "tool principal: $client_id ($tool_sp_id)"

grant_filter="appRoleId%20eq%20%27${role_id}%27%20and%20principalId%20eq%20${tool_sp_id}"
read_existing() {
  az rest --method GET \
    --url "https://graph.microsoft.com/v1.0/servicePrincipals/$graph_sp_id/appRoleAssignedTo?%24filter=$grant_filter" \
    --query "value[0].id" -o tsv 2>/dev/null || true
}
existing="$(read_existing)"
if [ -n "$existing" ] && [ "$existing" != "" ]; then
  echo "permission: TeamsAppManagement.ReadWrite.All already granted"
else
  echo "permission: granting TeamsAppManagement.ReadWrite.All to the tool principal"
  if ! az rest --method POST \
      --url "https://graph.microsoft.com/v1.0/servicePrincipals/$graph_sp_id/appRoleAssignedTo" \
      --headers "Content-Type=application/json" \
      --body "{\"principalId\":\"$tool_sp_id\",\"resourceId\":\"$graph_sp_id\",\"appRoleId\":\"$role_id\"}" \
      >/dev/null; then
    if [ -n "$(read_existing)" ]; then
      echo "permission: grant call reported an error, but the assignment exists - continuing"
    else
      echo "grant failed; consent in a browser instead (one click as a Global Administrator):" >&2
      echo "  https://login.microsoftonline.com/$tenant_id/adminconsent?client_id=$client_id" >&2
      exit 1
    fi
  else
    echo "permission: granted"
  fi
fi

token="$(curl --fail --silent \
  -d grant_type=client_credentials \
  -d "client_id=$client_id" \
  -d "client_secret=$client_secret" \
  -d "scope=https://graph.microsoft.com/.default" \
  "https://login.microsoftonline.com/$tenant_id/oauth2/v2.0/token" | jq -er .access_token)"
echo "token: client-credentials for Graph acquired"

echo "uploading to the org app catalog..."
http="$(curl --silent \
  -X POST \
  -H "Authorization: Bearer $token" \
  -H "Content-Type: application/zip" \
  --data-binary @"$pkg" \
  -o "$pkg.response.json" \
  -w '%{http_code}' \
  "https://graph.microsoft.com/v1.0/appCatalogs/teamsApps")"

if [ "$http" = "201" ] || [ "$http" = "200" ]; then
  app_id="$(jq -r .id "$pkg.response.json")"
  state="$(jq -r .publishingState "$pkg.response.json")"
  echo "published: teamsApp id $app_id (state: $state)"
  echo "install from Teams: Apps -> Built for your organization -> IT Support"
elif [ "$http" = "409" ]; then
  app_id="$(jq -r .id "$pkg.response.json")"
  echo "already in the catalog (teamsApp id $app_id); submitting an update"
  http="$(curl --silent \
    -X POST \
    -H "Authorization: Bearer $token" \
    -H "Content-Type: application/zip" \
    --data-binary @"$pkg" \
    -o "$pkg.response.json" \
    -w '%{http_code}' \
    "https://graph.microsoft.com/v1.0/appCatalogs/teamsApps/$app_id/appDefinitions")"
  if [ "$http" = "200" ] || [ "$http" = "201" ] || [ "$http" = "202" ]; then
    echo "update submitted (state: $(jq -r .publishingState "$pkg.response.json" 2>/dev/null || echo unknown))"
  else
    echo "update failed: HTTP $http" >&2
    cat "$pkg.response.json" >&2
    exit 1
  fi
else
  echo "upload failed: HTTP $http" >&2
  cat "$pkg.response.json" >&2
  rm -f "$pkg.response.json"
  exit 1
fi
rm -f "$pkg.response.json"
