# NEXORA backend image (API, Celery workers, beat, migrations). Build context = repo root.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin tesseract-ocr-guj \
      libgomp1 postgresql-client curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend

# CPU-only torch keeps the image small (sentence-transformers dependency)
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install -r /tmp/requirements.txt

COPY backend/ /app/backend/

RUN useradd --create-home --uid 10001 nexora && mkdir -p /models && chown nexora /models
USER nexora

EXPOSE 8000
CMD ["uvicorn", "nexora.asgi:application", "--host", "0.0.0.0", "--port", "8000"]
