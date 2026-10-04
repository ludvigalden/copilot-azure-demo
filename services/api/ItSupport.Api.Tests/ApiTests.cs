using Xunit;
using Microsoft.Extensions.Hosting;
using ItSupport.Api.Identity;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.HttpOverrides;
using ItSupport.Api.Tickets;
using Microsoft.AspNetCore.Mvc.Testing;
using System.Net;
using System.Net.Http.Json;
using Azure.Data.Tables;
using Microsoft.Extensions.Azure;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;

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

    /// <summary>
    /// The Entra-mode settings: the web API's registration plus the bot app id
    /// the inbound bot audience resolves from. Requests without a token reach
    /// the API unauthenticated, which is what the guest branches serve.
    /// </summary>
    private static Dictionary<string, string?> EntraSettings() => new()
    {
        ["AzureAd:ClientId"] = "00000000-0000-0000-0000-0000000000aa",
        ["AzureAd:TenantId"] = "00000000-0000-0000-0000-0000000000bb",
        ["AZURE_CLIENT_ID"] = "00000000-0000-0000-0000-0000000000cc",
    };

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
    public async Task WithAzureAdAndBotAppId_configReturnsAuth_andMeWithoutTokenIsGuest()
    {
        // Production carries the bot app id (the container app's managed-identity
        // client id) alongside the web API's Entra registration; the bot audience
        // resolves from AZURE_CLIENT_ID when TokenValidation:Audiences is unset.
        // Sign-in is optional: without a token the profile endpoint answers the
        // guest profile.
        var client = Factory(settings: EntraSettings()).CreateClient();

        var config = await client.GetFromJsonAsync<ClientConfig>("/api/config");
        Assert.NotNull(config?.Auth);
        Assert.Equal("00000000-0000-0000-0000-0000000000aa", config.Auth.ClientId);

        var profile = await client.GetFromJsonAsync<UserProfile>("/api/me");
        Assert.NotNull(profile);
        Assert.Equal(Guest.Name, profile.DisplayName);
        Assert.Equal(Guest.Email, profile.Email);
    }

    [Fact]
    public async Task Guest_inEntraMode_getsAnswers()
    {
        var kb = await WriteTempKbAsync();
        try
        {
            var settings = EntraSettings();
            settings["KnowledgeBase:Path"] = kb;
            var client = Factory(settings: settings).CreateClient();

            var response = await client.PostAsJsonAsync("/api/answers", new AnswerRequest
            {
                Question = "How do I reset my password?",
            });

            Assert.Equal(HttpStatusCode.OK, response.StatusCode);
            var answer = await response.Content.ReadFromJsonAsync<Answer>();
            Assert.NotNull(answer);
            Assert.NotEmpty(answer.Citations);
            Assert.Equal("Password reset", answer.Citations[0].Title);
        }
        finally
        {
            Directory.Delete(kb, recursive: true);
        }
    }

    [Fact]
    public async Task Guest_inEntraMode_createsTicket_withGuestCaller()
    {
        RecordingTicketService? store = null;
        var client = Factory(
            services => services.AddSingleton<ITicketService>(sp => store = new RecordingTicketService()),
            settings: EntraSettings()).CreateClient();

        var response = await client.PostAsJsonAsync("/api/tickets", new CreateTicketRequest
        {
            ShortDescription = "Wifi is down",
        });

        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        Assert.NotNull(store);
        Assert.Equal(Guest.Name, store.Caller.DisplayName);
        Assert.Equal(Guest.Email, store.Caller.Email);

        var ticket = await response.Content.ReadFromJsonAsync<Ticket>();
        Assert.NotNull(ticket);
        Assert.Equal(Guest.Name, ticket.Caller.DisplayName);
    }

    [Fact]
    public async Task InvalidBearerToken_isRejectedOnGuestEndpoints()
    {
        // No token is a guest, but a token that was presented and fails
        // validation is a 401 rather than a silent downgrade to guest.
        var client = Factory(settings: EntraSettings()).CreateClient();
        client.DefaultRequestHeaders.Authorization = new("Bearer", "aaa.bbb.ccc");

        var me = await client.GetAsync("/api/me");
        var answers = await client.PostAsJsonAsync("/api/answers", new AnswerRequest { Question = "x" });
        var tickets = await client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "x" });

        Assert.Equal(HttpStatusCode.Unauthorized, me.StatusCode);
        Assert.Equal(HttpStatusCode.Unauthorized, answers.StatusCode);
        Assert.Equal(HttpStatusCode.Unauthorized, tickets.StatusCode);
    }

    [Fact]
    public async Task EmptyMalformedOrForeignCredentials_areRejectedOnGuestEndpoints()
    {
        // Any Authorization header that fails to authenticate is a 401, never
        // a silent downgrade to guest: an empty bearer value, whitespace, a
        // scheme-less bare value, and a foreign scheme all present intent to
        // authenticate and all failed.
        var client = Factory(settings: EntraSettings()).CreateClient();

        foreach (var credential in new[] { "Bearer", "Bearer ", "Bearer   ", "Basic dXNlcjpwYXNz" })
        {
            client.DefaultRequestHeaders.Authorization = new(
                credential.Split(' ')[0],
                credential.Split(' ').Length > 1 ? credential.Split(' ')[1] : null);

            var me = await client.GetAsync("/api/me");
            Assert.Equal(HttpStatusCode.Unauthorized, me.StatusCode);

            var answers = await client.PostAsJsonAsync("/api/answers", new AnswerRequest { Question = "x" });
            Assert.Equal(HttpStatusCode.Unauthorized, answers.StatusCode);

            var tickets = await client.PostAsJsonAsync(
                "/api/tickets", new CreateTicketRequest { ShortDescription = "x" });
            Assert.Equal(HttpStatusCode.Unauthorized, tickets.StatusCode);
        }
    }

    [Fact]
    public async Task RateLimit_returns429_beyondLimit_andPartitionsByForwardedAddress()
    {
        RecordingTicketService? store = null;
        var client = Factory(
            services => services.AddSingleton<ITicketService>(sp => store = new RecordingTicketService()),
            settings: new Dictionary<string, string?>
            {
                ["RateLimit:PermitLimit"] = "1",
                ["RateLimit:WindowSeconds"] = "60",
            }).CreateClient();

        var first = await client.PostAsJsonAsync("/api/tickets", new CreateTicketRequest { ShortDescription = "one" });
        var second = await client.PostAsJsonAsync("/api/tickets", new CreateTicketRequest { ShortDescription = "two" });

        Assert.Equal(HttpStatusCode.Created, first.StatusCode);
        Assert.Equal(HttpStatusCode.TooManyRequests, second.StatusCode);

        // The forwarded headers middleware restores the caller's address, so a
        // different X-Forwarded-For is a different rate-limit bucket.
        var forwarded = new HttpRequestMessage(HttpMethod.Post, "/api/tickets")
        {
            Content = JsonContent.Create(new CreateTicketRequest { ShortDescription = "three" }),
        };
        forwarded.Headers.Add("X-Forwarded-For", "203.0.113.20");
        var third = await client.SendAsync(forwarded);

        Assert.Equal(HttpStatusCode.Created, third.StatusCode);
    }

    [Fact]
    public async Task SpoofedForwardedEntry_ignored_beyondTheTrustedHop()
    {
        // One hop is processed: the rightmost X-Forwarded-For entry, the one
        // the ingress appends to whatever the client sent. A client-supplied
        // leftmost entry cannot choose the bucketing address, and anonymous
        // requests are limited exactly like authenticated ones — this test
        // runs in Entra mode, so every request below is a guest.
        RecordingTicketService? store = null;
        var client = Factory(
            services => services.AddSingleton<ITicketService>(sp => store = new RecordingTicketService()),
            settings: EntraSettings().Concat(new Dictionary<string, string?>
            {
                ["RateLimit:PermitLimit"] = "1",
                ["RateLimit:WindowSeconds"] = "60",
            }).ToDictionary(kvp => kvp.Key, kvp => kvp.Value)).CreateClient();

        var spoofed = new HttpRequestMessage(HttpMethod.Post, "/api/tickets")
        {
            Content = JsonContent.Create(new CreateTicketRequest { ShortDescription = "spoof" }),
        };
        spoofed.Headers.Add("X-Forwarded-For", "203.0.113.66, 203.0.113.20");
        var first = await client.SendAsync(spoofed);

        Assert.Equal(HttpStatusCode.Created, first.StatusCode);
        Assert.Equal(Guest.Email, store!.Caller.Email);

        // A different leftmost entry over the same rightmost address shares
        // the exhausted bucket: only the rightmost entry was ever honored.
        var repeat = new HttpRequestMessage(HttpMethod.Post, "/api/tickets")
        {
            Content = JsonContent.Create(new CreateTicketRequest { ShortDescription = "repeat" }),
        };
        repeat.Headers.Add("X-Forwarded-For", "198.51.100.1, 203.0.113.20");
        var second = await client.SendAsync(repeat);

        Assert.Equal(HttpStatusCode.TooManyRequests, second.StatusCode);
    }

    [Fact]
    public void ForwardedHeaders_areRestoredFromTheIngress()
    {
        var factory = Factory();

        var options = factory.Services.GetRequiredService<IOptions<ForwardedHeadersOptions>>().Value;

        Assert.Equal(ForwardedHeaders.XForwardedFor | ForwardedHeaders.XForwardedProto, options.ForwardedHeaders);
        // The ingress address is not pinnable, so no proxy is trusted by
        // address; the empty lists make the middleware process the headers.
        Assert.Empty(options.KnownIPNetworks);
        Assert.Empty(options.KnownProxies);
        // A single hop is processed: the rightmost entry, the one the nearest
        // proxy appended. Client-supplied leftmost entries are never honored.
        Assert.Equal(1, options.ForwardLimit);
    }

    [Fact]
    public void JwtBearer_audienceValidation_isLeftToMicrosoftIdentityWeb()
    {
        // The controlled precondition for accepting v2 tokens: no audience is
        // preset on the API scheme and Microsoft.Identity.Web's v2-aware
        // audience validator is installed, so token audience defaults to the
        // client id. A controlled test cannot present a real token; that is
        // the post-deploy retest.
        var factory = Factory(settings: EntraSettings());
        factory.CreateClient();

        var options = factory.Services.GetRequiredService<IOptionsMonitor<JwtBearerOptions>>().Get("Bearer");

        Assert.Null(options.Audience);
        Assert.Null(options.TokenValidationParameters.ValidAudience);
        Assert.Null(options.TokenValidationParameters.ValidAudiences);
        Assert.NotNull(options.TokenValidationParameters.AudienceValidator);
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
    /// <summary>A one-article knowledge base in a fresh temp directory, for the stub answer provider.</summary>
    private static async Task<string> WriteTempKbAsync()
    {
        var kb = Path.Combine(Path.GetTempPath(), $"it-support-tests-{Guid.NewGuid():N}");
        Directory.CreateDirectory(kb);
        await File.WriteAllTextAsync(Path.Combine(kb, "password-reset.md"),
            """
            ---
            title: Password reset
            ---

            ## Reset your password

            Open the account portal and choose Forgot password.
            """);
        return kb;
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
