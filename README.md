# Invoxa — Document & Invoice Analyzer

A privacy-first SaaS application for uploading business documents, extracting invoice
data, reviewing results, and exporting structured records. The stack includes FastAPI,
React with TypeScript and Vite, PostgreSQL, Redis, and a resilient background worker.

## Current capabilities

- Account registration, bearer-token authentication, roles, and password recovery
- Owner-scoped document upload, listing, search, filtering, sorting, and pagination
- Secure PDF and image validation with configurable size, page, pixel, and OCR limits
- PDF text extraction plus OCR fallback for scanned PDFs and supported image formats
- Rule-based invoice fields and line-item extraction with validation information
- Reliable Redis queue processing, abandoned-job recovery, and queued-job reconciliation
- Document preview, extracted text, processing states, reprocessing, and deletion
- Individual and bulk CSV, XLSX, and JSON exports
- Explainable AI Analysis findings with acknowledgement, resolution, and reopening
- Ownership-scoped analytics, trends, and financial/AI/processing-quality reports
- Health/readiness checks, structured logging, security headers, and rate limiting
- Docker Compose services and automated CI regression checks

## Prerequisites

- Docker Desktop with Compose v2
- Node.js 20+ and npm 10+ for local frontend tooling
- Python 3.12+ for local backend tooling (optional when using Docker)

## Run with Docker

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Open `http://localhost:5173` for the frontend, `http://localhost:8000/health` for API health, and `http://localhost:8000/health/ready` for readiness.

Stop with `Ctrl+C`, or run `docker compose down`.

## Run backend locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
uvicorn apps.api.app.main:app --reload --port 8000
```

The local backend expects PostgreSQL and Redis from Docker Compose. Start only those services with `docker compose up postgres redis`.

## Run frontend locally

```powershell
Set-Location apps\web
npm install
npm run dev
```

## Quality checks

Backend: `python -m ruff check .`, `python -m ruff format --check .`, `python -m pyright`, `python -m pytest`

Frontend: `npm run lint`, `npm run format:check`, `npm run test:run`, `npm run build`

Docker configuration: `docker compose config --quiet`

## Database migrations

The API container applies migrations when it starts. To apply them explicitly:

```powershell
docker compose run --rm api alembic upgrade head
```

## AI Analysis and reports

AI Analysis runs after extraction and validation complete. The document is committed as
usable first; analysis then runs in a separate transaction so an analysis failure cannot
erase OCR results or leave processing stuck. Findings are persisted and a stable signature
makes re-analysis idempotent while preserving review state.

The analysis combines existing deterministic financial validation with machine-assisted
statistical detection. Amount anomalies use the median and median absolute deviation (MAD)
within the same currency, requiring at least five prior invoices globally or four for the
same vendor. Small samples produce no statistical anomaly claim. Duplicate analysis uses
content hashes for exact file duplicates and multiple normalized invoice signals for probable
duplicates. Missing fields, dates, extraction validation evidence, arithmetic, and tax
consistency checks always include a human-readable reason; they do not claim fraud or legal
tax compliance. No paid or external AI service is required, and invoice data is not sent to
an AI provider.

The Analytics view calculates KPIs and trends on the server and keeps currency totals
separate. Reports provide financial summary, AI Analysis, and processing-quality data with
CSV, XLSX, and JSON exports. PDF report export is intentionally deferred: the current stack
has no PDF report renderer, and adding one solely for this phase would add disproportionate
runtime and maintenance cost compared with the printable UI and spreadsheet exports.

## Production notes

- Replace every development placeholder in `.env` before deployment, especially
  `AUTH_SECRET_KEY`, database credentials, and allowed origins/hosts.
- Use durable object/file storage instead of the local volume when deploying more than
  one API or worker instance.
- Configure SMTP for password recovery; the local outbox is intended only for development.
- Terminate TLS at the hosting platform or reverse proxy and keep PostgreSQL and Redis on
  private networks.
- Back up both PostgreSQL and document storage, and test restoration regularly.

## Environment

Copy `.env.example` to `.env`. The example contains development-only placeholders and no real secrets.
