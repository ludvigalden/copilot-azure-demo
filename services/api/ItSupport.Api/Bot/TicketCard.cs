using System.Text.Json.Nodes;
using ItSupport.Api.Tickets;
using Microsoft.Agents.Core.Models;

namespace ItSupport.Api.Bot;

/// <summary>
/// Builds the ticket-confirmation Adaptive Card shown when an escalation
/// creates a ticket, carrying the IT-&lt;date&gt;-&lt;suffix&gt; number the
/// caller references afterwards. Card JSON is assembled directly; no card
/// object model is needed for a single card shape.
/// </summary>
public static class TicketCard
{
    public static JsonObject Build(Ticket ticket)
    {
        var facts = new JsonArray
        {
            new JsonObject { ["title"] = "Ticket", ["value"] = ticket.Number },
            new JsonObject { ["title"] = "Caller", ["value"] = ticket.Caller.DisplayName },
        };

        if (!string.IsNullOrEmpty(ticket.Url))
        {
            facts.Add(new JsonObject { ["title"] = "Link", ["value"] = ticket.Url });
        }

        return new JsonObject
        {
            ["type"] = "AdaptiveCard",
            ["version"] = "1.4",
            ["body"] = new JsonArray
            {
                new JsonObject
                {
                    ["type"] = "TextBlock",
                    ["size"] = "Medium",
                    ["weight"] = "Bolder",
                    ["text"] = "IT ticket created",
                },
                new JsonObject
                {
                    ["type"] = "TextBlock",
                    ["text"] = ticket.ShortDescription,
                    ["wrap"] = true,
                },
                new JsonObject { ["type"] = "FactSet", ["facts"] = facts },
            },
        };
    }

    /// <summary>
    /// The message activity carrying the card, ready for SendActivityAsync.
    /// </summary>
    public static IActivity Message(Ticket ticket)
    {
        var attachment = new Attachment
        {
            ContentType = "application/vnd.microsoft.card.adaptive",
            Content = Build(ticket),
        };
        return MessageFactory.Attachment([attachment], inputHint: InputHints.AcceptingInput);
    }
}
