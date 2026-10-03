using Xunit;
using ItSupport.Api.Bot;

namespace ItSupport.Api.Tests;

/// <summary>
/// Unit tests for the rule-based intent router: the plain trigger-phrase
/// dispatch that mirrors the Copilot Studio topic structure this flow
/// replaces. Every mapping here is deterministic.
/// </summary>
public sealed class SupportRouterTests
{
    [Theory]
    [InlineData(null, SupportIntent.Fallback)]
    [InlineData("", SupportIntent.Fallback)]
    [InlineData("   ", SupportIntent.Fallback)]
    [InlineData("reset", SupportIntent.Reset)]
    [InlineData("/reset", SupportIntent.Reset)]
    [InlineData("start over", SupportIntent.Reset)]
    [InlineData("  RESET?  ", SupportIntent.Reset)]
    [InlineData("hi", SupportIntent.Greeting)]
    [InlineData("Hello", SupportIntent.Greeting)]
    [InlineData("hej", SupportIntent.Greeting)]
    [InlineData("good morning", SupportIntent.Greeting)]
    [InlineData("escalate: my laptop won't boot", SupportIntent.CreateTicket)]
    [InlineData("please escalate this", SupportIntent.CreateTicket)]
    [InlineData("I need to open a ticket for the printer", SupportIntent.CreateTicket)]
    [InlineData("report a problem with my monitor", SupportIntent.CreateTicket)]
    [InlineData("show my profile", SupportIntent.Profile)]
    [InlineData("who am i?", SupportIntent.Profile)]
    [InlineData("my details please", SupportIntent.Profile)]
    [InlineData("who is my manager", SupportIntent.Profile)]
    [InlineData("How do I connect to the VPN?", SupportIntent.Knowledge)]
    [InlineData("The VPN client keeps failing silently", SupportIntent.Knowledge)]
    [InlineData("/clear-screen", SupportIntent.Fallback)]
    [InlineData("escalate my profile", SupportIntent.CreateTicket)]
    public void Route_mapsTextToExpectedIntent(string? text, SupportIntent expected)
    {
        Assert.Equal(expected, SupportRouter.Route(text));
    }

    [Theory]
    [InlineData("escalate: my laptop won't boot", "my laptop won't boot")]
    [InlineData("please escalate this", "please this")]
    [InlineData("escalate", "")]
    [InlineData("escalate:", "")]
    [InlineData("report a problem with my monitor", "with my monitor")]
    [InlineData("How do I connect to the VPN?", "How do I connect to the VPN?")]
    [InlineData(null, "")]
    public void TicketSummary_stripsTrigger_andKeepsRest(string? text, string expected)
    {
        Assert.Equal(expected, SupportRouter.TicketSummary(text));
    }
}
