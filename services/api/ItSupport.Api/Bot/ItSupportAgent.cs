using System.Security.Claims;
using ItSupport.Api.Answers;
using ItSupport.Api.Identity;
using ItSupport.Api.Tickets;
using Microsoft.Agents.Builder;
using Microsoft.Agents.Builder.App;
using Microsoft.Agents.Builder.State;
using Microsoft.Agents.Core.Models;
using Microsoft.Extensions.Logging;

namespace ItSupport.Api.Bot;

/// <summary>
/// Marks whether the bot can resolve a user profile on behalf of the activity
/// sender. True in Development (StubUserDirectory); false in cloud until
/// Teams SSO lands, where the user token from the activity feeds the existing
/// on-behalf-of plumbing. The profile intent answers gracefully when false.
/// </summary>
public sealed record BotProfileOptions(bool UserDirectoryAvailable);

/// <summary>
/// The support agent: one rule-based flow routing between knowledge answers,
/// ticket escalation, and profile lookup, with the system behaviors
/// (greeting, reset, fallback, error) hand-implemented. Routing is plain C#
/// pattern dispatch — no generative orchestration — so every branch is
/// unit-testable and the hosting layer can later swap the router for
/// model-driven orchestration without touching it.
/// </summary>
public sealed class ItSupportAgent : AgentApplication
{
    private readonly IAnswerProvider _answers;
    private readonly ITicketService _tickets;
    private readonly IUserDirectory _directory;
    private readonly BotProfileOptions _profileOptions;
    private readonly ILogger<ItSupportAgent> _logger;

    public ItSupportAgent(
        AgentApplicationOptions options,
        IAnswerProvider answers,
        ITicketService tickets,
        IUserDirectory directory,
        BotProfileOptions profileOptions,
        ILogger<ItSupportAgent> logger)
        : base(options)
    {
        _answers = answers;
        _tickets = tickets;
        _directory = directory;
        _profileOptions = profileOptions;
        _logger = logger;
        // AddRoute is the non-obsolete registration surface at 1.8.77; the
        // On* shims delegate to it. Neither route is an invoke route.
        AddRoute(IsConversationUpdate, GreetAsync);
        AddRoute(SupportIntentRouterSelector, RouteAsync);
    }

    private static Task<bool> IsConversationUpdate(ITurnContext turnContext, CancellationToken cancellationToken)
    {
        return Task.FromResult(turnContext.Activity?.Type == ActivityTypes.ConversationUpdate);
    }

    private static Task<bool> SupportIntentRouterSelector(ITurnContext turnContext, CancellationToken cancellationToken)
    {
        return Task.FromResult(turnContext.Activity?.Type == ActivityTypes.Message);
    }

    private async Task GreetAsync(ITurnContext turnContext, ITurnState turnState, CancellationToken cancellationToken)
    {
        var membersAdded = turnContext.Activity.MembersAdded ?? [];
        if (membersAdded.Any(member => member.Id != turnContext.Activity.Recipient?.Id))
        {
            await turnContext.SendActivityAsync(
                "Hi! I'm the IT support assistant. Ask me a how-to question, "
                + "say \"escalate\" to open a ticket, or ask for \"my profile\".",
                cancellationToken: cancellationToken);
        }
    }

    private async Task RouteAsync(ITurnContext turnContext, ITurnState turnState, CancellationToken cancellationToken)
    {
        var text = turnContext.Activity.Text;
        switch (SupportRouter.Route(text))
        {
            case SupportIntent.Reset:
                await OnResetAsync(turnContext, cancellationToken);
                break;
            case SupportIntent.Greeting:
                await OnGreetingAsync(turnContext, cancellationToken);
                break;
            case SupportIntent.CreateTicket:
                await OnCreateTicketAsync(turnContext, text!, cancellationToken);
                break;
            case SupportIntent.Profile:
                await OnProfileAsync(turnContext, cancellationToken);
                break;
            case SupportIntent.Knowledge:
                await OnKnowledgeAsync(turnContext, text!, cancellationToken);
                break;
            default:
                await turnContext.SendActivityAsync(
                    "I can answer IT how-to questions from the knowledge base, "
                    + "open a support ticket (start your message with \"escalate\"), "
                    + "or show your profile. How can I help?",
                    cancellationToken: cancellationToken);
                break;
        }
    }

