FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first (better layer caching).
COPY dashboard/requirements.txt ./dashboard/requirements.txt
RUN pip install --no-cache-dir -r dashboard/requirements.txt

# App code. No secrets are baked in — all config comes from env vars
# (DASHBOARD_PASSWORD, GOOGLE_SERVICE_ACCOUNT_JSON, TARGETS_JSON, DASHBOARD_SECRET).
COPY dashboard/app.py dashboard/set_password.py ./dashboard/
COPY dashboard/targets.json ./dashboard/
COPY dashboard/templates ./dashboard/templates
COPY dashboard/static ./dashboard/static
COPY data/sheets_db.py data/sheets_sa.py ./data/

WORKDIR /app/dashboard
EXPOSE 8000

# Render injects $PORT; default to 8000 for local docker runs.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
