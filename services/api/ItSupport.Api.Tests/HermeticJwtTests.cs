using System.Net;
using System.Net.Http.Json;
using System.Security.Claims;
using System.Security.Cryptography;
using ItSupport.Api.Bot;
using ItSupport.Api.Identity;
using ItSupport.Api.Tickets;
using Microsoft.AspNetCore.Authentication;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.HttpOverrides;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.AspNetCore.TestHost;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using Microsoft.IdentityModel.JsonWebTokens;
using Microsoft.IdentityModel.Protocols;
using Microsoft.IdentityModel.Protocols.OpenIdConnect;
using Microsoft.IdentityModel.Tokens;
using Xunit;

namespace ItSupport.Api.Tests;

/// <summary>
/// Hermetic JWT regression suite: presents locally minted RS256 access
/// tokens to the real "Bearer" JwtBearer handler that Program.cs builds in
/// Entra mode, through real HTTP calls. The key material and the OIDC
/// metadata source are swapped for local stand-ins; everything else, i.e.
/// Microsoft.Identity.Web's v2 audience validator, the framework's issuer,
/// lifetime, and signature validation, and the controller's identity and
/// scope gating, runs exactly as configured in the app.
///
/// The fixture deltas:
///   1. JwtBearerOptions.ConfigurationManager is replaced with a
///      StaticConfigurationManager whose Issuer is the configured tenant's
///      v2.0 issuer, so no network metadata fetch ever runs.
///   2. TokenValidationParameters.IssuerSigningKey is set to a local RSA key,
///      making it the only candidate signing key.
///   3. IUserDirectory is replaced with a fixed profile so the authenticated
///      /api/me branch needs no Graph call.
///   4. TokenValidationParameters.ValidIssuers is pinned to the configured
///      tenant's v2.0 issuer, because the retained AadIssuerValidator would
///      otherwise fall back to live metadata for the nonexistent fixture
///      tenant; the delegate itself still runs and still decides.
///   5. In the bot AllowedCallers test, the scheme's ABS metadata-switching
///      event is replaced (it would fetch live Entra metadata) while its
///      OnTokenValidated enforcement, the logic under test, is kept.
/// Local signatures are not live-Entra proof; they make the validation and
/// gating logic exercised here repeatable offline.
/// </summary>
public sealed class HermeticJwtTests : IDisposable
{
    private const string ClientId = "00000000-0000-0000-0000-0000000000aa";
    private const string TenantId = "00000000-0000-0000-0000-0000000000bb";
    private const string BotAppId = "00000000-0000-0000-0000-0000000000cc";
    private const string KnownCallerAppId = "00000000-0000-0000-0000-0000000000dd";
    private const string ForeignCallerAppId = "00000000-0000-0000-0000-0000000000ff";
    private const string TenantIssuer = "https://login.microsoftonline.com/" + TenantId + "/v2.0";
    private const string ForeignIssuer = "https://login.microsoftonline.com/99999999-9999-9999-9999-999999999999/v2.0";
    private const string ForeignAudience = "11111111-2222-3333-4444-555555555555";

    private readonly RSA _signingKey = RSA.Create(2048);
    private readonly RSA _otherKey = RSA.Create(2048);
    private readonly List<WebApplicationFactory<Program>> _factories = [];

    public void Dispose()
    {
        foreach (var factory in _factories)
        {
            factory.Dispose();
        }
    }

    private TestFactory Factory(
        Action<IServiceCollection>? configureServices = null,
        IDictionary<string, string?>? settings = null)
    {
        var factory = new TestFactory(configureServices, settings);
        _factories.Add(factory);
        return factory;
    }

    private static Dictionary<string, string?> EntraSettings() => new()
    {
        ["AzureAd:ClientId"] = ClientId,
        ["AzureAd:TenantId"] = TenantId,
        ["AZURE_CLIENT_ID"] = BotAppId,
    };

