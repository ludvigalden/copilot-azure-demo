using System.Net.Http.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Options;
namespace ItSupport.Api.Tickets;

/// <summary>
/// Creates ServiceNow incidents through the Table API. Selected at startup
/// when <c>ServiceNow:InstanceUrl</c> is present; the caller comes from the
/// access token, and the email is passed as <c>caller_id</c>.
/// </summary>
public sealed class ServiceNowTicketService(
    HttpClient httpClient,
    IOptions<ServiceNowOptions> options) : ITicketService
{
    public async Task<Ticket> CreateAsync(
        Person caller,
        string shortDescription,
        string? description,
        CancellationToken cancellationToken = default)
    {
        var baseUrl = options.Value.InstanceUrl.TrimEnd('/') + "/";
        using var response = await httpClient.PostAsJsonAsync(
            $"{baseUrl}api/now/table/incident",
            new IncidentRequest
            {
                ShortDescription = shortDescription,
                Description = description,
                CallerId = caller.Email,
            },
            cancellationToken);
        response.EnsureSuccessStatusCode();

        var payload = await response.Content
            .ReadFromJsonAsync<IncidentResponse>(cancellationToken: cancellationToken)
            ?? throw new InvalidOperationException("ServiceNow returned no body.");

        return new Ticket
        {
            Number = payload.Result?.Number ?? string.Empty,
            Url = payload.Result?.SysId is { } sysId
                ? $"{baseUrl}nav_to.do?uri=incident.do%3Fsys_id%3D{Uri.EscapeDataString(sysId)}"
                : null,
            ShortDescription = shortDescription,
            Caller = caller,
            CreatedAt = DateTimeOffset.UtcNow,
        };
    }

    private sealed class IncidentRequest
    {
        [JsonPropertyName("short_description")]
        public string ShortDescription { get; set; } = string.Empty;

        [JsonPropertyName("description")]
        [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
        public string? Description { get; set; }

        [JsonPropertyName("caller_id")]
        public string? CallerId { get; set; }
    }

    private sealed class IncidentResponse
    {
        [JsonPropertyName("result")]
        public IncidentResult? Result { get; set; }
    }

    private sealed class IncidentResult
    {
        [JsonPropertyName("number")]
        public string? Number { get; set; }

        [JsonPropertyName("sys_id")]
        public string? SysId { get; set; }
    }
}
