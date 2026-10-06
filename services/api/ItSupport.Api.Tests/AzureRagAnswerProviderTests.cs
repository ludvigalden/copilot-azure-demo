using System.Net;
using System.Text;
using System.Text.Json;
using Azure;
using Azure.Core.Pipeline;
using Azure.Core.Serialization;
using Azure.Search.Documents;
using Azure.Search.Documents.Models;
using OpenAI;
using OpenAI.Chat;
using System.ClientModel;
using System.ClientModel.Primitives;
using ItSupport.Api.Answers;
using Xunit;

namespace ItSupport.Api.Tests;

public sealed class AzureRagAnswerProviderTests
{
    [Fact]
    public void ToAnswer_keepsChunksInRetrievalOrder_andCitesEachArticleOnce()
    {
        var passwordUrl = "https://github.com/acme/it-demo/blob/0123456789abcdef0123456789abcdef01234567/kb/password-reset.md";
        var documents = new List<KbDocument>
        {
            new() { Id = "password-reset-1", Title = "Password reset", Content = "First chunk.", Url = passwordUrl },
            new() { Id = "vpn-1", Title = "Connect to the VPN", Content = "VPN chunk.", Url = "https://github.com/acme/it-demo/blob/0123456789abcdef0123456789abcdef01234567/kb/vpn.md" },
            new() { Id = "password-reset-2", Title = "Password reset", Content = "Second chunk.", Url = passwordUrl },
        };

        var answer = AzureRagAnswerProvider.ToAnswer("Reset steps.", documents);

        Assert.Equal("Reset steps.", answer.Text);
        Assert.Equal(
            [("Password reset", passwordUrl), ("Connect to the VPN", "https://github.com/acme/it-demo/blob/0123456789abcdef0123456789abcdef01234567/kb/vpn.md")],
            answer.Citations.Select(citation => (citation.Title, citation.Url)));
        Assert.Equal(
            ["First chunk.", "VPN chunk.", "Second chunk."],
            answer.Chunks.Select(chunk => chunk.Content));
    }

    [Fact]
    public async Task AnswerAsync_sendsTheTokenCapToTheChatClient()
    {
        const string searchJson = """
            {"value":[{"@search.score":1,"id":"printer-1","title":"Printer","content":"Restart it.","url":"https://example.test/printer.md"}]}
            """;
        const string chatJson = """
            {"id":"stub","object":"chat.completion","created":1,"model":"stub","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"Restart it."}}],"usage":{"prompt_tokens":10,"completion_tokens":3,"total_tokens":13}}
            """;
        using var searchHandler = new CannedHandler(searchJson);
        using var chatHandler = new CannedHandler(chatJson);
        using var searchHttp = new HttpClient(searchHandler);
        using var chatHttp = new HttpClient(chatHandler);
        var search = new SearchClient(new Uri("https://search.example.test"), "kb", new AzureKeyCredential("stub"),
            new SearchClientOptions
            {
                Transport = new HttpClientTransport(searchHttp),
                Serializer = new JsonObjectSerializer(new JsonSerializerOptions(JsonSerializerDefaults.Web)),
            });
        var chat = new ChatClient("stub", new ApiKeyCredential("stub"),
            new OpenAIClientOptions { Transport = new HttpClientPipelineTransport(chatHttp) });

        var answer = await new AzureRagAnswerProvider(search, chat).AnswerAsync("Printer is offline?");

        using var request = JsonDocument.Parse(Assert.IsType<string>(chatHandler.Body));
        var wire = request.RootElement;
        var capped = wire.TryGetProperty("max_completion_tokens", out var completionTokens)
            && completionTokens.GetInt32() == AzureRagAnswerProvider.MaxOutputTokenCount;
        var legacy = wire.TryGetProperty("max_tokens", out var legacyTokens)
            && legacyTokens.GetInt32() == AzureRagAnswerProvider.MaxOutputTokenCount;
        Assert.True(capped || legacy, $"the chat request carries no output-token cap: {chatHandler.Body}");
        Assert.Equal(256, AzureRagAnswerProvider.MaxOutputTokenCount);
        Assert.Contains("Printer is offline?", chatHandler.Body);
        Assert.Contains("Restart it.", chatHandler.Body);
        Assert.Equal("Restart it.", answer.Text);
        Assert.Single(answer.Citations);
    }

    private sealed class CannedHandler(string response) : HttpMessageHandler
    {
        public string? Body { get; private set; }

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            Body = request.Content is null ? "" : await request.Content.ReadAsStringAsync(cancellationToken);
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(response, Encoding.UTF8, "application/json"),
            };
        }
    }

    [Fact]
    public void BuildSearchOptions_isHybridSemantic_withServerSideVectorization()
    {
        var question = "How do I reset a forgotten password?";

        var options = AzureRagAnswerProvider.BuildSearchOptions(question);

        Assert.Equal(SearchQueryType.Semantic, options.QueryType);
        Assert.Equal(AzureRagAnswerProvider.SemanticConfigurationName, options.SemanticSearch?.SemanticConfigurationName);
        Assert.Equal(3, options.Size);

        var query = Assert.IsType<VectorizableTextQuery>(Assert.Single(options.VectorSearch.Queries));
        Assert.Equal(question, query.Text);
        Assert.Equal(5, query.KNearestNeighborsCount);
        Assert.Contains("embedding", query.Fields);
    }
}
