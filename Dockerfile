# Slim by design: the default provider (`voyager`) and the credential-free
# `public` provider talk to LinkedIn over plain HTTP and never launch a browser,
# so shipping Chromium would add ~1.4 GB to the image for nothing.
#
# Only `PROVIDER=linkedin_scraper` needs a browser. To build for that, swap the
# base image for Playwright's — it has Chromium and its ~60 shared libraries
# already installed and version-matched to the pip package:
#
#     FROM mcr.microsoft.com/playwright/python:v1.49.1-noble
#
# `scripts/login.py` also needs a browser, but it runs on your machine, not here.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Dependencies first, so code edits do not invalidate the pip layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY config.json ./
COPY app ./app

# Where a session is cached when no writable volume is mounted.
RUN mkdir -p /app/.session

EXPOSE 8000

# One worker on purpose: the LinkedIn session is process-local state, so
# replicas would each need their own.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
