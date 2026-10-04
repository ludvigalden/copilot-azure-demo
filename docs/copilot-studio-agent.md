# Copilot Studio agent design

> **Secondary path.** The repository's primary agent is the one in
> code — see [docs/agent.md](agent.md) and
> [ADR 0005](adr/0005-agent-in-code.md). This document records the
> design of the earlier Power Platform agent, kept as the banked
> alternative for a maker-authored agent.

This document is the full design of the Copilot Studio agent and its
agent flow, at the level of exact build steps. The agent does not
exist as a live resource, and nothing in the repository claims
authorship of one: the committed solution under
`apps/power-platform/solution/` carries only the custom connector and
the environment variable definition, because `pac solution pack` —
verified against the packer itself — ignores bot and botcomponent
components in the classic solution layout. The agent, its topic and
its flow are built in the Copilot Studio portal by following this
document; a first real export of the solution can later bring them
in as committed components.

The build is done in the maker portal, signed in as the account that
created the environment.

## The agent

One agent, named **IT Support Assistant**, in the `itsupport`
solution's environment:

- **Knowledge.** Add knowledge of type Azure AI Search: the search
  service in the Sweden Central region that Terraform manages, and
  its index `kb`. Authenticate with **Entra ID Integrated** so the
  knowledge connection uses the directory rather than an admin key.
  The index holds the chunked knowledge-base articles with their
  citation URLs, ingested by `services/ingest`; answers cite the
  articles they came from.
- **Instructions.** Answer IT questions from the knowledge source,
  cite the article each answer comes from, and escalate to a ticket
  when the user asks for a human or the question is not covered by
  the knowledge base. Never guess an answer that the knowledge base
  does not support.
- **Tools.** The IT Support API custom connector (the solution's
  `itsup_ItSupportApi`), imported by the
  solution, with one action enabled at the agent level:
  `GetMyProfile` (`GET /me`), returning the signed-in caller's
  display name and mail. Ticket creation goes through the agent flow
  below, not through a direct agent action, so the flow owns the
  request shape and the response handed back to the agent.

## The Escalate to IT topic

One topic, **Escalate to IT**, in the same agent:

1. **Trigger phrases:** `escalate`, `create a ticket`, `open a
   ticket`, `I need help from IT`, `talk to a human`,
   `support ticket`.
2. **Capture the problem.** A question node asks for a one-line
   summary of the problem unless the conversation already holds one;
   the answer is stored in `Topic.ShortDescription`.
3. **Build the ticket summary** with a Set variable value node in
   Formula mode (Power Fx), storing the result in
   `Topic.TicketSummary`:

   ```powerfx
   Concatenate(
       Topic.ShortDescription,
       " — reported by ",
       Topic.UserDisplayName,
       " (", Topic.UserMail, ")"
   )
   ```

   `Topic.UserDisplayName` and `Topic.UserMail` come from the
   agent-level `GetMyProfile` action.
4. **Call the agent flow** `Create support ticket` (design below)
   with `Topic.TicketSummary` as its `summary` input; the flow's
   response is stored in `Topic.TicketNumber`.
5. **Condition** — Formula mode, `IsBlank(Topic.TicketNumber)`:
   - **True** (no ticket was created): send a message that the
     ticket could not be created and that IT has the request, and
     end the topic.
   - **False**: send an adaptive card, Message variant in Formula
     mode, schema 1.5, confirming the ticket:

     ```json
     {
       "type": "AdaptiveCard",
       "version": "1.5",
       "body": [
         {
           "type": "TextBlock",
           "size": "Medium",
           "weight": "Bolder",
           "text": "Ticket created"
         },
         {
           "type": "TextBlock",
           "text": "${Topic.TicketSummary}",
           "wrap": true
         },
         {
           "type": "FactSet",
           "facts": [
             { "title": "Ticket", "value": "${Topic.TicketNumber}" }
           ]
         }
       ]
     }
     ```

## The Create support ticket agent flow

One instant agent flow, **Create support ticket**, created from the
agent's Flows tab so it starts from the **When an agent calls the
flow** trigger:

1. **Trigger.** `When an agent calls the flow`, with one text input:
   `summary`.
2. **Action.** The IT Support API custom connector's `CreateTicket`
   operation (`POST /tickets`): `shortDescription` bound to the
   trigger's summary. Agent flows are Logic Apps workflows: the
   binding is a workflow-definition expression,
   `triggerBody()?['summary']`, not Power Fx.
3. **Respond to the agent.** Return the created ticket's `number`
   (`body('CreateTicket')?['number']`), which the topic stores in
   `Topic.TicketNumber`.

The flow contains no further logic: the API owns validation and the
ticket number form (`IT-<date>-<suffix>`), and the flow only passes
the summary in and the number out.

## Verification

The test pane checks that exercise this design end to end — a cited
knowledge answer, then an escalation ending in a ticket card — are in
the setup checklist (`docs/power-platform-setup.md`).
