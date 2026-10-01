using System.Text;
using Azure.Identity;
using ItSupport.Api.Answers;
using ItSupport.Api.Identity;
using ItSupport.Api.Tickets;
using Microsoft.AspNetCore.Authentication;
using Microsoft.Extensions.Azure;
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
    builder.Services.AddAzureClients(tickets =>
    {
        var section = cfg.GetSection("Tickets");
        if (!string.IsNullOrEmpty(section.Value))
        {
            tickets.AddTableServiceClient(section.Value);
        }
        else
        {
            tickets.AddTableServiceClient(section);
        }

        tickets.UseCredential(new DefaultAzureCredential());
    });
    builder.Services.AddSingleton<ITicketService, TableTicketService>();
}

// Answers: the stub is Development-only.
// above this.
if (dev && string.IsNullOrEmpty(cfg["OpenAI:Endpoint"]))
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
