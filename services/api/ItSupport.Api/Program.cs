using System.Text;
using Azure.Data.Tables;
using Azure.Identity;
using ItSupport.Api.Answers;
using ItSupport.Api.Identity;
using ItSupport.Api.Tickets;
using Microsoft.AspNetCore.Authentication;
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
if (!string.IsNullOrEmpty(cfg["AzureAd:ClientId"]))
{
    builder.Services
        .AddMicrosoftIdentityWebApiAuthentication(cfg)
        .EnableTokenAcquisitionToCallDownstreamApi()
        .AddDownstreamApi("Graph", cfg.GetSection("Graph"))
        .AddInMemoryTokenCaches();
    builder.Services.AddScoped<IUserDirectory, GraphUserDirectory>();
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
        if (string.IsNullOrEmpty(uri))
        {
            if (dev)
            {
                uri = "http://127.0.0.1:10002/devstoreaccount1";
            }
            else
            {
                throw new InvalidOperationException(
                    "Tickets:ServiceUri is required outside Development.");
            }
        }
        return new TableTicketService(new TableServiceClient(
            new Uri(uri), new DefaultAzureCredential()));
    });
}

// Answers: configured OpenAI and Search endpoints select retrieval-augmented
// answering from the search index; the stub is Development-only; outside
// Development an answer provider must be configured.
var openAiEndpoint = cfg["OpenAI:Endpoint"];
var searchEndpoint = cfg["Search:Endpoint"];
if (!string.IsNullOrEmpty(openAiEndpoint) && !string.IsNullOrEmpty(searchEndpoint))
{
    builder.Services.AddSingleton(AzureRagAnswerProvider.CreateSearchClient(searchEndpoint));
    builder.Services.AddSingleton(AzureRagAnswerProvider.CreateChatClient(openAiEndpoint));
    builder.Services.AddSingleton<IAnswerProvider, AzureRagAnswerProvider>();
}
else if (dev && string.IsNullOrEmpty(cfg["OpenAI:Endpoint"]))
{
    builder.Services.AddSingleton<IAnswerProvider, StubAnswerProvider>();
}
else
{
    throw new InvalidOperationException("No answer provider is configured.");
}

var app = builder.Build();
app.UseDefaultFiles();
app.UseStaticFiles();
app.UseAuthentication();
app.UseAuthorization();
app.MapControllers();
app.MapFallbackToFile("index.html");
app.Run();

public partial class Program;
