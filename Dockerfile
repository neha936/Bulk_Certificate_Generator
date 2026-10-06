# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so this layer stays cached when only source code changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY static ./static

# Run as an unprivileged user. Only the storage directory needs to be writable.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/storage/certificates \
    && chown -R appuser:appuser /app/storage
USER appuser

ENV STORAGE_DIR=/app/storage/certificates \
    CERTIFICATE_LAYOUT_PATH=/app/static/templates/certificate_layout.json

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# One worker on purpose: BackgroundTasks run inside the worker process. To scale
# out, move processing to Celery workers (see README, "Scaling").
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
