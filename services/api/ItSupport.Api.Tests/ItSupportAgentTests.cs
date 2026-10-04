using Xunit;
using System.Security.Claims;
using System.Text.Json;
using System.Text.Json.Nodes;
using ItSupport.Api.Answers;
using ItSupport.Api.Bot;
using ItSupport.Api.Identity;
using ItSupport.Api.Tickets;
using Microsoft.Agents.Builder;
using Microsoft.Agents.Builder.App;
using Microsoft.Agents.Builder.Testing;
using Microsoft.Agents.Core.Models;
using Microsoft.Extensions.Logging.Abstractions;

namespace ItSupport.Api.Tests;

/// <summary>
/// Handler-behavior tests for the support agent, driven through TestAdapter
/// exactly as a Bot Protocol activity post would arrive: one turn in, the
/// agent's reply activities out of the adapter queue.
/// </summary>
public sealed class ItSupportAgentTests
{
    [Fact]
    public async Task KnowledgeQuestion_answersWithText_andCitation()
    {
        var agent = BuildAgent();

        var reply = await SendAsync(agent, "How do I fix the office printer?");

        Assert.Contains(FixedAnswerProvider.AnswerText, reply.Text);
        Assert.Contains(
            $"[1] {FixedAnswerProvider.CitationTitle} ({FixedAnswerProvider.CitationUrl})",
            reply.Text);
    }

    [Fact]
    public async Task Escalation_createsTicket_andRepliesWithAdaptiveCard()
    {
        var store = new RecordingTicketService();
        var agent = BuildAgent(tickets: store);

        var reply = await SendAsync(agent, "escalate: my laptop won't boot");

        Assert.Equal("my laptop won't boot", store.ShortDescription);
        Assert.Equal(Guest.Name, store.Caller.DisplayName);
        Assert.Equal(Guest.Email, store.Caller.Email);

        Assert.NotNull(reply.Attachments);
        var attachment = Assert.Single(reply.Attachments!);
        Assert.Equal("application/vnd.microsoft.card.adaptive", attachment.ContentType);
        var card = Assert.IsType<JsonObject>(attachment.Content);
        Assert.Equal("IT ticket created", (string?)card["body"]![0]!["text"]);
        Assert.Equal(store.Created.Number, (string?)card["body"]![2]!["facts"]![0]!["value"]);
        Assert.Equal(Guest.Name, (string?)card["body"]![2]!["facts"]![1]!["value"]);
    }

    [Fact]
    public async Task EscalationWithoutSummary_promptsInsteadOfCreating()
    {
        var store = new RecordingTicketService();
        var agent = BuildAgent(tickets: store);

        var reply = await SendAsync(agent, "escalate");

        Assert.Null(store.Created.Number);
        Assert.Contains("escalate: my laptop won't boot", reply.Text);
    }

    [Fact]
    public async Task ProfileIntent_repliesWithProfile_whenDirectoryAvailable()
    {
        var agent = BuildAgent(userDirectoryAvailable: true);

        var reply = await SendAsync(agent, "my details");

        Assert.Contains("Dev Caller <dev.user@example.com>", reply.Text);
        Assert.Contains("Department: IT", reply.Text);
        Assert.Contains("Manager: Alex Manager", reply.Text);
    }

    [Fact]
    public async Task ProfileIntent_repliesGracefully_whenDirectoryUnavailable()
    {
        var agent = BuildAgent(userDirectoryAvailable: false);

        var reply = await SendAsync(agent, "my details");

        Assert.Contains("single sign-on", reply.Text);
    }

    [Fact]
    public async Task GreetingMessage_repliesWithGreeting()
    {
        var agent = BuildAgent();

        var reply = await SendAsync(agent, "hello");

        Assert.Contains("how-to question", reply.Text);
    }

    [Fact]
    public async Task ResetMessage_repliesWithResetCopy()
    {
        var agent = BuildAgent();

        var reply = await SendAsync(agent, "reset");

        Assert.Contains("start over", reply.Text);
    }

    [Fact]
    public async Task EmptyMessage_repliesWithFallbackCopy()
    {
        var agent = BuildAgent();

        var reply = await SendAsync(agent, "");

        Assert.Contains("open a support ticket", reply.Text);
    }