    private async Task OnResetAsync(ITurnContext turnContext, CancellationToken cancellationToken)
    {
        // The R1 flow keeps no cross-turn state, so a reset is the explicit
        // confirmation that the conversation starts fresh; the copy matches
        // the Copilot Studio reset topic it replaces.
        await turnContext.SendActivityAsync(
            "Okay, let's start over. Ask me an IT question or say \"escalate\" to open a ticket.",
            cancellationToken: cancellationToken);
    }

    private Task OnGreetingAsync(ITurnContext turnContext, CancellationToken cancellationToken)
    {
        return turnContext.SendActivityAsync(
            "Hi! Ask me an IT how-to question, say \"escalate\" to open a ticket, "
            + "or ask for \"my profile\".",
            cancellationToken: cancellationToken);
    }

    private async Task OnKnowledgeAsync(ITurnContext turnContext, string question, CancellationToken cancellationToken)
    {
        var answer = await _answers.AnswerAsync(question, cancellationToken);
        var text = answer.Text;
        if (answer.Citations is { Count: > 0 })
        {
            var sources = string.Join(
                "\n",
                answer.Citations.Select((citation, index) =>
                    $"[{index + 1}] {citation.Title}{(string.IsNullOrEmpty(citation.Url) ? string.Empty : $" ({citation.Url})")}"));
            text = $"{text}\n\nSources:\n{sources}";
        }

        await turnContext.SendActivityAsync(text, cancellationToken: cancellationToken);
    }

    private async Task OnCreateTicketAsync(ITurnContext turnContext, string text, CancellationToken cancellationToken)
    {
        var summary = SupportRouter.TicketSummary(text);
        if (summary.Length == 0)
        {
            await turnContext.SendActivityAsync(
                "Tell me what the problem is and I'll open a ticket — for example: "
                + "\"escalate: my laptop won't boot\".",
                cancellationToken: cancellationToken);
            return;
        }

        var caller = CallerFromActivity(turnContext.Activity);
        var ticket = await _tickets.CreateAsync(caller, summary, text, cancellationToken);
        _logger.LogInformation(
            "Bot created ticket {TicketNumber} for {Caller} from activity {ActivityId}",
            ticket.Number, caller.Email, turnContext.Activity.Id);
        await turnContext.SendActivityAsync(
            TicketCard.Message(ticket), cancellationToken: cancellationToken);
    }

    private async Task OnProfileAsync(ITurnContext turnContext, CancellationToken cancellationToken)
    {
        if (!_profileOptions.UserDirectoryAvailable)
        {
            await turnContext.SendActivityAsync(
                "Profile lookup over Teams needs single sign-on, which isn't wired up yet — "
                + "until then your profile is available on the web app.",
                cancellationToken: cancellationToken);
            return;
        }

        var caller = CallerPrincipal(turnContext.Activity);
        var profile = await _directory.GetProfileAsync(caller, cancellationToken);
        var lines = new List<string> { $"{profile.DisplayName} <{profile.Email}>" };
        if (!string.IsNullOrEmpty(profile.Department))
        {
            lines.Add($"Department: {profile.Department}");
        }

        if (profile.Manager is not null)
        {
            lines.Add($"Manager: {profile.Manager.DisplayName}");
        }

        await turnContext.SendActivityAsync(
            string.Join('\n', lines), cancellationToken: cancellationToken);
    }

    /// <summary>
    /// Resolves the ticket caller from the activity sender. The
    /// channel-asserted sender fields (from.name, from.properties) are
    /// client-controllable in Direct Line and are not verified Entra
    /// claims, so nothing derived from the activity is recorded as the
    /// caller: bot-created tickets record the same guest person the
    /// unauthenticated REST endpoints use, until Teams single sign-on
    /// supplies a verified user token.
    /// </summary>
    public static Person CallerFromActivity(IActivity activity)
    {
        return Guest.Person;
    }

    private static ClaimsPrincipal CallerPrincipal(IActivity activity)
    {
        var caller = CallerFromActivity(activity);
        var claims = new List<Claim>
        {
            new(ClaimTypes.Name, caller.DisplayName),
            new(ClaimTypes.Email, caller.Email),
        };
        var identity = new ClaimsIdentity(claims, authenticationType: "Bot");
        return new ClaimsPrincipal(identity);
    }
}
