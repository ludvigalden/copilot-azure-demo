using System.Security.Claims;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Identity.Abstractions;

namespace ItSupport.Api.Identity;

/// <summary>
/// Reads the caller's profile from Microsoft Graph on behalf of the caller
/// (on-behalf-of flow with delegated <c>User.Read</c>).
/// </summary>
public sealed class GraphUserDirectory(IDownstreamApi downstreamApi) : IUserDirectory
{
    public async Task<UserProfile> GetProfileAsync(ClaimsPrincipal caller, CancellationToken cancellationToken = default)
    {
        var me = await downstreamApi.GetForUserAsync<GraphMe>("Graph", options =>
        {
            options.RelativePath =
                "me?$select=displayName,mail,department&$expand=manager($select=displayName,mail)";
        }, caller, cancellationToken);

        if (me is null)
        {
            throw new InvalidOperationException("Graph returned no profile.");
        }

        return new UserProfile
        {
            DisplayName = me.DisplayName ?? string.Empty,
            Email = me.Mail ?? string.Empty,
            Department = me.Department,
            Manager = me.Manager is null ? null : new Person
            {
                DisplayName = me.Manager.DisplayName ?? string.Empty,
                Email = me.Manager.Mail ?? string.Empty,
            },
        };
    }

    private sealed class GraphMe
    {
        [JsonPropertyName("displayName")]
        public string? DisplayName { get; set; }

        [JsonPropertyName("mail")]
        public string? Mail { get; set; }

        [JsonPropertyName("department")]
        public string? Department { get; set; }

        [JsonPropertyName("manager")]
        public GraphMe? Manager { get; set; }
    }
}
