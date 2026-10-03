// Ported from Microsoft Agents-for-net v1.8.77, src/samples/Shared/AspNetExtensions.cs
// (MIT License, Copyright (c) Microsoft Corporation). The SDK's XML docs name this
// sample implementation as the default inbound-auth vehicle for agent endpoints; at
// 1.8.77 it is sample-provided, not packaged. The port registers the JWT bearer
// scheme under a dedicated name so it composes with the web app's existing Entra
// scheme for the SPA API instead of replacing the default authentication scheme.

using Microsoft.Agents.Authentication;
using Microsoft.Agents.Core;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.IdentityModel.JsonWebTokens;
using Microsoft.IdentityModel.Protocols;
using Microsoft.IdentityModel.Protocols.OpenIdConnect;
using Microsoft.IdentityModel.Tokens;
using Microsoft.IdentityModel.Validators;
using System.Collections.Concurrent;
using System.Globalization;
using System.Net.Http;
using System.Security.Claims;

namespace ItSupport.Api.Bot;

/// <summary>
/// JWT bearer token validation for Azure Bot Service and agent-to-agent requests,
/// read from the "TokenValidation" configuration section.
/// </summary>
public sealed class TokenValidationOptions
{
    public IList<string>? Audiences { get; set; }
    public string? TenantId { get; set; }
    public IList<string>? ValidIssuers { get; set; }
    public bool IsGov { get; set; } = false;
    public bool AzureBotServiceOnly { get; set; } = false;
    public string? AzureBotServiceOpenIdMetadataUrl { get; set; }
    public string? OpenIdMetadataUrl { get; set; }
    public bool AzureBotServiceTokenHandling { get; set; } = true;
    public TimeSpan? OpenIdMetadataRefresh { get; set; }
    public IList<string>? AllowedCallers { get; set; }
}

public static class BotAuthentication
{
    /// <summary>
    /// Scheme name for the bot's inbound Bot Protocol validation; distinct from
    /// the web app's Entra scheme so each surface validates its own tokens.
    /// </summary>
    public const string SchemeName = "BotJwt";

    private static readonly ConcurrentDictionary<string, ConfigurationManager<OpenIdConnectConfiguration>> OpenIdMetadataCache = new();

    private static bool IsBotFrameworkIssuer(string issuer)
    {
        return AuthenticationConstants.BotFrameworkTokenIssuer.Equals(issuer, StringComparison.OrdinalIgnoreCase)
            || AuthenticationConstants.GovBotFrameworkTokenIssuer.Equals(issuer, StringComparison.OrdinalIgnoreCase)
            || AuthenticationConstants.ChinaBotFrameworkTokenIssuer.Equals(issuer, StringComparison.OrdinalIgnoreCase);
    }

