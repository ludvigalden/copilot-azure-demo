using System.Security.Claims;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.RateLimiting;

namespace ItSupport.Api;

[ApiController]
[Authorize]
public sealed class ItSupportController(
    Tickets.ITicketService tickets,
    Identity.IUserDirectory directory,
    Answers.IAnswerProvider answers,
    IConfiguration configuration) : ItSupportControllerBase
{
    /// <summary>The rate-limiting policy applied to the guest-open endpoints.</summary>
    public const string RateLimitPolicy = "client-ip";

    // Sign-in is optional, but credentials that were presented and failed
    // authentication must fail loudly: no Authorization header means a guest,
    // a valid token the caller's identity, and any header that failed to
    // authenticate — a malformed, empty, whitespace, or non-bearer credential
    // — a 401 challenge rather than a silent downgrade to guest. A token that
    // authenticated without the delegated access_as_user scope is challenged
    // the same way: an app-only roles token, or a delegated token naming an
    // unrelated scope, is not a credential this API accepts.
    private bool PresentedCredentialsAreInvalid()
    {
        return Request.Headers.Authorization.Count > 0
            && (User.Identity?.IsAuthenticated != true || !HasDelegatedScope(User));
    }

    // Delegated access requires the access_as_user scope, read from the
    // token's scp claim; the claim surfaces either under its short name or
    // under the identity-claim URI depending on the handler's inbound claim
    // mapping, so both spellings are read.
    private static bool HasDelegatedScope(ClaimsPrincipal user)
    {
        var scopes = user.FindFirst("scp")?.Value
            ?? user.FindFirst("http://schemas.microsoft.com/identity/claims/scope")?.Value;
        return scopes?.Split(' ', StringSplitOptions.RemoveEmptyEntries)
            .Contains("access_as_user", StringComparer.Ordinal) == true;
    }

    [AllowAnonymous]
    public override Task<ActionResult<ClientConfig>> GetConfig(CancellationToken cancellationToken = default)
    {
        var clientId = configuration["AzureAd:ClientId"];
        var tenantId = configuration["AzureAd:TenantId"];
        ClientConfig config;
        if (string.IsNullOrEmpty(clientId) || string.IsNullOrEmpty(tenantId))
        {
            config = new ClientConfig { Auth = null };
        }
        else
        {
            // The scope must match the app registration's application ID URI.
            // Terraform publishes that URI, so the deployed configuration
            // carries the exact string; the default covers local development.
            var scope = configuration["AzureAd:Scope"];
            config = new ClientConfig
            {
                Auth = new AuthConfig
                {
                    ClientId = clientId,
                    TenantId = tenantId,
                    Scope = string.IsNullOrEmpty(scope)
                        ? $"api://{clientId}/access_as_user"
                        : scope,
                },
            };
        }

        return Task.FromResult<ActionResult<ClientConfig>>(Ok(config));
    }

    // Sign-in is optional: a signed-in caller gets their directory profile
    // read on their behalf, an anonymous caller the guest profile. The
    // endpoint is guest-open, so it is rate limited like the other
    // guest-open endpoints.
    [AllowAnonymous]
    [EnableRateLimiting(RateLimitPolicy)]
    public override async Task<ActionResult<UserProfile>> GetMyProfile(CancellationToken cancellationToken = default)
    {
        if (PresentedCredentialsAreInvalid())
        {
            return Challenge();
        }

        return User.Identity?.IsAuthenticated == true
            ? Ok(await directory.GetProfileAsync(User, cancellationToken))
            : Ok(Identity.Guest.Profile);
    }

    // Guest-open: a signed-in caller is answered as themselves, an anonymous
    // caller as a guest, and either way the endpoint answers.
    [AllowAnonymous]
    [EnableRateLimiting(RateLimitPolicy)]
    public override async Task<ActionResult<Answer>> AnswerQuestion(
        [FromBody] AnswerRequest body, CancellationToken cancellationToken = default)
    {
        if (PresentedCredentialsAreInvalid())
        {
            return Challenge();
        }

        return Ok(await answers.AnswerAsync(body.Question, cancellationToken));
    }

    // Guest-open: the caller is the signed-in identity when a valid token is
    // presented and a guest otherwise — never a value from the request body.
    [AllowAnonymous]
    [EnableRateLimiting(RateLimitPolicy)]
    public override async Task<ActionResult<Ticket>> CreateTicket(
        [FromBody] CreateTicketRequest body, CancellationToken cancellationToken = default)
    {
        if (PresentedCredentialsAreInvalid())
        {
            return Challenge();
        }

        var caller = User.Identity?.IsAuthenticated == true
            ? CallerFromClaims()
            : Identity.Guest.Person;
        var ticket = await tickets.CreateAsync(caller, body.ShortDescription, body.Description, cancellationToken);
        return StatusCode(StatusCodes.Status201Created, ticket);
    }

    private Person CallerFromClaims()
    {
        var email = User.FindFirstValue("preferred_username")
            ?? throw new InvalidOperationException("The caller has no preferred_username claim.");
        var name = User.FindFirstValue("name") ?? User.FindFirstValue(ClaimTypes.Name) ?? email;
        return new Person { DisplayName = name, Email = email };
    }
}
