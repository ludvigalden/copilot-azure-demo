namespace ItSupport.Api.Tickets;

public sealed class ServiceNowOptions
{
    public string InstanceUrl { get; set; } = string.Empty;

    public string? Username { get; set; }

    public string? Password { get; set; }
}
