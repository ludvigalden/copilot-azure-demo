using Xunit;
using ItSupport.Api.Answers;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.FileProviders;
using Microsoft.Extensions.Hosting;

namespace ItSupport.Api.Tests;

public sealed class StubAnswerProviderTests
{
    [Fact]
    public async Task AnswerAsync_citesArticleMatchingQuestion()
    {
        var kb = Directory.CreateTempSubdirectory("it-support-tests-").FullName;
        try
        {
            await File.WriteAllTextAsync(Path.Combine(kb, "printer.md"), "---\ntitle: Add a printer\n---\n");
            await File.WriteAllTextAsync(Path.Combine(kb, "vpn.md"), "---\ntitle: Connect to the VPN\n---\n");
            var configuration = new ConfigurationBuilder()
                .AddInMemoryCollection(new Dictionary<string, string?> { ["KnowledgeBase:Path"] = kb })
                .Build();

            var answer = await new StubAnswerProvider(configuration, new TestEnvironment())
                .AnswerAsync("I cannot reach the vpn");

            var citation = Assert.Single(answer.Citations);
            Assert.Equal("Connect to the VPN", citation.Title);
            Assert.DoesNotContain(kb, citation.Url);
            Assert.Contains("Connect to the VPN", Assert.Single(answer.Chunks).Content);
        }
        finally
        {
            Directory.Delete(kb, recursive: true);
        }
    }

    private sealed class TestEnvironment : IHostEnvironment
    {
        public string ApplicationName { get; set; } = "test";
        public IFileProvider ContentRootFileProvider { get; set; } = new NullFileProvider();
        public string ContentRootPath { get; set; } = "/";
        public string EnvironmentName { get; set; } = "Development";
    }
}