    /// <summary>
    /// Swaps the metadata source and the signing key for hermetic stand-ins
    /// after the named options have been warmed (configure + postconfigure
    /// have run); the mutation lands on the cached instance the handler reads.
    /// </summary>
    private void UseHermeticKeys(WebApplicationFactory<Program> factory)
    {
        var options = factory.Services
            .GetRequiredService<IOptionsMonitor<JwtBearerOptions>>()
            .Get("Bearer");

        var metadata = new OpenIdConnectConfiguration { Issuer = TenantIssuer };
        metadata.SigningKeys.Add(new RsaSecurityKey(_signingKey));
        options.ConfigurationManager = new StaticConfigurationManager<OpenIdConnectConfiguration>(metadata);
        options.TokenValidationParameters.IssuerSigningKey = new RsaSecurityKey(_signingKey);
        // Delta 4: the MIW-installed AadIssuerValidator delegate is retained,
        // but its metadata fallback would fetch live OIDC metadata for the
        // fixture tenant (which does not exist). Pinning ValidIssuers to the
        // configured tenant's v2.0 issuer keeps the delegate deciding — it
        // accepts exactly this issuer and rejects any other — without the
        // network round trip.
        options.TokenValidationParameters.ValidIssuers = [TenantIssuer];
    }

    /// <summary>An Entra-mode API whose ticket store and directory are fixed.</summary>
    private ApiHandle NewApi(IDictionary<string, string?>? extraSettings = null)
    {
        var handle = new ApiHandle();
        var settings = extraSettings is null
            ? EntraSettings()
            : EntraSettings().Concat(extraSettings).ToDictionary(kvp => kvp.Key, kvp => kvp.Value);
        var factory = Factory(
            services =>
            {
                services.AddSingleton<ITicketService>(sp =>
                {
                    var store = new RecordingTicketService();
                    handle.Store = store;
                    return store;
                });
                services.AddScoped<IUserDirectory>(_ => FixedUserDirectory.Instance);
            },
            settings);
        handle.Factory = factory;
        handle.Client = factory.CreateClient();
        UseHermeticKeys(factory);
        return handle;
    }

    /// <summary>Holds the API client, its factory, and the store resolved on first use.</summary>
    private sealed class ApiHandle
    {
        public WebApplicationFactory<Program> Factory { get; set; } = default!;
        public HttpClient Client { get; set; } = default!;
        public RecordingTicketService? Store { get; set; }
    }

    /// <summary>
    /// Mints a v2-shaped Entra access token signed with the local key. The
    /// defaults produce a valid delegated token for the API's own client id.
    /// </summary>
    private string Mint(
        string aud = ClientId,
        string iss = TenantIssuer,
        RSA? key = null,
        (DateTimeOffset nbf, DateTimeOffset exp)? lifetime = null,
        string? scp = "access_as_user",
        bool withUsername = true,
        IReadOnlyCollection<string>? roles = null,
        string ver = "2.0",
        string azp = ClientId)
    {
        var now = DateTimeOffset.UtcNow;
        var claims = new Dictionary<string, object>
        {
            ["aud"] = aud,
            ["iss"] = iss,
            ["iat"] = now.ToUnixTimeSeconds(),
            ["nbf"] = (lifetime?.nbf ?? now.AddMinutes(-1)).ToUnixTimeSeconds(),
            ["exp"] = (lifetime?.exp ?? now.AddHours(1)).ToUnixTimeSeconds(),
            ["ver"] = ver,
            ["tid"] = TenantId,
            ["sub"] = "ffffffff-ffff-ffff-ffff-ffffffffffff",
            ["azp"] = azp,
        };
        if (scp is not null)
        {
            claims["scp"] = scp;
        }

        if (withUsername)
        {
            claims["preferred_username"] = "caller@example.com";
            claims["name"] = "Caller Example";
        }

        if (roles is { Count: > 0 })
        {
            claims["roles"] = roles;
        }

        return new JsonWebTokenHandler().CreateToken(new SecurityTokenDescriptor
        {
            Claims = claims,
            SigningCredentials = new SigningCredentials(
                new RsaSecurityKey(key ?? _signingKey), SecurityAlgorithms.RsaSha256),
            TokenType = "at+jwt",
        });
    }

