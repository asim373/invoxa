# Production deployment

This guide describes Invoxa's production boundary without assuming a cloud provider. A trusted
reverse proxy or hosting platform terminates HTTPS and forwards to loopback-bound services.

## Deployment model

Compose runs PostgreSQL, Redis, a one-shot migration service, API, worker, and static web server.
Named volumes persist the database, queue, and documents. `docker-compose.production.yml` removes
host publication for PostgreSQL/Redis and binds API/web to `127.0.0.1` for a reverse proxy.

This is a single-host model. Multiple hosts require shared document storage and externally
managed PostgreSQL/Redis; independent document volumes cannot be used across replicas.

## Production environment

Create an untracked `.env.production` from `.env.example`. Set at minimum:

```dotenv
APP_ENVIRONMENT=production
POSTGRES_PASSWORD=<strong-unique-value>
DATABASE_URL=postgresql://<user>:<password>@postgres:5432/<database>
REDIS_URL=redis://redis:6379/0
AUTH_SECRET_KEY=<cryptographically-random-value-at-least-32-characters>
RATE_LIMIT_ENABLED=true
VITE_API_BASE_URL=https://api.example.com
FRONTEND_BASE_URL=https://app.example.com
CORS_ORIGINS=["https://app.example.com"]
ALLOWED_HOSTS=["api.example.com"]
SMTP_HOST=<smtp-host>
SMTP_PORT=587
SMTP_USERNAME=<smtp-user-if-required>
SMTP_PASSWORD=<smtp-secret-if-required>
SMTP_FROM_ADDRESS=no-reply@example.com
SMTP_STARTTLS=true
SMTP_USE_SSL=false
```

Use STARTTLS or implicit SSL, never both. Production startup rejects development JWT/database
defaults, wildcard or non-HTTPS CORS, wildcard/missing trusted hosts, disabled rate limiting,
non-HTTPS frontend URLs, and missing SMTP delivery configuration.

`VITE_API_BASE_URL` is public and embedded at build time; rebuild web when it changes. JWT,
database, SMTP, and OAuth secrets are server-side secrets.

## Google Sign-In

Create a Google OAuth 2.0 **Web application** with only `openid`, `email`, and `profile` and set:

```dotenv
GOOGLE_OAUTH_CLIENT_ID=<production-web-client-id>
GOOGLE_OAUTH_CLIENT_SECRET=<production-web-client-secret>
GOOGLE_OAUTH_REDIRECT_URI=https://api.example.com/auth/google/callback
```

The callback must exactly match Google Cloud. Provider credentials stay in deployment secrets or
an ignored environment file; credential JSON is not needed at runtime. The flow uses code + PKCE
S256, state, nonce, signature/issuer/audience validation, durable subject linkage, and a hashed
single-use exchange code. The production transaction cookie is Secure, HttpOnly, and SameSite=Lax.

## Validate and deploy

```powershell
docker compose --env-file .env.production `
  -f docker-compose.yml -f docker-compose.production.yml config --quiet
docker compose --env-file .env.production `
  -f docker-compose.yml -f docker-compose.production.yml up -d --build
```

The migration container runs `alembic upgrade head`; API and worker wait for its success and for
healthy dependencies. A failed migration prevents startup against an incompatible schema.

```powershell
docker compose ps
docker compose exec -T api alembic current
docker compose exec -T api tesseract --version
docker compose exec -T api tesseract --list-langs
```

`GET /health` is liveness. `GET /health/ready` requires PostgreSQL and Redis. The web root serves
the compiled SPA and never receives server secrets.

## Reverse proxy and HTTPS

Route the frontend hostname to the loopback web port and API hostname to the loopback API port.
Preserve `Host` for trusted-host enforcement, redirect HTTP to HTTPS, and configure request size
and timeout limits for uploads. TLS certificates remain an infrastructure responsibility.

The API sends nosniff, deny-framing, no-referrer, permissions-policy, no-store, and production
HSTS headers. NGINX sends baseline headers, immutable caching for hashed assets, and no-store for
`index.html`. Raw API access-query logs are disabled to avoid retaining OAuth callback codes;
do not enable verbose proxy query logging for authentication callbacks.

## Persistence, backups, and updates

- `postgres_data`: users, documents, invoices, findings, and review state
- `redis_data`: queued and in-flight jobs
- `document_storage`: uploaded source documents

Back up PostgreSQL and document storage together and test restoration. Redis persistence aids
recovery but the database is authoritative for queued documents. Never use
`docker compose down --volumes` for routine deployment.

For updates: back up data, build from a reviewed commit, run the migration service, verify
readiness/workflows, and retain prior images. Do not automatically downgrade production schema;
review migration and backup compatibility first.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| API unhealthy | API/migrate logs, database and Redis URLs, trusted hosts |
| Migration failed | migrate logs and schema/configuration before restarting |
| Document remains queued | worker logs, Redis, reconciliation interval, shared storage mount |
| OCR failure | Tesseract/languages, signature, page/pixel limits, storage access |
| Google button disabled | both Google client ID and secret; `/auth/google/config` |
| Google callback fails | exact HTTPS callback, frontend URL, provider test-user/publish state |
| Password email absent | SMTP host/from, TLS mode, authentication, provider delivery logs |
| Browser calls localhost | rebuild web with the correct `VITE_API_BASE_URL` |
