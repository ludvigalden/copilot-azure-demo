namespace ItSupport.Api.Answers;

/// <summary>
/// Development-only answer provider: cites the kb/ article whose file name shares the
/// most words with the question and returns the whole article as one chunk.
/// </summary>
public sealed class StubAnswerProvider(IConfiguration configuration, IHostEnvironment environment) : IAnswerProvider
{
    public Task<Answer> AnswerAsync(string question, CancellationToken cancellationToken = default)
    {
        var kb = Path.Combine(environment.ContentRootPath, configuration["KnowledgeBase:Path"] ?? "kb");
        var words = question.ToLowerInvariant().Split([' ', '?', '.', ','], StringSplitOptions.RemoveEmptyEntries);
        var file = Directory.EnumerateFiles(kb, "*.md")
            .MaxBy(path => Path.GetFileNameWithoutExtension(path).Split('-').Count(words.Contains))!;
        var content = File.ReadAllText(file);
        var title = File.ReadLines(file).First(line => line.StartsWith("title:", StringComparison.Ordinal))[6..].Trim();
        return Task.FromResult(new Answer
        {
            Text = $"See the knowledge-base article \"{title}\".",
            Citations = [new Citation { Title = title, Url = string.Empty }],
            Chunks = [new RetrievedChunk { Title = title, Content = content }],
        });
    }
}
