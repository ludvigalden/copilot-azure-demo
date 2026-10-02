using Azure.Search.Documents.Models;
using ItSupport.Api.Answers;
using Xunit;

namespace ItSupport.Api.Tests;

public sealed class AzureRagAnswerProviderTests
{
    [Fact]
    public void ToAnswer_keepsChunksInRetrievalOrder_andCitesEachArticleOnce()
    {
        var passwordUrl = "https://github.com/acme/it-demo/blob/main/kb/password-reset.md";
        var documents = new List<KbDocument>
        {
            new() { Id = "password-reset-1", Title = "Password reset", Content = "First chunk.", Url = passwordUrl },
            new() { Id = "vpn-1", Title = "Connect to the VPN", Content = "VPN chunk.", Url = "https://github.com/acme/it-demo/blob/main/kb/vpn.md" },
            new() { Id = "password-reset-2", Title = "Password reset", Content = "Second chunk.", Url = passwordUrl },
        };

        var answer = AzureRagAnswerProvider.ToAnswer("Reset steps.", documents);

        Assert.Equal("Reset steps.", answer.Text);
        Assert.Equal(
            [("Password reset", passwordUrl), ("Connect to the VPN", "https://github.com/acme/it-demo/blob/main/kb/vpn.md")],
            answer.Citations.Select(citation => (citation.Title, citation.Url)));
        Assert.Equal(
            ["First chunk.", "VPN chunk.", "Second chunk."],
            answer.Chunks.Select(chunk => chunk.Content));
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
