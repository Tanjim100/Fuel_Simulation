FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer caches across code changes.
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code is baked into the image (no host bind-mount: this project's
# path contains spaces, which Docker Desktop refuses to mount).
COPY backend/app ./app
COPY backend/tests ./tests

# The reference client, so the drift guard (tests/test_client_parity.py) can
# compare it against the backend's copy inside the container. The build context
# is the project root for this reason.
COPY verifier/simulator_client.py ./reference/simulator_client.py

RUN mkdir -p /app/data

EXPOSE 8002

HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=5 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8002/api/health',timeout=4)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
