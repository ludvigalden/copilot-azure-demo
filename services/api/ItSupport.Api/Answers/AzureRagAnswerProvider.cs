using System.Text.Json;
using Azure.AI.OpenAI;
using Azure.Core.Serialization;
using Azure.Identity;
using Azure.Search.Documents;
using Azure.Search.Documents.Models;
using OpenAI.Chat;

namespace ItSupport.Api.Answers;

/// <summary>
/// Answers questions from the knowledge-base chunks stored in the Azure AI
/// Search index: hybrid retrieval (full-text plus a server-vectorized text
/// query, so no embedding is computed in the API) with semantic ranking,
/// followed by a chat completion grounded in the retrieved chunk texts.
/// </summary>
/// <remarks>
/// Semantic ranking needs the search service to support it: on the free SKU
/// the query is rejected with 400 until the service is upgraded, even though
/// the index itself carries the semantic configuration.
/// </remarks>
public sealed class AzureRagAnswerProvider(SearchClient search, ChatClient chat) : IAnswerProvider
{
    public const string IndexName = "kb";
    public const string ChatDeploymentName = "chat";
    public const string SemanticConfigurationName = "kb-semantic";
    private const int RetrievedChunks = 3;
    private const int VectorNeighbors = 5;

    public static SearchClient CreateSearchClient(string endpoint) =>
        new(
            new Uri(endpoint),
            IndexName,
            new DefaultAzureCredential(),
            new SearchClientOptions(SearchClientOptions.ServiceVersion.V2024_07_01)
            {
                // The index stores camelCase field names; Web defaults read
                // them into plain PascalCase records.
                Serializer = new JsonObjectSerializer(new JsonSerializerOptions(JsonSerializerDefaults.Web)),
            });

    public static ChatClient CreateChatClient(string endpoint) =>
        new AzureOpenAIClient(new Uri(endpoint), new DefaultAzureCredential())
            .GetChatClient(ChatDeploymentName);

    public async Task<Answer> AnswerAsync(string question, CancellationToken cancellationToken = default)
    {
        var documents = await RetrieveAsync(question, cancellationToken).ConfigureAwait(false);

        var sources = string.Join(
            "\n\n",
            documents.Select((document, index) => $"Source {index + 1} ({document.Title}):\n{document.Content}"));

        var completion = await chat
            .CompleteChatAsync(
                [
                    new SystemChatMessage(
                        """
                        You answer IT-support questions for company employees.
                        Answer only from the sources provided by the user, in at most four sentences.
                        If the sources do not cover the question, say that the knowledge base has no answer.
                        """),
                    new UserChatMessage($"Sources:\n\n{sources}\n\nQuestion: {question}"),
                ],
                cancellationToken: cancellationToken)
            .ConfigureAwait(false);

        return ToAnswer(TextOf(completion.Value), documents);
    }

    private static string TextOf(ChatCompletion completion) => string.Concat(
        completion.Content
            .Where(part => part.Kind == ChatMessageContentPartKind.Text)
            .Select(part => part.Text));

    public static SearchOptions BuildSearchOptions(string question)
    {
        var options = new SearchOptions
        {
            QueryType = SearchQueryType.Semantic,
            SemanticSearch = new SemanticSearchOptions
            {
                SemanticConfigurationName = SemanticConfigurationName,
            },
            Size = RetrievedChunks,
            VectorSearch = new VectorSearchOptions(),
        };
        options.VectorSearch.Queries.Add(
            new VectorizableTextQuery(question)
            {
                // The service embeds the text through the index's vectorizer,
                // so the API never calls the embedding deployment itself.
                KNearestNeighborsCount = VectorNeighbors,
                Fields = { "embedding" },
            });
        return options;
    }

    public static Answer ToAnswer(string text, IReadOnlyList<KbDocument> documents)
    {
        var citations = new List<Citation>();
        foreach (var document in documents)
        {
            if (citations.All(citation => citation.Url != document.Url))
            {
                citations.Add(new Citation { Title = document.Title, Url = document.Url });
            }
        }

        return new Answer
        {
            Text = text,
            Citations = citations,
            Chunks = [.. documents.Select(document => new RetrievedChunk
            {
                Title = document.Title,
                Content = document.Content,
            })],
        };
    }

    private async Task<IReadOnlyList<KbDocument>> RetrieveAsync(
        string question,
        CancellationToken cancellationToken)
    {
        var response = await search
            .SearchAsync<KbDocument>(question, BuildSearchOptions(question), cancellationToken)
            .ConfigureAwait(false);

        var documents = new List<KbDocument>();
        await foreach (var result in response.Value.GetResultsAsync().WithCancellation(cancellationToken))
        {
            documents.Add(result.Document);
        }

        return documents;
    }
}

/// <summary>A knowledge-base chunk as stored in the search index.</summary>
public sealed record KbDocument
{
    public string Id { get; init; } = "";

    public string Title { get; init; } = "";

    public string Content { get; init; } = "";

    public string Url { get; init; } = "";
}
