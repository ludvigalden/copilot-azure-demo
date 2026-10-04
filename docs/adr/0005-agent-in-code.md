# 0005 - The agent in code

- **Status:** Accepted
- **Date:** 2026-10-04

## Context

The assistant began as a Power Platform agent: the conversational
logic lived in a maker-authored agent, reached the API through a
custom connector, and shipped as an unmanaged solution. Once the
infrastructure behind it was declarative end to end — Terraform
describing every Azure resource, pipelines applying them — the agent
layer stood out as the one part of the system that deployment text
could not describe. Building it, moving it between environments, and
recovering it all ran through interactive studio work, and its
conversation logic could not be unit-tested at all.

The agent's actual behavior turned out to be small and
rule-shaped: three intents, a greeting, a reset, a fallback. Nothing
in it needed a low-code canvas.

## Decision

The conversational agent is ordinary code in the API service. The
Microsoft 365 Agents SDK hosts it on the same ASP.NET Core
application; routing is plain pattern dispatch; the intents call the
same answer, ticket, and directory services the web app uses. The
Azure Bot registration and its channels are Terraform resources like
every other, and the bot's Microsoft App identity is the
application's user-assigned managed identity, so the channel path
adds no credential.

This makes the agent layer match the properties the rest of the
system already has. Declarative deployment covers it: a merge to
`main` provisions or updates the registration, the channels, and the
endpoint exactly as it does the container app. The identity story
stays secretless: no client secret exists anywhere in the channel
path. And the logic is testable: every routing branch is a unit
test, and the whole conversation surface is provable headlessly over
Direct Line by a script.

The Power Platform agent is not deleted but banked. The unmanaged
solution still packs, imports, and publishes through its pipeline —
that path is demonstrated and stays green — and it remains the
faster route to a maker-editable agent for someone working in that
toolchain. What it cannot offer is the properties above: its
authoring and some environment steps stay interactive, and its logic
has no unit surface. It is a secondary path, documented as such, not
a competing design.

## Consequences

- One codebase carries the whole conversational surface; the
  repository layout gained no new runtime unit, only the agent
  classes inside the API and the bot resources in Terraform.
- The Teams door stays open at zero cost: the channel is live, the
  app package and its build and publish scripts are committed, and
  nothing in this repository blocks on a tenant that has Teams.
- The Power Platform solution, its setup checklist, and the agent
  design notes remain in the repository as the banked secondary
  path, each marked as such where a reader would otherwise take them
  for the primary one.
