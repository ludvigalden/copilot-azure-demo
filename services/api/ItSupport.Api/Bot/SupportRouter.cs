namespace ItSupport.Api.Bot;

/// <summary>
/// The intents the support agent routes between, mirroring the Copilot Studio
/// topic structure this flow replaces: knowledge answering, ticket escalation,
/// profile lookup, and the system behaviors (greeting, reset, fallback).
/// </summary>
public enum SupportIntent
{
    Knowledge,
    CreateTicket,
    Profile,
    Greeting,
    Reset,
    Fallback,
}

/// <summary>
/// Deterministic, rule-based intent matching over message text. No generative
/// orchestration: each rule is a plain pattern, so routing is fully
/// unit-testable and the knowledge path stays the only LLM-backed surface.
/// </summary>
public static class SupportRouter
{
    private static readonly string[] Greetings = ["hi", "hello", "hey", "hej", "good morning", "good afternoon"];

    private static readonly string[] TicketTriggers =
    [
        "escalate",
        "open a ticket",
        "create a ticket",
        "new ticket",
        "raise a ticket",
        "file a ticket",
        "report a problem",
        "it support",
    ];

    private static readonly string[] ProfileTriggers =
    [
        "my profile",
        "who am i",
        "my manager",
        "my department",
        "my details",
    ];

    public static SupportIntent Route(string? text)
    {
        if (string.IsNullOrWhiteSpace(text))
        {
            return SupportIntent.Fallback;
        }

        var trimmed = text.Trim().TrimEnd('?', '!', '.');
        var lowered = trimmed.ToLowerInvariant();

        if (lowered is "reset" or "/reset" or "start over")
        {
            return SupportIntent.Reset;
        }

        if (Greetings.Contains(lowered))
        {
            return SupportIntent.Greeting;
        }

        if (TicketTriggers.Any(trigger => lowered.Contains(trigger)))
        {
            return SupportIntent.CreateTicket;
        }

        if (ProfileTriggers.Any(trigger => lowered.Contains(trigger)))
        {
            return SupportIntent.Profile;
        }

        if (trimmed.StartsWith('/'))
        {
            return SupportIntent.Fallback;
        }

        return SupportIntent.Knowledge;
    }

    /// <summary>
    /// Strips the ticket trigger phrase from the message, leaving the short
    /// description to file the ticket under; empty when nothing is left.
    /// </summary>
    public static string TicketSummary(string? text)
    {
        var lowered = (text ?? string.Empty).ToLowerInvariant();
        var trigger = TicketTriggers.FirstOrDefault(lowered.Contains);
        if (trigger == null)
        {
            return (text ?? string.Empty).Trim();
        }

        var index = lowered.IndexOf(trigger, StringComparison.Ordinal);
        var before = text![..index];
        var after = text[(index + trigger.Length)..].TrimStart(':', '-', ' ');
        var summary = $"{before}{after}".Trim();
        return summary.Length == 0 ? string.Empty : summary;
    }
}
