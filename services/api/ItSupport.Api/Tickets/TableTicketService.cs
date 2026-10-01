using Azure;
using Azure.Data.Tables;

namespace ItSupport.Api.Tickets;

/// <summary>
/// The built-in ticket store: one Azure Storage table named <c>tickets</c>.
/// The table is created on first use through a lazy handle rather than at
/// startup, which keeps the WebApplicationFactory tests hermetic without
/// Azurite; the effect is the same idempotent create. PartitionKey is
/// yyyyMMdd, RowKey is the ticket number, and a 409 conflict is retried with
/// a fresh number up to five times.
/// </summary>
public sealed class TableTicketService(TableServiceClient tables) : ITicketService
{
    public const string TableName = "tickets";
    private const int MaxAttempts = 5;

    private readonly Lazy<Task<TableClient>> _table = new(
        async () =>
        {
            var client = tables.GetTableClient(TableName);
            await client.CreateIfNotExistsAsync();
            return client;
        },
        LazyThreadSafetyMode.ExecutionAndPublication);

    public async Task<Ticket> CreateAsync(
        Person caller,
        string shortDescription,
        string? description,
        CancellationToken cancellationToken = default)
    {
        var table = await _table.Value.ConfigureAwait(false);
        var createdAt = DateTimeOffset.UtcNow;

        for (var attempt = 1; ; attempt++)
        {
            var number = TicketNumber.New(createdAt);
            var entity = new TableEntity(createdAt.ToString("yyyyMMdd"), number)
            {
                ["ShortDescription"] = shortDescription,
                ["Description"] = description ?? string.Empty,
                ["CallerName"] = caller.DisplayName,
                ["CallerEmail"] = caller.Email,
                ["CreatedAt"] = createdAt.ToString("O"),
            };
            try
            {
                await table.AddEntityAsync(entity, cancellationToken);
                return new Ticket
                {
                    Number = number,
                    Url = null,
                    ShortDescription = shortDescription,
                    Caller = caller,
                    CreatedAt = createdAt,
                };
            }
            catch (RequestFailedException ex) when (ex.Status == 409 && attempt < MaxAttempts)
            {
                // A fresh number is drawn on the next iteration.
            }
        }
    }
}
