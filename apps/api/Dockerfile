FROM python:3.12-slim

WORKDIR /app

COPY alembic.ini ./
COPY apps/api/requirements-runtime.lock ./requirements-runtime.lock

RUN apt-get update \
    && apt-get install --no-install-recommends -y tesseract-ocr xmlsec1 \
    && tesseract --version \
    && tesseract --list-langs | grep -Fx eng \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --timeout 120 --retries 5 --no-cache-dir --requirement requirements-runtime.lock \
    && addgroup --system app \
    && adduser --system --ingroup app app \
    && mkdir -p /app/storage/documents /app/storage/password-reset-outbox

COPY apps ./apps

RUN chown -R app:app /app

USER app

EXPOSE 8000

CMD ["uvicorn", "apps.api.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]


