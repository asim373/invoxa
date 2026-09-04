# Architecture and engineering decisions

## Product boundary

Invoxa is a modular monolith with a separate background worker. Authentication, ownership,
invoice data, analytics, and reports share one domain model while expensive extraction is kept
outside HTTP requests. It is not an accounting ledger, payment system, ERP, CRM, tax-law engine,
or fraud classifier.

## Processing flow

1. API authenticates the caller and validates upload metadata, signature, and limits.
2. It streams to generated owner-scoped storage, records SHA-256, and commits a queued document.
3. It publishes an idempotent Redis job; publication failure leaves a recoverable DB record.
4. Worker atomically moves work to in-flight and marks the document processing.
5. PDF extraction or Tesseract OCR produces text; extraction derives fields and line items.
6. Deterministic validation records reconciliation and quality evidence.
7. The completed document commits before AI Analysis runs in a separate transaction.
8. Worker acknowledges terminal work and periodically reconciles queued DB records.

Worker startup recovers abandoned in-flight jobs. Database state is authoritative for
reconciliation; Redis provides delivery/recovery. Paths are resolved below the storage root.

## AI Analysis: accurate terminology

AI Analysis combines extraction confidence, deterministic financial reasoning, multi-signal
matching, and robust statistics. It uses no paid external model and does not rename basic
validation as machine learning.

Deterministic components cover arithmetic, line items, missing fields, low confidence, dates,
exact checksum duplicates, and financial/tax consistency without legal claims.

Statistical amount analysis uses median and median absolute deviation (MAD) in the user's own
same-currency history, requiring at least five prior invoices globally or four for a vendor.
Small samples create no statistical claim. Probable duplicates require multiple normalized
signals (invoice number, vendor, date, currency, total); exact checksums are separate. Stable
signatures make re-analysis idempotent and preserve review state.

No finding is a fraud probability. Explanations expose evidence and observed/expected values.

## Authentication and authorization

Passwords use Argon2. JWTs carry identity and auth version; password reset invalidates older
tokens. Redis-backed limits protect auth, recovery, uploads, and Google endpoints. The limiter
fails open on Redis outage without logging caller identity; public deployments should add edge
rate limiting for defense in depth.

Google uses OAuth/OIDC code flow with PKCE S256, signed state, nonce, issuer/audience/azp checks,
JWKS validation, verified email, durable provider subject, and hashed single-use exchange code.
Provider tokens are exchanged server-side and not stored as application credentials.

RBAC controls mutations and every business query applies owner identity. Cross-owner access uses
non-disclosing 404 responses.

## Data, aggregation, and migrations

PostgreSQL stores users, documents, invoices/line items, validation, findings/review state, OAuth
identities, and short-lived token records. Append-only Alembic migrations run before API/worker.
Constraints and query-driven indexes protect integrity and ownership/filter paths.

Analytics aggregate server-side. Currencies are grouped, never implicitly converted or summed.
The browser receives scoped aggregates and pagination rather than the full invoice dataset.

## Failure isolation

- Streaming cleanup prevents partial uploads becoming documents.
- Reconciliation closes the DB-commit/queue-publish gap.
- In-flight recovery restores interrupted work.
- Safe terminal states prevent indefinite processing.
- AI failure cannot roll back extraction.
- Re-analysis upserts stable findings.
- Readiness distinguishes liveness from dependency health.

## Security controls

- Argon2, short-lived reset tokens, JWT auth-version invalidation
- OAuth state/nonce/PKCE/signature/issuer/audience/single-use validation
- restricted CORS/trusted hosts and production startup validation
- secure production OAuth cookie and external HTTPS boundary
- upload limits, signature checks, OCR bounds, storage path containment
- owner scoping, RBAC, and spreadsheet-formula neutralization
- API security headers, production HSTS, static asset cache controls
- raw API access-query logs disabled to avoid OAuth callback-code retention
- ignored environment/deployment secret management

## Testing and engineering tradeoffs

Backend tests cover extraction, validation, queues, security, auth/OAuth, ownership, AI,
analytics, reports, and exports. Frontend tests cover auth, dashboard/documents, findings,
reports, states, and roles. Docker tests process real generated PDF/JPG/PNG, rapid uploads, and
cross-user denial. Static gates include Ruff, Pyright, ESLint, Prettier, TypeScript, Vite, npm
production audit, images, migrations, health, Tesseract, and OCR language data.

Known tradeoffs:

- Modular monolith avoids premature distributed-system overhead.
- Redis rate limiting fails open; edge limiting is recommended.
- SPA bearer tokens use local storage; XSS prevention and controlled content are important.
- Named-volume files are single-host, not multi-node object storage.
- English OCR is bundled; multilingual OCR needs customization.
- No FX, tax-law decision, fraud model, paid LLM, PDF report renderer, or malware scanner.
- SMTP, TLS, backups, monitoring, production OAuth, and DNS are operator responsibilities.

These limits distinguish application guarantees from deployment responsibilities and future work.
