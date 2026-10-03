using Xunit;
using Microsoft.Extensions.Hosting;
using ItSupport.Api.Identity;
using Microsoft.AspNetCore.Hosting;
using ItSupport.Api.Tickets;
using Microsoft.AspNetCore.Mvc.Testing;
using System.Net;
using System.Net.Http.Json;
using Azure.Data.Tables;
using Microsoft.Extensions.Azure;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;

namespace ItSupport.Api.Tests;

public sealed class ApiTests : IDisposable
{
    private readonly List<WebApplicationFactory<Program>> _factories = [];

    public void Dispose()
    {
        foreach (var factory in _factories)
        {
            factory.Dispose();
        }
    }

    private TestFactory Factory(
        Action<IServiceCollection>? configureServices = null,
        IDictionary<string, string?>? settings = null)
    {
        var factory = new TestFactory(configureServices, settings);
        _factories.Add(factory);
        return factory;
    }

    [Fact]
    public async Task Config_returnsNullAuth_inDevelopment()
    {
        var client = Factory().CreateClient();

        var response = await client.GetFromJsonAsync<ClientConfig>("/api/config");

        Assert.NotNull(response);
        Assert.Null(response.Auth);
    }

    [Fact]
    public void WithAzureAd_withoutBotAppId_throws()
    {
        // In Entra mode the bot's inbound-token audience comes from
        // AZURE_CLIENT_ID (or TokenValidation:Audiences); boot must fail
        // loudly rather than start with an unvalidatable bot endpoint.
        var factory = Factory(settings: new Dictionary<string, string?>
        {
            ["Environment"] = "Production",
            ["AzureAd:ClientId"] = "00000000-0000-0000-0000-0000000000aa",
            ["AzureAd:TenantId"] = "00000000-0000-0000-0000-0000000000bb",
        });

        Assert.Throws<InvalidOperationException>(factory.CreateClient);
    }

    [Fact]
    public async Task WithAzureAdAndBotAppId_configReturnsAuth_andMeWithoutTokenIs401()
    {
        // Production carries the bot app id (the container app's managed-identity
        // client id) alongside the web API's Entra registration; the bot audience
        // resolves from AZURE_CLIENT_ID when TokenValidation:Audiences is unset.
        var client = Factory(settings: new Dictionary<string, string?>
        {
            ["AzureAd:ClientId"] = "00000000-0000-0000-0000-0000000000aa",
            ["AzureAd:TenantId"] = "00000000-0000-0000-0000-0000000000bb",
            ["AZURE_CLIENT_ID"] = "00000000-0000-0000-0000-0000000000cc",
        }).CreateClient();

        var config = await client.GetFromJsonAsync<ClientConfig>("/api/config");
        Assert.NotNull(config?.Auth);
        Assert.Equal("00000000-0000-0000-0000-0000000000aa", config.Auth.ClientId);

        var me = await client.GetAsync("/api/me");
        Assert.Equal(HttpStatusCode.Unauthorized, me.StatusCode);
    }

    [Fact]
    public void Production_withoutAzureAd_throws()
    {
        var factory = Factory(settings: new Dictionary<string, string?>
        {
            ["Environment"] = "Production",
        });

        Assert.Throws<InvalidOperationException>(factory.CreateClient);
    }

    [Fact]
    public async Task PostTickets_recordsDevCaller_andReturns201()
    {
        RecordingTicketService? store = null;
        var client = Factory(services => services.AddSingleton<ITicketService>(
            sp => store = new RecordingTicketService())).CreateClient();

        var response = await client.PostAsJsonAsync("/api/tickets", new CreateTicketRequest
        {
            ShortDescription = "Laptop will not start",
            Description = "Black screen on power on.",
        });

        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        Assert.NotNull(store);
        Assert.Equal(DevCallerHandler.DevEmail, store.Caller.Email);
        Assert.Equal("Laptop will not start", store.ShortDescription);
        Assert.Matches(TicketNumber.Pattern, store.Created.Number);

        var ticket = await response.Content.ReadFromJsonAsync<Ticket>();
        Assert.NotNull(ticket);
        Assert.Equal(store.Created.Number, ticket.Number);
    }

