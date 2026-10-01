using Xunit;
using ItSupport.Api.Tickets;

namespace ItSupport.Api.Tests;

public sealed class TicketNumberTests
{
    [Fact]
    public void New_matchesTheDocumentedPattern()
    {
        var number = TicketNumber.New(new DateTimeOffset(2026, 10, 1, 12, 0, 0, TimeSpan.Zero));

        Assert.Matches(TicketNumber.Pattern, number);
        Assert.StartsWith("IT-20261001-", number);
    }

    [Fact]
    public void New_usesOnlyTheCrockfordBase32Alphabet()
    {
        for (var i = 0; i < 200; i++)
        {
            var number = TicketNumber.New(DateTimeOffset.UtcNow);
            var suffix = number[^4..];
            Assert.DoesNotContain(suffix, "ILOU");
        }
    }
}