    /// <summary>A directory that answers with a fixed profile, standing in for Graph OBO.</summary>
    private sealed class FixedUserDirectory : IUserDirectory
    {
        public const string Email = "principal@fixture.test";
        public const string DisplayName = "Fixture Principal";

        public static readonly FixedUserDirectory Instance = new();

        public Task<UserProfile> GetProfileAsync(ClaimsPrincipal caller, CancellationToken cancellationToken = default)
        {
            return Task.FromResult(new UserProfile { DisplayName = DisplayName, Email = Email });
        }
    }

    [Fact]
    public async Task ValidDelegatedToken_accepts_andBindsCallerClaims()
    {
        var api = NewApi();
        var client = api.Client;
        client.DefaultRequestHeaders.Authorization = new("Bearer", Mint());

        var response = await client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "from a signed-in caller" });

        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        Assert.NotNull(api.Store);
        Assert.Equal("caller@example.com", api.Store.Caller.Email);
        Assert.Equal("Caller Example", api.Store.Caller.DisplayName);
        Assert.NotEqual(Guest.Email, api.Store.Caller.Email);
    }

    [Fact]
    public async Task ValidDelegatedToken_profileServesDirectoryIdentity()
    {
        var api = NewApi();
        var client = api.Client;
        client.DefaultRequestHeaders.Authorization = new("Bearer", Mint());

        var profile = await client.GetFromJsonAsync<UserProfile>("/api/me");

        Assert.NotNull(profile);
        Assert.Equal(FixedUserDirectory.Email, profile.Email);
        Assert.NotEqual(Guest.Email, profile.Email);
    }

    [Fact]
    public async Task AnonymousCaller_withoutCredentials_isServedAsGuest()
    {
        // No Authorization header is a guest by design; the scope gate applies
        // only to credentials that were presented.
        var api = NewApi();

        var me = await api.Client.GetAsync("/api/me");
        var tickets = await api.Client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "x" });

        Assert.Equal(HttpStatusCode.OK, me.StatusCode);
        Assert.Equal(HttpStatusCode.Created, tickets.StatusCode);
        Assert.NotNull(api.Store);
        Assert.Equal(Guest.Email, api.Store.Caller.Email);
    }

    [Fact]
    public async Task ScopeUriAudience_isRejected_onGuestOpenEndpoints()
    {
        // The regression the fix removed: the pre-deploy configuration preset
        // exactly this audience (the identifier URI) on the API scheme, so
        // every v2 token failed validation. Presenting a token aimed at the
        // identifier URI must still be rejected: v2 tokens name the client id.
        var api = NewApi();
        var client = api.Client;
        client.DefaultRequestHeaders.Authorization =
            new("Bearer", Mint(aud: $"api://{ClientId}/access_as_user"));

        var me = await client.GetAsync("/api/me");
        var tickets = await client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "x" });

        Assert.Equal(HttpStatusCode.Unauthorized, me.StatusCode);
        Assert.Equal(HttpStatusCode.Unauthorized, tickets.StatusCode);
        Assert.Contains(me.Headers.WwwAuthenticate, h => h.Scheme == "Bearer");
    }

    [Fact]
    public async Task ForeignAudience_isRejected()
    {
        var api = NewApi();
        var client = api.Client;
        client.DefaultRequestHeaders.Authorization = new("Bearer", Mint(aud: ForeignAudience));

        var response = await client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "x" });

        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
    }

    [Fact]
    public async Task BotAppAudience_isRejected_onSpaEndpoints()
    {
        // Scheme isolation: a token minted for the bot's app id (the managed
        // identity client id) does not open the SPA's endpoints; bot tokens
        // are only validable under the separate BotJwt scheme on /api/bot.
        var api = NewApi();
        var client = api.Client;
        client.DefaultRequestHeaders.Authorization = new("Bearer", Mint(aud: BotAppId));

        var me = await client.GetAsync("/api/me");
        var tickets = await client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "x" });

        Assert.Equal(HttpStatusCode.Unauthorized, me.StatusCode);
        Assert.Equal(HttpStatusCode.Unauthorized, tickets.StatusCode);
    }

    [Fact]
    public async Task ForeignIssuer_isRejected()
    {
        var api = NewApi();
        var client = api.Client;
        client.DefaultRequestHeaders.Authorization = new("Bearer", Mint(iss: ForeignIssuer));

        var response = await client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "x" });

        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
    }

    [Fact]
    public async Task ForeignSigningKey_isRejected()
    {
        // Claims, audience, and issuer are all correct; only the signature
        // was made by a key the configuration does not trust.
        var api = NewApi();
        var client = api.Client;
        client.DefaultRequestHeaders.Authorization = new("Bearer", Mint(key: _otherKey));

        var response = await client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "x" });

        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
    }

    [Fact]
    public async Task ExpiredToken_isRejected()
    {
        var now = DateTimeOffset.UtcNow;
        var api = NewApi();
        var client = api.Client;
        client.DefaultRequestHeaders.Authorization =
            new("Bearer", Mint(lifetime: (now.AddHours(-2), now.AddMinutes(-10))));

        var response = await client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "x" });

        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
    }

    [Fact]
    public async Task AppOnlyToken_isRejected_onProfile()
    {
        // An app-only token for the API's client id — roles but no scp, no
        // preferred_username — authenticates at the handler but carries no
        // delegated scope, so the endpoint challenges it (401) before any
        // directory lookup is attempted.
        var api = NewApi();
        var client = api.Client;
        client.DefaultRequestHeaders.Authorization =
            new("Bearer", Mint(scp: null, withUsername: false, roles: ["It.Support.Admin"]));

        var response = await client.GetAsync("/api/me");

        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
    }

    [Fact]
    public async Task AppOnlyToken_isRejected_onTickets()
    {
        // The former failure path — handler accepted the app-only token and
        // caller mapping threw on the missing preferred_username, surfacing
        // as HTTP 500 — is closed: the missing delegated scope is challenged
        // before caller mapping runs.
        var api = NewApi();
        var client = api.Client;
        client.DefaultRequestHeaders.Authorization =
            new("Bearer", Mint(scp: null, withUsername: false, roles: ["It.Support.Admin"]));

        var response = await client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "x" });

        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
    }

    [Fact]
    public async Task ScopeClaim_wrongValue_isRejected_asIsMissingScopesAndRoles()
    {
        // The delegated gate checks the VALUE of scp: a token naming an
        // unrelated scope authenticates at the handler but is challenged
        // (401) instead of creating tickets. A token with neither scp nor
        // roles is still rejected.
        var api = NewApi();
        var client = api.Client;

        client.DefaultRequestHeaders.Authorization = new("Bearer", Mint(scp: "User.Read"));
        var wrongScope = await client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "a" });
        Assert.Equal(HttpStatusCode.Unauthorized, wrongScope.StatusCode);

        client.DefaultRequestHeaders.Authorization = new("Bearer", Mint(scp: null, roles: null));
        var noScopesNoRoles = await client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "b" });
        Assert.Equal(HttpStatusCode.Unauthorized, noScopesNoRoles.StatusCode);
    }

    [Fact]
    public async Task SingleEntryForwardedFor_selectsTheBucket_onATrustedLoopbackPeer()
    {
        // App-level complement to the middleware tests below: over the test
        // server's loopback peer, a single-entry X-Forwarded-For chooses the
        // rate-limit bucket, and a headerless request lands in the peer's own
        // bucket. This is exactly the trust the middleware tests examine.
        var api = NewApi(new Dictionary<string, string?>
        {
            ["RateLimit:PermitLimit"] = "1",
            ["RateLimit:WindowSeconds"] = "60",
        });
        var client = api.Client;

        var forwarded = new HttpRequestMessage(HttpMethod.Post, "/api/tickets")
        {
            Content = JsonContent.Create(new CreateTicketRequest { ShortDescription = "one" }),
        };
        forwarded.Headers.Add("X-Forwarded-For", "203.0.113.50");
        var first = await client.SendAsync(forwarded);
        Assert.Equal(HttpStatusCode.Created, first.StatusCode);

        var repeat = new HttpRequestMessage(HttpMethod.Post, "/api/tickets")
        {
            Content = JsonContent.Create(new CreateTicketRequest { ShortDescription = "two" }),
        };
        repeat.Headers.Add("X-Forwarded-For", "203.0.113.50");
        var second = await client.SendAsync(repeat);
        Assert.Equal(HttpStatusCode.TooManyRequests, second.StatusCode);

        var direct = await client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "three" });
        Assert.Equal(HttpStatusCode.Created, direct.StatusCode);
    }

    [Fact]
    public async Task ForwardedHeaders_loopbackPeer_processesRightmostEntry()
    {
        // The trust case every repo test exercises: the loopback peer.
        var address = await ProcessForwardedHeadersAsync(
            IPAddress.IPv6Loopback, "203.0.113.66, 198.51.100.7");

        Assert.Equal(IPAddress.Parse("198.51.100.7"), address);
    }

    [Fact]
    public async Task ForwardedHeaders_ingressPeerWithoutKnownProxies_processesTheHeaders()
    {
        // Observed on the exact configuration the app registers: with the
        // known-proxy lists cleared, the middleware trusts the forwarded
        // headers from ANY direct peer, not only listed proxies. Behind the
        // Container Apps ingress the peer is always the ingress, so the
        // rightmost entry is the ingress-appended client address and the
        // per-client rate-limit bucket forms as intended. The residual is a
        // caller with direct network reach to the container (past the
        // ingress), who may place its own address rightmost and choose its
        // bucket.
        var address = await ProcessForwardedHeadersAsync(
            IPAddress.Parse("10.244.3.7"), "203.0.113.66, 198.51.100.7");

        Assert.Equal(IPAddress.Parse("198.51.100.7"), address);
    }

    /// <summary>
    /// Runs the exact middleware the app registers, with the exact options
    /// Program.cs configures, against a caller-controlled peer address.
    /// </summary>
    private static async Task<IPAddress> ProcessForwardedHeadersAsync(IPAddress peer, string forwardedFor)
    {
        var options = new ForwardedHeadersOptions
        {
            ForwardedHeaders = ForwardedHeaders.XForwardedFor | ForwardedHeaders.XForwardedProto,
            ForwardLimit = 1,
        };
        options.KnownIPNetworks.Clear();
        options.KnownProxies.Clear();

        var context = new DefaultHttpContext();
        context.Connection.RemoteIpAddress = peer;
        context.Request.Headers["X-Forwarded-For"] = forwardedFor;

        var middleware = new ForwardedHeadersMiddleware(
            _ => Task.CompletedTask,
            NullLoggerFactory.Instance,
            Options.Create(options));

        await middleware.Invoke(context);
        return context.Connection.RemoteIpAddress!;
    }

    /// <summary>
    /// The controlled comparison of the corrected diagnosis: the SAME valid
    /// GUID-audience delegated token, against the pre-fix configuration (an
    /// AzureAd:Audience override published the way terraform's removed
    /// AzureAd__Audience environment variable was) and against the corrected
    /// configuration. With the override, Microsoft.Identity.Web's postconfigure
    /// binds JwtBearerOptions.Audience to the override value, so the GUID
    /// audience fails validation (401); without it, the same token is accepted
    /// (201). The named options are recorded in both arms.
    /// </summary>
    [Fact]
    public async Task PreFixAudienceOverride_sameTokenRejected_withoutOverrideAccepted()
    {
        var token = Mint();

        var prefixed = NewApi(new Dictionary<string, string?>
        {
            ["AzureAd:Audience"] = "api://copaz-api",
        });
        var monitor = prefixed.Factory.Services
            .GetRequiredService<IOptionsMonitor<JwtBearerOptions>>();
        Assert.Equal("api://copaz-api", monitor.Get("Bearer").Audience);
        prefixed.Client.DefaultRequestHeaders.Authorization = new("Bearer", token);
        var rejected = await prefixed.Client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "prefix" });
        Assert.Equal(HttpStatusCode.Unauthorized, rejected.StatusCode);

        var corrected = NewApi();
        var correctedMonitor = corrected.Factory.Services
            .GetRequiredService<IOptionsMonitor<JwtBearerOptions>>();
        // Observed shape, corrected: without AzureAd:Audience neither the named
        // option's Audience nor TokenValidationParameters.ValidAudience carries
        // a pre-request value — MIW applies the ClientId audience at validation
        // time (RegisterValidAudience), which the request below and the
        // foreign-audience rejections elsewhere in this suite exercise.
        var correctedOptions = correctedMonitor.Get("Bearer");
        Assert.Null(correctedOptions.Audience);
        Assert.Null(correctedOptions.TokenValidationParameters.ValidAudience);
        corrected.Client.DefaultRequestHeaders.Authorization = new("Bearer", token);
        var accepted = await corrected.Client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "corrected" });
        Assert.Equal(HttpStatusCode.Created, accepted.StatusCode);
        Assert.Equal("caller@example.com", corrected.Store!.Caller.Email);
    }

    /// <summary>
    /// The actual BotJwt named options shape: dedicated scheme, GUID audiences
    /// from AZURE_CLIENT_ID, ABS-public-cloud default issuers plus the
    /// configured tenant's v1/v2 issuers, full issuer/audience/lifetime
    /// validation with signed tokens required, and the ABS/Entra
    /// metadata-switching and AllowedCallers events wired. Options shape
    /// only — no request, no metadata fetch.
    /// </summary>
    [Fact]
    public void BotJwtNamedOptions_shape()
    {
        var api = NewApi();
        var options = api.Factory.Services
            .GetRequiredService<IOptionsMonitor<JwtBearerOptions>>()
            .Get(BotAuthentication.SchemeName);

        var tvp = options.TokenValidationParameters;
        Assert.Equal([BotAppId], tvp.ValidAudiences);
        Assert.Null(tvp.ValidAudience);
        Assert.True(tvp.ValidateIssuer);
        Assert.True(tvp.ValidateAudience);
        Assert.True(tvp.ValidateLifetime);
        Assert.Equal(TimeSpan.FromMinutes(5), tvp.ClockSkew);
        Assert.True(tvp.RequireSignedTokens);
        Assert.True(tvp.ValidateIssuerSigningKey);
        Assert.Contains(
            "https://api.botframework.com",
            (IEnumerable<string>)tvp.ValidIssuers!);
        Assert.Contains(
            $"https://login.microsoftonline.com/{TenantId}/v2.0",
            (IEnumerable<string>)tvp.ValidIssuers!);
        // EnableAadSigningKeyIssuerValidation wires the configuration-aware
        // validator delegate in current Microsoft.IdentityModel.Validators;
        // either delegate present proves a signing-key validator is active.
        Assert.True(
            tvp.IssuerSigningKeyValidator is not null
            || tvp.IssuerSigningKeyValidatorUsingConfiguration is not null,
            "EnableAadSigningKeyIssuerValidation must wire a signing-key validator.");
        Assert.NotNull(options.Events?.OnMessageReceived);
        Assert.NotNull(options.Events?.OnTokenValidated);
    }

    /// <summary>
    /// The bot factory's AllowedCallers enforcement, exercised hermetically:
    /// the scheme is built by AddBotAspNetAuthentication with a specific
    /// caller list in test configuration only, and the request-level verdict
    /// comes from the factory's own OnTokenValidated logic. A token whose
    /// azp names a listed caller authenticates; a foreign azp is rejected.
    /// Test configuration only — Program.cs wires no caller list, so no
    /// runtime behavior is invented here.
    /// </summary>
    [Fact]
    public async Task BotAllowedCallers_listedCallerAccepted_foreignCallerRejected()
    {
        using var host = await new HostBuilder()
            .ConfigureWebHost(web => web
                .UseTestServer()
                .ConfigureServices(services =>
                {
                    services.AddBotAspNetAuthentication(new TokenValidationOptions
                    {
                        Audiences = [BotAppId],
                        TenantId = TenantId,
                        AllowedCallers = [KnownCallerAppId],
                    });
                })
                .Configure(app =>
                {
                    app.UseAuthentication();
                    app.Run(async context =>
                    {
                        var result = await context.AuthenticateAsync(BotAuthentication.SchemeName);
                        context.Response.StatusCode = result.Succeeded
                            ? StatusCodes.Status200OK
                            : StatusCodes.Status401Unauthorized;
                    });
                }))
            .StartAsync();

        var options = host.Services
            .GetRequiredService<IOptionsMonitor<JwtBearerOptions>>()
            .Get(BotAuthentication.SchemeName);
        var botEvents = options.Events
            ?? throw new InvalidOperationException("The bot scheme must carry events.");
        var metadata = new OpenIdConnectConfiguration { Issuer = TenantIssuer };
        metadata.SigningKeys.Add(new RsaSecurityKey(_signingKey));
        options.ConfigurationManager = new StaticConfigurationManager<OpenIdConnectConfiguration>(metadata);
        options.TokenValidationParameters.IssuerSigningKey = new RsaSecurityKey(_signingKey);
        options.TokenValidationParameters.ValidIssuers = [TenantIssuer];
        // Hermetic delta: the ABS metadata-switching event would fetch live
        // Entra metadata for the fixture tenant; keep only OnTokenValidated,
        // the enforcement under test.
        options.Events = new JwtBearerEvents { OnTokenValidated = botEvents.OnTokenValidated };

        var client = host.GetTestServer().CreateClient();

        client.DefaultRequestHeaders.Authorization =
            new("Bearer", Mint(aud: BotAppId, azp: KnownCallerAppId));
        var listed = await client.GetAsync("/");
        Assert.Equal(HttpStatusCode.OK, listed.StatusCode);

        client.DefaultRequestHeaders.Authorization =
            new("Bearer", Mint(aud: BotAppId, azp: ForeignCallerAppId));
        var foreign = await client.GetAsync("/");
        Assert.Equal(HttpStatusCode.Unauthorized, foreign.StatusCode);
    }

    /// <summary>
    /// The deployed limiter defaults, exercised: twenty guest tickets per
    /// IP per sixty-second window pass, the twenty-first answers 429 — the
    /// guest caller is rate limited on the same per-address partition as
    /// everyone else, which is what makes guest openness abusable only at
    /// a bounded rate.
    /// </summary>
    [Fact]
    public async Task LimiterDefaults_twentyGuestTicketsPass_twentyFirstIs429()
    {
        var api = NewApi();
        for (var i = 1; i <= 20; i++)
        {
            var ok = await api.Client.PostAsJsonAsync(
                "/api/tickets", new CreateTicketRequest { ShortDescription = $"n{i}" });
            Assert.Equal(HttpStatusCode.Created, ok.StatusCode);
        }

        var over = await api.Client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "over" });
        Assert.Equal(HttpStatusCode.TooManyRequests, over.StatusCode);
        // The 429 short-circuits before the action, so only the twenty guest
        // calls reached the store — the guest caller is throttled too.
        Assert.NotNull(api.Store);
        Assert.Equal(Guest.Email, api.Store.Caller.Email);
    }

    /// <summary>
    /// Window reset: with a one-second window, a request that exhausted the
    /// budget succeeds again once the window rolls — the fixed window is
    /// temporary, not a ban.
    /// </summary>
    [Fact]
    public async Task LimiterWindow_exhaustedBudgetResetsAfterTheWindow()
    {
        var api = NewApi(new Dictionary<string, string?>
        {
            ["RateLimit:PermitLimit"] = "1",
            ["RateLimit:WindowSeconds"] = "1",
        });
        api.Client.DefaultRequestHeaders.Authorization = new("Bearer", Mint());
        Assert.Equal(HttpStatusCode.Created, (await api.Client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "first" })).StatusCode);
        Assert.Equal(HttpStatusCode.TooManyRequests, (await api.Client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "second" })).StatusCode);

        await Task.Delay(1200);

        Assert.Equal(HttpStatusCode.Created, (await api.Client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "third" })).StatusCode);
    }

    /// <summary>
    /// Invalid credentials consume from the same per-address bucket as valid
    /// ones: with a budget of one, a presented-but-invalid bearer burns the
    /// permit on its 401, and the following valid request from the same
    /// address answers 429. Rate limiting is enforced ahead of the auth
    /// decision, so challenges are not a free side channel.
    /// </summary>
    [Fact]
    public async Task Limiter_invalidBearerConsumesThePermitBeforeTheAuthDecision()
    {
        var api = NewApi(new Dictionary<string, string?>
        {
            ["RateLimit:PermitLimit"] = "1",
            ["RateLimit:WindowSeconds"] = "60",
        });
        api.Client.DefaultRequestHeaders.Authorization = new("Bearer", "not-a-jwt");
        var invalid = await api.Client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "bad" });
        Assert.Equal(HttpStatusCode.Unauthorized, invalid.StatusCode);

        api.Client.DefaultRequestHeaders.Authorization = new("Bearer", Mint());
        var valid = await api.Client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "good" });
        Assert.Equal(HttpStatusCode.TooManyRequests, valid.StatusCode);
    }

    /// <summary>
    /// Production-hosting control: the same hermetic accept/reject behavior
    /// under UseEnvironment(Production) with the fail-closed configuration
    /// complete (Entra ids, bot audience, ticket service URI, OpenAI/Search
    /// endpoints — clients constructed, never called). This proves the Entra
    /// branch bootstraps outside Development and behaves identically; it is
    /// still local-signature evidence, not a live Entra round trip.
    /// </summary>
    [Fact]
    public async Task ProductionHosting_hermeticMatrixBehavesIdentically()
    {
        var handle = new ApiHandle();
        var settings = new Dictionary<string, string?>
        {
            ["Environment"] = "Production",
            ["AzureAd:ClientId"] = ClientId,
            ["AzureAd:TenantId"] = TenantId,
            ["AZURE_CLIENT_ID"] = BotAppId,
            ["Tickets:ServiceUri"] = "https://fixture.table.core.windows.net",
            ["OpenAI:Endpoint"] = "https://fixture.openai.azure.com",
            ["Search:Endpoint"] = "https://fixture.search.windows.net",
            ["Search:IndexName"] = "fixture-index",
        };
        var factory = Factory(
            services =>
            {
                services.AddSingleton<ITicketService>(sp =>
                {
                    var store = new RecordingTicketService();
                    handle.Store = store;
                    return store;
                });
                services.AddScoped<IUserDirectory>(_ => FixedUserDirectory.Instance);
            },
            settings);
        handle.Client = factory.CreateClient();
        UseHermeticKeys(factory);

        handle.Client.DefaultRequestHeaders.Authorization = new("Bearer", Mint());
        var accepted = await handle.Client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "prod ok" });
        Assert.Equal(HttpStatusCode.Created, accepted.StatusCode);
        Assert.Equal("caller@example.com", handle.Store!.Caller.Email);

        handle.Client.DefaultRequestHeaders.Authorization = new("Bearer", Mint(
            iss: ForeignIssuer));
        var rejected = await handle.Client.PostAsJsonAsync(
            "/api/tickets", new CreateTicketRequest { ShortDescription = "prod bad" });
        Assert.Equal(HttpStatusCode.Unauthorized, rejected.StatusCode);
    }
}
