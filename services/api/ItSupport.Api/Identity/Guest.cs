namespace ItSupport.Api.Identity;

/// <summary>
/// The fixed identity served to anonymous callers on the guest-open
/// endpoints. Guests never reach the directory: their profile is static and
/// their tickets record the guest person.
/// </summary>
public static class Guest
{
    public const string Name = "Guest";
    public const string Email = "guest@example.com";

    public static Person Person => new() { DisplayName = Name, Email = Email };

    public static UserProfile Profile => new() { DisplayName = Name, Email = Email };
}
