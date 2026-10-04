using System.Text;
using System.Threading.RateLimiting;
using Azure.Data.Tables;
using Azure.Identity;
using ItSupport.Api.Answers;
using ItSupport.Api.Bot;
using ItSupport.Api.Identity;
using ItSupport.Api.Tickets;
using Microsoft.AspNetCore.Authentication;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.HttpOverrides;
using Microsoft.AspNetCore.RateLimiting;
using Microsoft.Agents.Hosting.AspNetCore;
using Microsoft.Extensions.Options;
using Microsoft.Identity.Web;

var builder = WebApplication.CreateBuilder(args);
var cfg = builder.Configuration;
var dev = builder.Environment.IsDevelopment();
builder.Services.AddControllers();
builder.Services.AddAuthorization();

// Identity: presence of AzureAd:ClientId selects Entra; otherwise stand-ins,
// registered only in Development, so a deployed build without configuration
// fails at startup rather than running unauthenticated.
//
// The bot app id is the container app's user-assigned managed identity client
// id, published as AZURE_CLIENT_ID; the outbound service connection mirrors it
// and no client secret exists anywhere in the bot path.
var botAppId = cfg["AZURE_CLIENT_ID"];
if (!string.IsNullOrEmpty(botAppId))
{
    cfg["Connections:ServiceConnection:Settings:ClientId"] = botAppId;
}

if (!string.IsNullOrEmpty(cfg["AzureAd:ClientId"]))
{
    builder.Services
        .AddMicrosoftIdentityWebApiAuthentication(cfg)
        .EnableTokenAcquisitionToCallDownstreamApi()
        .AddDownstreamApi("Graph", cfg.GetSection("Graph"))
        .AddInMemoryTokenCaches();

    builder.Services.AddScoped<IUserDirectory, GraphUserDirectory>();

    // Bot inbound auth: Bot Protocol JWTs validated under a dedicated scheme
    // so the SPA API's Entra scheme stays the default. The audience is the bot
    // app id (TokenValidation:Audiences overrides); the tenant follows AzureAd.
    var audiences = cfg.GetSection("TokenValidation:Audiences").Get<string[]>();
    if (audiences is not { Length: > 0 })
    {
        if (string.IsNullOrEmpty(botAppId))
        {
            throw new InvalidOperationException(
                "TokenValidation:Audiences or AZURE_CLIENT_ID is required for bot token validation.");
        }

        audiences = [botAppId];
    }

    builder.Services.AddBotAspNetAuthentication(new TokenValidationOptions
    {
        Audiences = audiences,
        TenantId = cfg["TokenValidation:TenantId"] ?? cfg["AzureAd:TenantId"],
    });
}
else if (dev)
{
    builder.Services
        .AddAuthentication(DevCallerHandler.SchemeName)
        .AddScheme<AuthenticationSchemeOptions, DevCallerHandler>(DevCallerHandler.SchemeName, null);
    builder.Services.AddSingleton<IUserDirectory, StubUserDirectory>();
}
else
{
    throw new InvalidOperationException("AzureAd:ClientId is required outside Development.");
}

// Tickets: presence of ServiceNow:InstanceUrl selects ServiceNow; otherwise
// the built-in Table Storage store (Azurite locally).
if (!string.IsNullOrEmpty(cfg["ServiceNow:InstanceUrl"]))
{
    builder.Services.Configure<ServiceNowOptions>(cfg.GetSection("ServiceNow"));
    builder.Services.AddHttpClient<ITicketService, ServiceNowTicketService>((sp, client) =>
    {
        var options = sp.GetRequiredService<IOptions<ServiceNowOptions>>().Value;
        if (!string.IsNullOrEmpty(options.Username))
        {
            var token = Convert.ToBase64String(
                Encoding.UTF8.GetBytes($"{options.Username}:{options.Password}"));
            client.DefaultRequestHeaders.Authorization = new("Basic", token);
        }
    });
}
else
{
    // Terraform publishes the table service URI under Tickets:ServiceUri.
    // The Azure client factory only maps convention-named keys (Endpoint),
    // so the client is constructed directly from the published value; the
    // registration resolves on first use, which keeps the tests hermetic:
    // they replace ITicketService before it is ever resolved.
    builder.Services.AddSingleton<ITicketService>(sp =>
    {
        var uri = cfg["Tickets:ServiceUri"];
        if (!string.IsNullOrEmpty(uri))
        {
            return new TableTicketService(new TableServiceClient(
                new Uri(uri), new DefaultAzureCredential()));
        }

        // Development without Tickets:ServiceUri targets local Azurite, which
        // authenticates with the well-known development account over
        // shared-key: Azurite does not speak Entra, and Azure.Core refuses
        // bearer tokens on non-TLS endpoints.
        if (!dev)
        {
            throw new InvalidOperationException(
                "Tickets:ServiceUri is required outside Development.");
        }

        return new TableTicketService(new TableServiceClient(
            new Uri("http://127.0.0.1:10002/devstoreaccount1"),
            new TableSharedKeyCredential(
                "devstoreaccount1",
                "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==")));
    });
}