    [Fact]
    public async Task ConversationUpdate_greetsWhenUserJoins()
    {
        var agent = BuildAgent();

        var reply = await SendConversationUpdateAsync(
            agent,
            new ChannelAccount(id: "user1", name: "User1"),
            new ChannelAccount(id: "bot", name: "Bot"));

        Assert.NotNull(reply);
        Assert.Contains("IT support assistant", reply.Text);
    }

    [Fact]
    public async Task ConversationUpdate_staysSilent_whenOnlyAgentAdded()
    {
        var agent = BuildAgent();

        var reply = await SendConversationUpdateAsync(
            agent,
            new ChannelAccount(id: "bot", name: "Bot"));

        Assert.Null(reply);
    }

    [Fact]
    public void CallerFromActivity_ignoresForgedChannelIdentity()
    {
        var activity = new Activity
        {
            From = new ChannelAccount(id: "aad-123", name: "Forged Caller"),
        };
        activity.From.Properties["email"] = JsonSerializer.SerializeToElement("ceo@example.com");

        var caller = ItSupportAgent.CallerFromActivity(activity);

        Assert.Equal(Guest.Name, caller.DisplayName);
        Assert.Equal(Guest.Email, caller.Email);
        Assert.NotEqual("ceo@example.com", caller.Email);
        Assert.NotEqual("Forged Caller", caller.DisplayName);
    }

    [Fact]
    public void CallerFromActivity_recordsGuestWithoutChannelIdentity()
    {
        var activity = new Activity
        {
            From = new ChannelAccount(id: "aad-456", name: "Other Caller"),
        };

        var caller = ItSupportAgent.CallerFromActivity(activity);

        Assert.Equal(Guest.Name, caller.DisplayName);
        Assert.Equal(Guest.Email, caller.Email);
    }

    private static ItSupportAgent BuildAgent(
        IAnswerProvider? answers = null,
        ITicketService? tickets = null,
        IUserDirectory? directory = null,
        bool userDirectoryAvailable = true)
    {
        return new ItSupportAgent(
            new AgentApplicationOptions(
                (Microsoft.Agents.Storage.IStorage)null!, NullLoggerFactory.Instance),
            answers ?? new FixedAnswerProvider(),
            tickets ?? new RecordingTicketService(),
            directory ?? new FixedUserDirectory(),
            new BotProfileOptions(UserDirectoryAvailable: userDirectoryAvailable),
            NullLogger<ItSupportAgent>.Instance);
    }

    private static async Task<IActivity> SendAsync(ItSupportAgent agent, string text)
    {
        var adapter = new TestAdapter();
        var activity = adapter.MakeActivity(text);
        await adapter.ProcessActivityAsync(
            activity,
            (turnContext, cancellationToken) => agent.OnTurnAsync(turnContext, cancellationToken),
            CancellationToken.None);
        var reply = adapter.GetNextReply();
        Assert.NotNull(reply);
        return reply;
    }

    private static async Task<IActivity?> SendConversationUpdateAsync(
        ItSupportAgent agent, params ChannelAccount[] membersAdded)
    {
        var adapter = new TestAdapter();
        var activity = adapter.MakeActivity(string.Empty);
        activity.Type = ActivityTypes.ConversationUpdate;
        activity.MembersAdded = [.. membersAdded];
        await adapter.ProcessActivityAsync(
            activity,
            (turnContext, cancellationToken) => agent.OnTurnAsync(turnContext, cancellationToken),
            CancellationToken.None);
        return adapter.GetNextReply();
    }

    private sealed class FixedAnswerProvider : IAnswerProvider
    {
        public const string AnswerText = "Restart the print spooler service.";
        public const string CitationTitle = "Print troubleshooting";
        public const string CitationUrl = "https://kb.example/print";

        public Task<Answer> AnswerAsync(string question, CancellationToken cancellationToken = default)
        {
            return Task.FromResult(new Answer
            {
                Text = AnswerText,
                Citations = [new Citation { Title = CitationTitle, Url = CitationUrl }],
            });
        }
    }

    private sealed class FixedUserDirectory : IUserDirectory
    {
        public Task<UserProfile> GetProfileAsync(
            ClaimsPrincipal caller, CancellationToken cancellationToken = default)
        {
            return Task.FromResult(new UserProfile
            {
                DisplayName = "Dev Caller",
                Email = "dev.user@example.com",
                Department = "IT",
                Manager = new Person { DisplayName = "Alex Manager", Email = "alex.m@example.com" },
            });
        }
    }
}
