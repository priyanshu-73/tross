# Playwright's own image: Chromium plus the ~60 shared libraries it needs are
# already installed and version-matched to the `playwright` pip package. Building
# this from python:slim means apt-installing all of them by hand, and any drift
# between the browser build and the client library shows up as flaky launches.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Dependencies first so code edits do not invalidate the pip layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY config.json ./
COPY app ./app

# Default session location for local/docker runs. On Render this is overridden
# by SESSION_STATE_PATH=/data/storage_state.json, backed by a persistent disk.
RUN mkdir -p /app/.session

EXPOSE 8000

# Runs as root: Render mounts persistent disks root-owned, and dropping
# privileges here would leave the container unable to write the session cookie.
# One worker on purpose - the browser context and its LinkedIn session are
# process-local state, so replicas would each need their own login.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
