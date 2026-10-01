namespace ItSupport.Api.Tickets;

/// <summary>
/// Ticket numbers on the form IT-yyyyMMdd-XXXX, where XXXX is four random
/// characters from the Crockford base32 alphabet (no I, L, O or U).
/// </summary>
public static class TicketNumber
{
    /// <summary>The documented pattern; not enforced as a schema pattern.</summary>
    public const string Pattern = "^IT-\\d{8}-[0-9A-HJKMNP-TV-Z]{4}$";

    private const string Alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

    public static string New(DateTimeOffset date)
    {
        Span<char> suffix = stackalloc char[4];
        for (var i = 0; i < suffix.Length; i++)
        {
            suffix[i] = Alphabet[Random.Shared.Next(Alphabet.Length)];
        }

        return $"IT-{date:yyyyMMdd}-{new string(suffix)}";
    }
}
