using System.Globalization;

namespace ItSupport.Api.Answers;

/// <summary>
/// Development-only answer provider: keyword-matches the question against the
/// titles of the committed kb/ articles and returns a fixed sentence with a
/// citation and the article's sections as chunks. Registered only when
/// <c>OpenAI:Endpoint</c> is absent and the environment is Development.
/// </summary>
public sealed class StubAnswerProvider(
    IConfiguration configuration,
    IHostEnvironment environment) : IAnswerProvider
{
    public Task<Answer> AnswerAsync(string question, CancellationToken cancellationToken = default)
    {
        var root = ResolveKbRoot();
        var best = EnumerateArticles(root, rootUrlPrefix())
            .Select(article => (article, score: Score(article.Title, question)))
            .Where(pair => pair.score > 0)
            .OrderByDescending(pair => pair.score)
            .FirstOrDefault();
        Answer answer;
        if (best.article is null)
        {
            answer = new Answer
            {
                Text = "I could not find anything in the knowledge base for that question.",
                Citations = [],
                Chunks = [],
            };
        }
        else
        {
            answer = new Answer
            {
                Text = $"Here is what the knowledge base says about \"{best.article.Title}\".",
                Citations =
                [
                    new Citation { Title = best.article.Title, Url = best.article.Url },
                ],
                Chunks = [.. best.article.Sections
                    .Select(section => new RetrievedChunk
                    {
                        Title = best.article.Title,
                        Content = section,
                    })],
            };
        }

        return Task.FromResult(answer);
    }

    private string ResolveKbRoot()
    {
        var configured = configuration["KnowledgeBase:Path"] ?? "kb";
        return Path.IsPathRooted(configured)
            ? configured
            : Path.GetFullPath(Path.Combine(environment.ContentRootPath, configured));
    }

    /// <summary>The POSIX form of the configured path, used as the citation URL prefix.</summary>
    private string rootUrlPrefix() =>
        (configuration["KnowledgeBase:Path"] ?? "kb").Replace('\\', '/').TrimEnd('/');

    private static IEnumerable<KbArticle> EnumerateArticles(string root, string urlPrefix)
    {
        if (!Directory.Exists(root))
        {
            yield break;
        }

        foreach (var file in Directory.EnumerateFiles(root, "*.md"))
        {
            var article = Load(file, root, urlPrefix);
            if (article is not null)
            {
                yield return article;
            }
        }
    }

    private static KbArticle? Load(string file, string root, string urlPrefix)
    {
        var lines = File.ReadAllLines(file);
        string? title = null;
        List<string> sections = [];
        string? currentSection = null;

        for (var i = 0; i < lines.Length; i++)
        {
            var line = lines[i];
            if (title is null)
            {
                if (line.StartsWith("---", StringComparison.Ordinal))
                {
                    for (i++; i < lines.Length && !lines[i].StartsWith("---", StringComparison.Ordinal); i++)
                    {
                        if (lines[i].StartsWith("title:", StringComparison.Ordinal))
                        {
                            title = lines[i]["title:".Length..].Trim().Trim('"');
                        }
                    }
                }
                else if (line.StartsWith("# ", StringComparison.Ordinal))
                {
                    title = line[2..].Trim();
                }
            }
            else if (line.StartsWith("## ", StringComparison.Ordinal))
            {
                currentSection = line[3..].Trim();
                sections.Add(currentSection);
            }
            else if (currentSection is not null && line.Length > 0)
            {
                sections[^1] = sections[^1] + "\n" + line;
            }
        }

        if (title is null)
        {
            return null;
        }

        var relative = Path.GetRelativePath(root, file).Replace(Path.DirectorySeparatorChar, '/');
        var url = $"{urlPrefix}/{relative}";
        return new KbArticle(title, url, [.. sections]);
    }

    private static int Score(string title, string question)
    {
        var titleWords = Words(title).ToHashSet();
        return Words(question).Count(titleWords.Contains);
    }

    private static IEnumerable<string> Words(string text) =>
        text.Split([' ', '-', ',', '.', '?', '!', ':', ';'], StringSplitOptions.RemoveEmptyEntries)
            .Select(word => word.ToLower(CultureInfo.InvariantCulture))
            .Where(word => word.Length > 2);

    private sealed record KbArticle(string Title, string Url, string[] Sections);
}
