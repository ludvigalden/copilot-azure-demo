# Teams app package

This directory holds the Microsoft Teams app package that makes the
agent installable in Teams: a manifest pointing at the bot, the two
icons Teams requires, and the scripts that build, publish, and verify
the whole path without touching a portal.

- `manifest.json` — Teams app manifest (v1.16); `bots[].botId` is the
  bot's Microsoft App ID (the container app's managed-identity client
  id, `4ee0f855-64aa-4858-b976-ac3e5f01ddea`, the same id the bot
  registration in `infra/terraform/main/bot.tf` carries).
- `icons/color.png` (192x192) and `icons/outline.png` (32x32).
- `dist/` — build output (git-ignored), produced by
  `scripts/build-teams-package.sh`.
- `scripts/publish-teams-app.sh` — publishes the package to the tenant
  app catalog through `POST /appCatalogs/teamsApps`, zero portal clicks.
- `scripts/verify-bot-conversation.sh` — proves the bot end to end over
  Direct Line: warm-up, then a knowledge question whose answer must
  carry a citation.

## Spike verdict (2026-10-04): the tenant has no Microsoft Teams

The scripted org-catalog route was implemented and exercised, and it is
**blocked by tenant provisioning, not by permissions, manifest schema,
or anything in this repository**:

```
POST https://graph.microsoft.com/v1.0/appCatalogs/teamsApps
HTTP 403
{"error":{"code":"Forbidden","message":"Microsoft Teams hasn't been
provisioned on the tenant.  Ensure the tenant has a valid Office365
subscription."}}
innerError.request-id: fd246acb-5c1d-44ae-97b9-e43a2695f335
```

The tenant holds Azure (the whole demo runs there) but no Office 365 /
Microsoft Teams workload, so there is no Teams service to publish into
and no Teams client for this tenant to sideload into either — the
manual-upload fallback is blocked by the same fact, one step further
down. The permission model was never reached: provisioning is a
tenant-level gate that precedes the caller's scope check.

Consequences, in order:

1. The **Teams channel itself is live and declarative**:
   `azurerm_bot_channel_ms_teams` on the bot in `infra/terraform/main/bot.tf`
   (owner apply only — the channel is tenant-level), applied by the
   standard `infra.yml` path like every other resource.
2. The **package is real and schema-validated** (against the official
   v1.16 schema at build time) and the **publish route is scripted**:
   once the tenant carries a Teams-capable subscription, the whole
   publish is `az login` + `scripts/publish-teams-app.sh` — the script
   self-grants `TeamsAppManagement.ReadWrite.All` to the tool
   principal, exchanges its credentials for a Graph token, uploads the
   zip, and prints the catalog id. No recurring clicks exist in this
   path; a manifest change is a package rebuild and re-run.
3. Until then, the bot remains reachable on its other channels, and the
   scripted conversation proof
   (`scripts/verify-bot-conversation.sh`) asserts the same cited-answer
   behavior a Teams conversation would show.

## Building and publishing

```sh
scripts/build-teams-package.sh     # validate + zip into apps/teams/dist/
scripts/publish-teams-app.sh       # az login (Global Administrator) first
```

The publish script expects the tool principal's client id and secret in
`.env.local` (`ARM_CLIENT_ID` / `ARM_CLIENT_SECRET`, never committed).
If the Graph grant step cannot complete, it prints the one-click admin
consent URL as the fallback.
