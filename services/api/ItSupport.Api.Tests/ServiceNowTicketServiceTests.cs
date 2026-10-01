using Xunit;
using ItSupport.Api.Tickets;
using System.Net;
using System.Text;
using System.Text.Json;
using Microsoft.Extensions.Options;

namespace ItSupport.Api.Tests;

public sealed class ServiceNowTicketServiceTests
{
    [Fact]
    public async Task CreateAsync_postsIncident_withBasicAuth_andParsesResult()
    {
        HttpRequestMessage? seenRequest = null;
        string? seenBody = null;
        var handler = new StubHandler((request, _) =>
        {
            seenRequest = request;
            seenBody = request.Content is null ? null : request.Content.ReadAsStringAsync().Result;
            return new HttpResponseMessage(HttpStatusCode.Created)
            {
                Content = new StringContent(
                    """{"result": {"number": "INC0010001", "sys_id": "abc123def456"}}""",
                    Encoding.UTF8,
                    "application/json"),
            };
        });
        var httpClient = new HttpClient(handler)
        {
            BaseAddress = new Uri("https://dev00000.service-now.com/"),
            DefaultRequestHeaders =
            {
                Authorization = new("Basic",
                    Convert.ToBase64String(Encoding.UTF8.GetBytes("demo:secret"))),
            },
        };
        var service = new ServiceNowTicketService(
            httpClient,
            Options.Create(new ServiceNowOptions { InstanceUrl = "https://dev00000.service-now.com" }));

        var ticket = await service.CreateAsync(
            new Person { DisplayName = "Dev User", Email = "dev.user@example.com" },
            "Laptop will not start",
            "Black screen on power on.");

        Assert.NotNull(seenRequest);
        Assert.Equal(HttpMethod.Post, seenRequest.Method);
        Assert.Equal(
            new Uri("https://dev00000.service-now.com/api/now/table/incident"),
            seenRequest.RequestUri);
        Assert.Equal("Basic", seenRequest.Headers.Authorization?.Scheme);

        Assert.NotNull(seenBody);
        using var body = JsonDocument.Parse(seenBody);
        Assert.Equal("Laptop will not start", body.RootElement.GetProperty("short_description").GetString());
        Assert.Equal("Black screen on power on.", body.RootElement.GetProperty("description").GetString());
        Assert.Equal("dev.user@example.com", body.RootElement.GetProperty("caller_id").GetString());

        Assert.Equal("INC0010001", ticket.Number);
        Assert.Contains("abc123def456", ticket.Url);
        Assert.Equal("Laptop will not start", ticket.ShortDescription);
        Assert.Equal("dev.user@example.com", ticket.Caller.Email);
    }

    [Fact]
    public async Task CreateAsync_withoutDescription_omitsItFromBody()
    {
        string? seenBody = null;
        var handler = new StubHandler((request, _) =>
        {
            seenBody = request.Content is null ? null : request.Content.ReadAsStringAsync().Result;
            return new HttpResponseMessage(HttpStatusCode.Created)
            {
                Content = new StringContent(
                    """{"result": {"number": "INC0010002", "sys_id": "sys99"}}""",
                    Encoding.UTF8,
                    "application/json"),
            };
        });
        var service = new ServiceNowTicketService(
            new HttpClient(handler) { BaseAddress = new Uri("https://dev00000.service-now.com/") },
            Options.Create(new ServiceNowOptions { InstanceUrl = "https://dev00000.service-now.com" }));

        var ticket = await service.CreateAsync(
            new Person { DisplayName = "Dev User", Email = "dev.user@example.com" },
            "Printer offline",
            null);

        Assert.NotNull(seenBody);
        using var body = JsonDocument.Parse(seenBody);
        Assert.False(body.RootElement.TryGetProperty("description", out _));
        Assert.Equal("INC0010002", ticket.Number);
    }

    private sealed class StubHandler(
        Func<HttpRequestMessage, CancellationToken, HttpResponseMessage> respond)
        : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken)
        {
            return Task.FromResult(respond(request, cancellationToken));
        }
    }
}