    [Fact]
    public async Task PostAnswers_returnsCitation_fromTempKb()
    {
        var kb = Path.Combine(Path.GetTempPath(), $"it-support-tests-{Guid.NewGuid():N}");
        Directory.CreateDirectory(kb);
        try
        {
            await File.WriteAllTextAsync(Path.Combine(kb, "password-reset.md"),
                """
                ---
                title: Password reset
                ---

                ## Reset your password

                Open the account portal and choose Forgot password.
                """);

            var client = Factory(settings: new Dictionary<string, string?>
            {
                ["KnowledgeBase:Path"] = kb,
            }).CreateClient();

            var answer = await client.PostAsJsonAsync("/api/answers", new AnswerRequest
            {
                Question = "How do I reset my password?",
            }).ContinueWith(t => t.Result.Content.ReadFromJsonAsync<Answer>().Result);

            Assert.NotNull(answer);
            Assert.NotEmpty(answer.Citations);
            Assert.Equal("Password reset", answer.Citations[0].Title);
            Assert.NotEmpty(answer.Chunks);
        }
        finally
        {
            Directory.Delete(kb, recursive: true);
        }
    }

    [Fact]
    public async Task Me_returnsStubProfile()
    {
        var client = Factory().CreateClient();

        var profile = await client.GetFromJsonAsync<UserProfile>("/api/me");

        Assert.NotNull(profile);
        Assert.Equal(DevCallerHandler.DevName, profile.DisplayName);
        Assert.Equal(DevCallerHandler.DevEmail, profile.Email);
        Assert.Equal("IT", profile.Department);
        Assert.NotNull(profile.Manager);
    }
}

public sealed class TableClientShapeTests
{
    [Fact]
    public void EndpointKey_bindsToClientUri()
    {
        var config = new ConfigurationBuilder().AddInMemoryCollection(new Dictionary<string, string?>
        {
            ["Tickets:Endpoint"] = "https://acct.table.core.windows.net/",
        }).Build();
        var services = new ServiceCollection();
        services.AddAzureClients(tickets => tickets.AddTableServiceClient(config.GetSection("Tickets")));

        using var provider = services.BuildServiceProvider();
        var client = provider.GetRequiredService<TableServiceClient>();

        Assert.Equal(new Uri("https://acct.table.core.windows.net/"), client.Uri);
    }
}

/// <summary>A WebApplicationFactory whose configuration and services are set per test.</summary>
public sealed class TestFactory(
    Action<IServiceCollection>? configureServices = null,
    IDictionary<string, string?>? settings = null) : WebApplicationFactory<Program>
{
    protected override void ConfigureWebHost(IWebHostBuilder builder)
    {
        var environment = Environments.Development;
        if (settings is not null)
        {
            foreach (var (key, value) in settings)
            {
                if (key == "Environment")
                {
                    environment = value ?? environment;
                }
                else
                {
                    builder.UseSetting(key, value);
                }
            }
        }

        builder.UseEnvironment(environment);
        if (configureServices is not null)
        {
            builder.ConfigureServices(configureServices);
        }
    }
}

/// <summary>A ticket store that records the caller and returns a fixed ticket.</summary>
public sealed class RecordingTicketService : ITicketService
{
    public Person Caller { get; private set; } = new();
    public string ShortDescription { get; private set; } = string.Empty;
    public Ticket Created { get; private set; } = new();

    public Task<Ticket> CreateAsync(
        Person caller, string shortDescription, string? description, CancellationToken cancellationToken = default)
    {
        Caller = caller;
        ShortDescription = shortDescription;
        Created = new Ticket
        {
            Number = TicketNumber.New(DateTimeOffset.UtcNow),
            Url = null,
            ShortDescription = shortDescription,
            Caller = caller,
            CreatedAt = DateTimeOffset.UtcNow,
        };
        return Task.FromResult(Created);
    }
}
