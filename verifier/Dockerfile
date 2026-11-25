FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first so code edits don't bust the layer cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY simulator_client.py verify_simulator.py verify_api.py ./

# Redis cache + verifier report share a volume so results survive restarts.
ENV REDIS_URL=redis://redis:6379/0 \
    SIMULATOR_BASE_URL=http://simulator-api:8000 \
    REPORT_PATH=/app/reports

RUN mkdir -p /app/reports

# Liveness: the verifier container itself must be up and able to reach Redis.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import os,redis,sys; \
r=redis.Redis.from_url(os.environ['REDIS_URL'],socket_connect_timeout=3); \
sys.exit(0 if r.ping() else 1)"

CMD ["uvicorn", "verify_api:app", "--host", "0.0.0.0", "--port", "8001"]
