# Loupe Server

Accounts, sessions and the Claude proxy for Loupe.app. The Anthropic key lives
here only. Request and response bodies are never stored or logged.

| Route | Auth | Purpose |
|---|---|---|
| `GET /health` | none | Database check |
| `POST /v1/accounts` | none | Create account, returns a session token |
| `POST /v1/sessions` | none | Sign in, returns a session token |
| `DELETE /v1/sessions` | bearer | Sign out |
| `GET /v1/me` | bearer | Email and this month's token use |
| `POST /v1/messages` | bearer | Claude proxy: model allowlist, `max_tokens` cap, monthly token cap |

## Run Locally

Needs Node 22+, pnpm and Postgres.

```bash
cd server && pnpm install && cp .env.example .env
```

Fill in `DATABASE_URL` and `ANTHROPIC_API_KEY`, then:

```bash
createdb loupe && pnpm db:deploy && pnpm dev
```

## Test

Tests use a `loupe_test` database (override with `TEST_DATABASE_URL`).

```bash
createdb loupe_test && pnpm test && pnpm lint
```