    public static IServiceCollection AddBotAspNetAuthentication(
        this IServiceCollection services, TokenValidationOptions validationOptions)
    {
        AssertionHelpers.ThrowIfNull(validationOptions, nameof(validationOptions));

        // Must have at least one Audience.
        if (validationOptions.Audiences == null || validationOptions.Audiences.Count == 0)
        {
            throw new ArgumentException($"{nameof(TokenValidationOptions)}:Audiences requires at least one ClientId");
        }

        // Audience values must be GUIDs.
        foreach (var audience in validationOptions.Audiences)
        {
            if (!Guid.TryParse(audience, out _))
            {
                throw new ArgumentException($"{nameof(TokenValidationOptions)}:Audiences values must be a GUID");
            }
        }

        // If ValidIssuers is empty, default for ABS Public Cloud.
        if (validationOptions.ValidIssuers == null || validationOptions.ValidIssuers.Count == 0)
        {
            if (validationOptions.AzureBotServiceOnly)
            {
                validationOptions.ValidIssuers =
                [
                    validationOptions.IsGov
                        ? AuthenticationConstants.GovBotFrameworkTokenIssuer
                        : AuthenticationConstants.BotFrameworkTokenIssuer
                ];
            }
            else
            {
                validationOptions.ValidIssuers =
                [
                    AuthenticationConstants.BotFrameworkTokenIssuer,
                    "https://sts.windows.net/d6d49420-f39b-4df7-a1dc-d59a935871db/",
                    "https://login.microsoftonline.com/d6d49420-f39b-4df7-a1dc-d59a935871db/v2.0",
                    "https://sts.windows.net/f8cdef31-a31e-4b4a-93e4-5f571e91255a/",
                    "https://login.microsoftonline.com/f8cdef31-a31e-4b4a-93e4-5f571e91255a/v2.0",
                    "https://sts.windows.net/69e9b82d-4842-4902-8d1e-abc5b98a55e8/",
                    "https://login.microsoftonline.com/69e9b82d-4842-4902-8d1e-abc5b98a55e8/v2.0",
                ];

                if (!string.IsNullOrEmpty(validationOptions.TenantId) && Guid.TryParse(validationOptions.TenantId, out _))
                {
                    validationOptions.ValidIssuers.Add(string.Format(
                        CultureInfo.InvariantCulture, AuthenticationConstants.ValidTokenIssuerUrlTemplateV1, validationOptions.TenantId));
                    validationOptions.ValidIssuers.Add(string.Format(
                        CultureInfo.InvariantCulture, AuthenticationConstants.ValidTokenIssuerUrlTemplateV2, validationOptions.TenantId));
                }
            }
        }

        validationOptions.AzureBotServiceOpenIdMetadataUrl ??= validationOptions.IsGov
            ? AuthenticationConstants.GovAzureBotServiceOpenIdMetadataUrl
            : AuthenticationConstants.PublicAzureBotServiceOpenIdMetadataUrl;
        validationOptions.OpenIdMetadataUrl ??= validationOptions.IsGov
            ? AuthenticationConstants.GovOpenIdMetadataUrl
            : AuthenticationConstants.PublicOpenIdMetadataUrl;

        var openIdMetadataRefresh =
            validationOptions.OpenIdMetadataRefresh ?? BaseConfigurationManager.DefaultAutomaticRefreshInterval;

        // Registers the scheme under SchemeName without touching the default
        // authentication scheme, which stays the web app's Entra scheme.
        services.AddAuthentication().AddJwtBearer(SchemeName, options =>
        {
            options.SaveToken = true;
            options.TokenValidationParameters = new TokenValidationParameters
            {
                ValidateIssuer = true,
                ValidateAudience = true,
                ValidateLifetime = true,
                ClockSkew = TimeSpan.FromMinutes(5),
                ValidIssuers = validationOptions.ValidIssuers,
                ValidAudiences = validationOptions.Audiences,
                ValidateIssuerSigningKey = true,
                RequireSignedTokens = true,
            };

            // Using Microsoft.IdentityModel.Validators
            options.TokenValidationParameters.EnableAadSigningKeyIssuerValidation();

            options.Events = new JwtBearerEvents
            {
                // Create a ConfigurationManager based on the requestor, to handle
                // ABS non-Entra tokens alongside Entra-issued ones.
                OnMessageReceived = context =>
                {
                    string authorizationHeader = context.Request.Headers.Authorization.ToString();

                    if (string.IsNullOrEmpty(authorizationHeader))
                    {
                        context.Options.TokenValidationParameters.ConfigurationManager ??=
                            options.ConfigurationManager as BaseConfigurationManager;
                        return Task.CompletedTask;
                    }

                    string[] parts = authorizationHeader.Split(' ');
                    if (parts.Length != 2 || parts[0] != "Bearer")
                    {
                        context.Options.TokenValidationParameters.ConfigurationManager ??=
                            options.ConfigurationManager as BaseConfigurationManager;
                        return Task.CompletedTask;
                    }

                    // Lightweight issuer extraction without full token parsing.
                    JsonWebToken token = new(parts[1]);
                    string issuer = token.Issuer;

                    context.Options.TokenValidationParameters.ConfigurationManager = validationOptions.AzureBotServiceTokenHandling
                        && IsBotFrameworkIssuer(issuer)
                        ? OpenIdMetadataCache.GetOrAdd(validationOptions.AzureBotServiceOpenIdMetadataUrl, key =>
                            new ConfigurationManager<OpenIdConnectConfiguration>(
                                key, new OpenIdConnectConfigurationRetriever(), new HttpClient())
                            {
                                AutomaticRefreshInterval = openIdMetadataRefresh,
                            })
                        : OpenIdMetadataCache.GetOrAdd(validationOptions.OpenIdMetadataUrl, key =>
                            new ConfigurationManager<OpenIdConnectConfiguration>(
                                key, new OpenIdConnectConfigurationRetriever(), new HttpClient())
                            {
                                AutomaticRefreshInterval = openIdMetadataRefresh,
                            });

                    return Task.CompletedTask;
                },

                OnTokenValidated = context =>
                {
                    // AllowedCallers check for non-BotFramework tokens; BotFramework
                    // tokens use service-level issuers and skip it.
                    var issuer = context.Principal?.FindFirst("iss")?.Value;
                    bool isBotFrameworkToken = validationOptions.AzureBotServiceTokenHandling
                        && issuer != null && IsBotFrameworkIssuer(issuer);

                    if (!isBotFrameworkToken
                        && context.Principal?.Identity is ClaimsIdentity identity
                        && !identity.IsTenantIdIssuerValid())
                    {
                        context.Fail("Token tenant ID does not match its issuer.");
                        return Task.CompletedTask;
                    }

                    if (!isBotFrameworkToken
                        && validationOptions.AllowedCallers is { Count: > 0 } allowedCallers
                        && !allowedCallers.Any(c => c.Equals("*", StringComparison.Ordinal)))
                    {
                        var callerAppId = context.Principal?.FindFirst("azp")?.Value
                            ?? context.Principal?.FindFirst("appid")?.Value;

                        if (string.IsNullOrEmpty(callerAppId)
                            || !allowedCallers.Any(c => c.Equals(callerAppId, StringComparison.OrdinalIgnoreCase)))
                        {
                            context.Fail($"Caller App ID '{callerAppId}' is not in the AllowedCallers list.");
                        }
                    }

                    return Task.CompletedTask;
                },
            };
        });

        return services;
    }
}
