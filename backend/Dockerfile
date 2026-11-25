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

# The scenario configuration. It is the source of truth for the demand table,
# the regional multipliers, the clock origin and the seed -- see
# `docs/SCENARIO.md`. The path below must match `SCENARIO_PATH`.
COPY backend/scenarios ./scenarios
ENV SCENARIO_PATH=/app/scenarios/baseline.yaml

# The reference client, so the drift guard (tests/test_client_parity.py) can
# compare it against the backend's copy inside the container. The build context
# is the project root for this reason.
COPY verifier/simulator_client.py ./reference/simulator_client.py

# The compose file, so the settings' drift guard (tests/test_config_defaults.py)
# can check that the code's defaults agree with the values the stack is
# actually deployed with. Same reason: the build context is the project root.
COPY docker-compose.yml ./reference/docker-compose.yml

RUN mkdir -p /app/data

EXPOSE 8002

HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=5 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8002/api/health',timeout=4)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
