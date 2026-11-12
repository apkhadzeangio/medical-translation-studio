# GluAPI connection diagnosis

## Findings

Two different failures were observed and must be distinguished:

| Request | Client | Observed result |
| --- | --- | --- |
| `GET https://api.gluapi.com/v1/models`, without credentials | Python `urllib` | HTTP 403, plain-text `error code: 1010` |
| Same model-list endpoint, without credentials | Project's `httpx` library | HTTP 200, model catalogue |
| Model-list endpoint with a configured credential | Project's `httpx` library | HTTP 200; `gpt-6-astra` is advertised |
| A short synthetic Georgian translation through `LLMGateway` | Project's `httpx` library | HTTP 503, `No available accounts: no available accounts` |

The initial `urllib` probe did not represent the application's HTTP client. Cloudflare
[documents error 1010](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/)
as client-signature rejection and specifically discusses the default Python `urllib`
user agent. The comparison used the application's existing client without browser
impersonation, proxy changes, disabling certificate verification or access-control changes.

The application can reach GluAPI. Generation fails when the provider attempts to select
an upstream account. The returned message supports a provider-side availability/routing
failure; it does not reveal whether the underlying account is unavailable, rate-limited,
disabled or misconfigured. Only the provider can confirm that detail from its internal logs.
Do not treat a public or authenticated model list as proof that generation works.

## Bounded generation check

The synthetic check used the application's gateway, `gpt-6-astra`, JSON output, a 1,024-token
completion limit, one attempt, a 60-second timeout and an isolated development project.
It did not process a user document or modify production settings. The gateway recorded
the failed request; no translated output or token-usage response was returned.

After provider recovery, repeat the bounded check before enabling a chapter or a book.
Keep retries bounded while the provider reports no available accounts. Do not switch a
production translation model or send documents to another provider merely to hide this error.

## Provider support checklist

The provider needs to check:

1. Whether the CODEX group has healthy upstream accounts available to `gpt-6-astra`.
2. Whether those accounts are eligible for `POST /v1/chat/completions`, as advertised on
   [the provider's site](https://gluapi.com/).
3. Any account cooldowns, upstream quota, concurrency limits or group/model routing rules
   causing the selector to report `No available accounts`.

Provide the error, endpoint, model and test time through the provider's support channel.
Keep credentials out of tickets and source control.
