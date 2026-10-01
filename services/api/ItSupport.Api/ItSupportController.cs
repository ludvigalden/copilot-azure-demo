using System.Security.Claims;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;

namespace ItSupport.Api;

[ApiController]
[Authorize]
public sealed class ItSupportController(
    Tickets.ITicketService tickets,
    Identity.IUserDirectory directory,
    Answers.IAnswerProvider answers,
    IConfiguration configuration) : ItSupportControllerBase
{
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
            config = new ClientConfig
            {
                Auth = new AuthConfig
                {
                    ClientId = clientId,
                    TenantId = tenantId,
                    Scope = $"api://{clientId}/access_as_user",
                },
            };
        }

        return Task.FromResult<ActionResult<ClientConfig>>(Ok(config));
    }

    public override async Task<ActionResult<UserProfile>> GetMyProfile(CancellationToken cancellationToken = default)
    {
        return Ok(await directory.GetProfileAsync(User, cancellationToken));
    }

    public override async Task<ActionResult<Answer>> AnswerQuestion(
        [FromBody] AnswerRequest body, CancellationToken cancellationToken = default)
    {
        return Ok(await answers.AnswerAsync(body.Question, cancellationToken));
    }

    public override async Task<ActionResult<Ticket>> CreateTicket(
        [FromBody] CreateTicketRequest body, CancellationToken cancellationToken = default)
    {
        var caller = CallerFromClaims();
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
