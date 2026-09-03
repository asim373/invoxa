FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY alembic.ini ./

RUN apt-get update \
    && apt-get install --no-install-recommends -y tesseract-ocr \
    && tesseract --version \
    && tesseract --list-langs | grep -Fx eng \
    && rm -rf /var/lib/apt/lists/*

COPY apps ./apps

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[dev]" \
    && addgroup --system app \
    && adduser --system --ingroup app app \
    && mkdir -p /app/storage/documents /app/storage/password-reset-outbox \
    && chown -R app:app /app

USER app

EXPOSE 8000

CMD ["uvicorn", "apps.api.app.main:app", "--host", "0.0.0.0", "--port", "8000"]


