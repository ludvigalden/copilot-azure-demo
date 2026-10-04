# Power Platform setup checklist

> **Secondary path.** The primary agent runs in code and needs none
> of this setup — see [docs/agent.md](agent.md). This checklist covers
> the banked Power Platform deployment path only: the solution under
> `apps/power-platform/` still packs, imports and publishes through
> its pipeline, and this is its setup record.

One-time setup of the Power Platform side, done signed in as **the
account that created the environment** (the environment-creating
account; no other account has verified rights inside it). Public
tooling is named; everything else is described by what it is, not by
internal names.

1. **Sign in once for the CLI.** With `pac` installed (a .NET global
   tool: `dotnet tool install --global Microsoft.PowerApps.CLI.Tool`),
   create the one-time authentication profile for the environment:

   ```sh
   pac auth create --url https://<org>.crm17.dynamics.com
   ```

   The `<org>` host is the environment's URL from the admin center;
   it is deliberately not written down in this repository.

2. **Attach a billing plan (licensing).** In the Azure portal, Power
   Platform → billing policies: create a pay-as-you-go plan bound to
   the subscription, and add the environment to it. This is what
   licenses publishing to Microsoft Teams; the agent's **test pane
   works without it**, so nothing below is blocked by this step.

3. **Register the deploy pipeline as an application user.** In the
   admin center, the environment → Settings → Users + permissions →
   Application users: add the app registration the deploy workflows
   authenticate as (the one behind the `AZURE_CLIENT_ID` repository
   variable) with the security role **System Administrator** or
   **System Customizer**. Without this, the workflow's federated
   token is not a user in Dataverse and solution import will fail.

4. **Import the solution.** Preferred: let CI do it —
   `power-platform.yml` packs the solution on every pull request, and
   once `POWER_PLATFORM_ENVIRONMENT_URL` (the environment's URL,
   uncommitted by rule) exists both as a repository variable and on
   the deploy environment — the import job's gate can only see the
   repository-level copy — the import job imports and publishes with
   the federated identity, no stored secret. The job resolves the
   host the connector should call itself — the container app's host
   name, or the public hostname once that secret exists — and after
   importing it queries the environment for the connector record and
   prints it in the job log. To import by hand instead:

   ```sh
   pac solution pack --zipfile out/itsupport.zip \
     --folder apps/power-platform/solution --packagetype Unmanaged
   pac solution import --path out/itsupport.zip --publish-changes
   ```

   The connector record's full serialization is pinned by the first
   real export: if the import rejects the connector component, create
   the connector in the maker portal from the generated
   `Connector/itsup_ItSupportApi_openapidefinition.json`, add it to the
   `itsupport` solution, export, unpack, and commit the result —
   that export is the authoritative form the hand-authored layout was
   built to match.

5. **Build the agent.** Follow `docs/copilot-studio-agent.md` in the
   maker portal; this checklist gives the order and the failure modes.

   - **Knowledge.** Add the AI Search knowledge source (the
     Terraform-managed search service, index `kb`, authenticated with
     **Entra ID Integrated**). If the knowledge connection's test call
     fails with HTTP 403, the connection's first-party service
     principal (an enterprise application the connection created,
     listed under Enterprise applications) is missing read rights on
     the index; grant it:

     ```sh
     scope="/subscriptions/<subscription id>/resourceGroups/<resource group>"
     scope="$scope/providers/Microsoft.Search/searchServices/<search service>"
     az role assignment create \
       --assignee "<object id of the connection's enterprise application>" \
       --role "Search Index Data Reader" \
       --scope "$scope"
     ```

     Then re-run the connection test.

   - **The connector.** The solution import registers the IT Support
     API custom connector (`itsup_ItSupportApi`) in the environment.
     In the agent's
     **Tools** pane, add a tool and pick it from the custom
     connectors; if the agent was built before the import ran,
     refresh the connector list first. Adding it asks for a
     connection: sign in and consent. The consent screen names the
     connector's dedicated application registration (the one the
     infrastructure created for it) requesting access to the API on
     the signed-in user's behalf; if consent is not offered, grant
     admin consent for that application's API permissions in the
     Azure portal and create the connection again.

   - **The actions and the flow.** Enable the agent-level
     `GetMyProfile` action from the connector, and build the
     `Escalate to IT` topic and the `Create support ticket` agent
     flow exactly as `docs/copilot-studio-agent.md` specifies; ticket
     creation goes through the flow, which calls the connector's
     `CreateTicket` operation. With generative orchestration on, a
     turn can then choose between answering from the knowledge
     source, running the escalation topic, and calling the profile
     action.

   - **Publish.** Once the agent answers in the test pane, publish it
     to Microsoft Teams. This is the step the billing plan from step
     2 licenses; without the plan the publish check fails while the
     test pane keeps working.

6. **Verify in the test pane.** Two conversations exercise the whole
   chain:

   - Ask how to set up MFA, or how to change a password. Expect an
     answer that cites the knowledge-base article it came from.
   - Escalate: say you need help from IT. Expect a ticket card
     showing a ticket number on the form `IT-<date>-<suffix>` and the
     one-line summary built by the topic.

The agent design and the exact topic, action and flow definitions are
in `docs/copilot-studio-agent.md`; the committed solution layout and
its pipeline are described in the repository README.
