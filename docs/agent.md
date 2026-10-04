# The conversational agent

The web app is not the only way to talk to the assistant. The same API
service also hosts a conversational agent: a bot that answers the same
questions from the same knowledge base, opens tickets in the same
store, and reaches the same user directory — over any Bot Framework
channel. The web SPA posts to `/api/*`; the agent receives Bot
Protocol activities at `/api/bot/messages`. One service, one retrieval
pipeline, one ticket store, two doors in.

This page describes the agent as it exists today. The earlier Power
Platform agent design is the banked secondary path:
[copilot-studio-agent.md](copilot-studio-agent.md) records
its design and [power-platform-setup.md](power-platform-setup.md)
its setup checklist.

## What the agent can do

Routing is plain C# pattern dispatch — no generative orchestration —
so every branch is unit-testable and reads top to bottom in
`services/api/ItSupport.Api/Bot/`:

- **Answer a how-to question.** The question goes through the same
  answer provider the web app uses, so the reply carries the same
  citation list; the agent renders it as a `Sources:` block of
  numbered references.
- **Escalate into a ticket.** A message starting with `escalate:`
  creates a ticket through the normal ticket service — Table Storage
  by default, ServiceNow when credentials are configured — and
  replies with an Adaptive Card carrying the `IT-<date>-<suffix>`
  number the caller references afterwards.
- **Look up a profile.** "My profile" returns the caller's display
  name, email, department and manager from the user directory. In the
  cloud the caller's identity arrives with the channel message, and
  directory reads ride single sign-on; until that sign-on flow is
  wired, the intent says so and points at the web app instead of
  guessing at an identity.

Around the three intents sit the small system behaviors: a greeting
when the bot joins a conversation, an explicit reset, a fallback that
states what the agent can do, and error handling. Development runs
accept unsigned local activity posts, which is how the headless
development loop drives every intent over plain HTTP; production
requires the bot's JWT authentication scheme on the endpoint.

## Identity: the bot is the managed identity

The Azure Bot registration's Microsoft App identity is the
application's user-assigned managed identity — the bot's app ID is
that identity's client ID, and the registration points at the identity
resource. Channel traffic into the app therefore authenticates with
the same identity the API already uses outbound, and no client secret
exists anywhere. Terraform owns the registration and its channels; see
ADR 0005 for the decision and ADR 0004 for the infrastructure around
it.

## Channels

The registration carries three channels:

- **Direct Line** — a signed REST surface for headless clients. This
  is the channel scripted verification uses, and the one tooling
  should use.
- **Web Chat** — the token service that a web chat client would
  exchange a secret with. No client embeds it today.
- **Microsoft Teams** — the channel is live on the bot, and the
  installable Teams app package is built by
  `scripts/build-teams-package.sh` from `apps/teams/manifest.json`.
  The tenant this demo runs in carries no Teams service: publishing
  the package there is blocked upstream of this repository, and the
  publish script (`scripts/publish-teams-app.sh`) is ready for a
  tenant that has the service. Nothing else waits on it.

Staging runs the same registration for its own bot under the staging
prefix; only the owner apply declares the Teams channel, since Teams
is one tenant-wide service rather than a per-environment resource.

## Proving the agent end to end, headlessly

`scripts/verify-bot-conversation.sh` holds a real conversation over
Direct Line and asserts the knowledge answer carries a citation — the
same assertion a Teams or Web Chat conversation would show, scripted
so it runs anywhere an `az login` with the channel-key read permission
does:

```sh
az login                      # a principal that can read the channel keys
scripts/verify-bot-conversation.sh
```

The script fetches the Direct Line key for the bot, opens a
conversation, sends a warm-up message (the container app scales to
zero, and the first delivery after an idle period can outlive the
connector's patience while the app cold-starts), then asks a knowledge
question and passes only if the reply cites its sources. The question,
bot name, and resource group are environment-variable overridable.
