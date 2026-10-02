# One image serves the SPA and the API: the SPA is built first and copied
# into the API image's static files.

FROM node:24-alpine AS spa
WORKDIR /src/apps/web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

FROM mcr.microsoft.com/dotnet/sdk:10.0 AS api
WORKDIR /src
COPY dotnet-tools.json ItSupport.slnx Directory.Packages.props ./
COPY contracts/ contracts/
COPY services/api/ services/api/
# The OpenAPI document is the build input for the generated controllers, so
# the repository layout is preserved inside the image.
RUN dotnet publish services/api/ItSupport.Api/ItSupport.Api.csproj \
    -c Release -o /app

FROM mcr.microsoft.com/dotnet/aspnet:10.0
WORKDIR /app
COPY --from=api /app .
COPY --from=spa /src/apps/web/dist ./wwwroot
ENV ASPNETCORE_HTTP_PORTS=8080
ENTRYPOINT ["dotnet", "ItSupport.Api.dll"]
