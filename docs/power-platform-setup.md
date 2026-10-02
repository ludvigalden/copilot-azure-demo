# Power Platform setup checklist

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
   once the repository variables `POWER_PLATFORM_ENVIRONMENT_URL`
   (the environment's URL, uncommitted by rule) and
   `IT_SUPPORT_API_HOST` (the container app's host name) exist, the
   import job imports and publishes with the federated identity, no
   stored secret. To import by hand instead:

   ```sh
   pac solution pack --zipfile out/itsupport.zip \
     --folder apps/power-platform/solution --packagetype Unmanaged
   pac solution import --path out/itsupport.zip --publish-changes
   ```

   The connector record's full serialization is pinned by the first
   real export: if the import rejects the connector component, create
   the connector in the maker portal from the generated
   `Connectors/ItSupportApi/apiDefinition.swagger.json`, add it to the
   `itsupport` solution, export, unpack, and commit the result —
   that export is the authoritative form the hand-authored layout was
   built to match.

5. **Build the agent.** Follow `docs/copilot-studio-agent.md` in the
   maker portal. Its knowledge step adds the AI Search knowledge
   source (the Terraform-managed search service, index `kb`,
   authenticated with **Entra ID Integrated**). If the knowledge
   connection's test call fails with HTTP 403, the connection's
   first-party service principal (an enterprise application the
   connection created, listed under Enterprise applications) is
   missing read rights on the index; grant it:

   ```sh
   scope="/subscriptions/<subscription id>/resourceGroups/<resource group>"
   scope="$scope/providers/Microsoft.Search/searchServices/<search service>"
   az role assignment create \
     --assignee "<object id of the connection's enterprise application>" \
     --role "Search Index Data Reader" \
     --scope "$scope"
   ```

   Then re-run the connection test.

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
