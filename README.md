# Document & Invoice Analyzer

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
