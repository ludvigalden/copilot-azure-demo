using Xunit;
using ItSupport.Api.Answers;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.FileProviders;
using Microsoft.Extensions.Hosting;

namespace ItSupport.Api.Tests;

public sealed class StubAnswerProviderTests : IDisposable
{
    private readonly string _kb = Path.Combine(Path.GetTempPath(), $"it-support-tests-{Guid.NewGuid():N}");

    public void Dispose()
    {
        if (Directory.Exists(_kb))
        {
            Directory.Delete(_kb, recursive: true);
        }
    }

    [Fact]
    public async Task AnswerAsync_matchesByTitle_andReturnsChunks()
    {
        Directory.CreateDirectory(_kb);
        await File.WriteAllTextAsync(Path.Combine(_kb, "vpn.md"),
            """
            ---
            title: VPN access
            ---

            ## Connect to the VPN

            Start the VPN client and sign in.
            """);
        var provider = NewProvider();

        var answer = await provider.AnswerAsync("I cannot reach the vpn");

        Assert.Equal("VPN access", answer.Citations.Single().Title);
        Assert.EndsWith("vpn.md", answer.Citations.Single().Url);
        Assert.NotEmpty(answer.Chunks);
        Assert.Contains("VPN client", answer.Chunks.Single().Content);
    }

    [Fact]
    public async Task AnswerAsync_withoutMatch_returnsNoCitations()
    {
        Directory.CreateDirectory(_kb);
        await File.WriteAllTextAsync(Path.Combine(_kb, "vpn.md"),
            """
            ---
            title: VPN access
            ---

            ## Connect to the VPN

            Start the VPN client and sign in.
            """);
        var provider = NewProvider();

        var answer = await provider.AnswerAsync("what is the canteen menu");

        Assert.Empty(answer.Citations);
        Assert.Empty(answer.Chunks);
        Assert.Contains("could not find", answer.Text);
    }

    [Fact]
    public async Task AnswerAsync_withMissingKbDirectory_returnsNoCitations()
    {
        var provider = NewProvider();

        var answer = await provider.AnswerAsync("vpn");

        Assert.Empty(answer.Citations);
    }

    private StubAnswerProvider NewProvider()
    {
        var configuration = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?> { ["KnowledgeBase:Path"] = _kb })
            .Build();
        return new StubAnswerProvider(configuration, new TestEnvironment("/"));
    }

    private sealed class TestEnvironment(string contentRoot) : IHostEnvironment
    {
        public string ApplicationName { get; set; } = "test";
        public IFileProvider ContentRootFileProvider { get; set; } = new NullFileProvider();
        public string ContentRootPath { get; set; } = contentRoot;
        public string EnvironmentName { get; set; } = "Development";
    }
}