// Answers: configured OpenAI and Search endpoints select retrieval-augmented
// answering from the search index; the stub is Development-only; outside
// Development an answer provider must be configured. The index and chat
// deployment default to the primary environment's names; a deployment
// sharing another environment's services overrides both here.
var openAiEndpoint = cfg["OpenAI:Endpoint"];
var searchEndpoint = cfg["Search:Endpoint"];
var indexName = cfg["Search:IndexName"] ?? AzureRagAnswerProvider.IndexName;
var chatDeploymentName = cfg["OpenAI:DeploymentName"] ?? AzureRagAnswerProvider.ChatDeploymentName;
if (!string.IsNullOrEmpty(openAiEndpoint) && !string.IsNullOrEmpty(searchEndpoint))
{
    var searchClient = AzureRagAnswerProvider.CreateSearchClient(searchEndpoint, indexName);
    var chatClient = AzureRagAnswerProvider.CreateChatClient(openAiEndpoint, chatDeploymentName);
    builder.Services.AddSingleton<IAnswerProvider>(
        new AzureRagAnswerProvider(searchClient, chatClient, AzureRagAnswerProvider.SemanticConfigurationNameFor(indexName)));
}
else if (dev && string.IsNullOrEmpty(cfg["OpenAI:Endpoint"]))
{
    builder.Services.AddSingleton<IAnswerProvider, StubAnswerProvider>();
}
else
{
    throw new InvalidOperationException("No answer provider is configured.");
}

// Answers, tickets, and the profile endpoint are guest-open, so all are
// rate limited per client IP: one fixed window per address, twenty requests
// per minute by default, HTTP 429 beyond it. The window is configurable so
// tests can shrink it; the controller's actions opt in under the named
// policy.
builder.Services.AddRateLimiter(limiter =>
{
    limiter.RejectionStatusCode = StatusCodes.Status429TooManyRequests;
    var permitLimit = 20;
    if (int.TryParse(cfg["RateLimit:PermitLimit"], out var permits))
    {
        permitLimit = permits;
    }

    var windowSeconds = 60.0;
    if (double.TryParse(cfg["RateLimit:WindowSeconds"], out var seconds))
    {
        windowSeconds = seconds;
    }

    limiter.AddPolicy(
        ItSupport.Api.ItSupportController.RateLimitPolicy,
        httpContext => RateLimitPartition.GetFixedWindowLimiter(
            httpContext.Connection.RemoteIpAddress?.ToString() ?? "unknown",
            _ => new FixedWindowRateLimiterOptions
            {
                PermitLimit = permitLimit,
                Window = TimeSpan.FromSeconds(windowSeconds),
            }));
});

// The Container Apps ingress terminates TLS and proxies every request, so
// the app's socket peer is always the ingress and never a caller; there is
// no pinnable ingress address, so the known-proxy lists stay empty — the
// documented Container Apps pattern — and the connection peer counts as the
// trusted proxy. ForwardLimit bounds processing to the single rightmost
// X-Forwarded-For entry, the one the ingress appended to whatever the client
// sent, so a client-supplied leftmost entry is never honored and cannot
// choose the address the rate limiter partitions on. The forwarded address
// feeds only rate-limit bucketing. The step this trusts, the ingress
// appending the caller's address to X-Forwarded-For, is documented Container
// Apps ingress behavior (Microsoft Learn) rather than a measured property of
// this deployment: no live multi-IP evidence has been collected. One residual
// gap: an in-environment peer with direct reach to the container port is
// treated as the trusted ingress by this same configuration. Per-IP
// attribution is claimed no further than that.
builder.Services.Configure<ForwardedHeadersOptions>(options =>
{
    options.ForwardedHeaders = ForwardedHeaders.XForwardedFor | ForwardedHeaders.XForwardedProto;
    options.ForwardLimit = 1;
    options.KnownIPNetworks.Clear();
    options.KnownProxies.Clear();
});

// Agent: the M365 Agents SDK hosting layer. AddAgentDefaults re-registers
// HttpClient and Controllers idempotently; AddAgent registers the agent
// (transient) with AgentApplicationOptions bound from the "AgentApplication"
// config section. In Development the profile intent is live against
// StubUserDirectory; in cloud the user token for on-behalf-of lookups arrives
// with Teams SSO, so the intent answers gracefully until then.
builder.AddAgentDefaults().AddAgent<ItSupportAgent>();
builder.Services.AddSingleton(new BotProfileOptions(UserDirectoryAvailable: dev));

var app = builder.Build();
app.UseForwardedHeaders();
app.UseDefaultFiles();
app.UseStaticFiles();
app.UseAuthentication();
app.UseAuthorization();
app.UseRateLimiter();
app.MapControllers();

// The agent endpoint: Bot Protocol activity posts, mirroring the package's
// MapAgentApplicationEndpoints body. Production requires the bot JWT scheme
// on the group; Development accepts unsigned local activity posts, which is
// how the headless dev loop drives every intent over plain HTTP.
var botEndpoints = app.MapGroup("/api/bot");
if (!dev)
{
    botEndpoints.RequireAuthorization(new AuthorizeAttribute
    {
        AuthenticationSchemes = BotAuthentication.SchemeName,
    });
}

botEndpoints.MapPost("/messages", async (
    HttpRequest request,
    HttpResponse response,
    IAgentHttpAdapter adapter,
    ItSupportAgent agent,
    CancellationToken cancellationToken) =>
{
    await adapter.ProcessAsync(request, response, agent, cancellationToken);
});

app.MapFallbackToFile("index.html");
app.Run();

public partial class Program;
