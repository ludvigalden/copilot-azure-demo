using System.Security.Claims;

namespace ItSupport.Api.Identity;

public interface IUserDirectory
{
    Task<UserProfile> GetProfileAsync(ClaimsPrincipal caller, CancellationToken cancellationToken = default);
}
