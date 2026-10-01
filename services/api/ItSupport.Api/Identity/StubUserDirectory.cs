using System.Security.Claims;

namespace ItSupport.Api.Identity;

public sealed class StubUserDirectory : IUserDirectory
{
    public Task<UserProfile> GetProfileAsync(ClaimsPrincipal caller, CancellationToken cancellationToken = default)
    {
        return Task.FromResult(new UserProfile
        {
            DisplayName = DevCallerHandler.DevName,
            Email = DevCallerHandler.DevEmail,
            Department = "IT",
            Manager = new Person
            {
                DisplayName = "Alex Manager",
                Email = "alex.manager@example.com",
            },
        });
    }
}
