using System.Security.Claims;
using System.Text.Encodings.Web;
using Microsoft.AspNetCore.Authentication;
using Microsoft.Extensions.Options;

namespace ItSupport.Api.Identity;

/// <summary>
/// Development-only authentication: every request is the same fixed caller.
/// Registered only when <c>AzureAd:ClientId</c> is absent and the environment
/// is Development.
/// </summary>
public sealed class DevCallerHandler(
    IOptionsMonitor<AuthenticationSchemeOptions> options,
    ILoggerFactory logger,
    UrlEncoder encoder) : AuthenticationHandler<AuthenticationSchemeOptions>(options, logger, encoder)
{
    public const string SchemeName = "DevCaller";
    public const string DevName = "Dev User";
    public const string DevEmail = "dev.user@example.com";
    public const string DevOid = "00000000-0000-0000-0000-000000000001";

    protected override Task<AuthenticateResult> HandleAuthenticateAsync()
    {
        var identity = new ClaimsIdentity(
        [
            new Claim(ClaimTypes.Name, DevName),
            new Claim("preferred_username", DevEmail),
            new Claim(ClaimTypes.NameIdentifier, DevOid),
        ], SchemeName);
        var ticket = new AuthenticationTicket(new ClaimsPrincipal(identity), SchemeName);
        return Task.FromResult(AuthenticateResult.Success(ticket));
    }
}
