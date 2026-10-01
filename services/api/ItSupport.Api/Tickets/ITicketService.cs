namespace ItSupport.Api.Tickets;

public interface ITicketService
{
    Task<Ticket> CreateAsync(
        Person caller, string shortDescription, string? description, CancellationToken cancellationToken = default);
}
