# The operator console.
#
# Two stages: build the assets with Vite, then serve them from a slim Node
# runtime that also proxies /api to the backend. The build stage never ships,
# so the final image carries no toolchain.

# ---------------------------------------------------------------------------
# Stage 1 — build
# ---------------------------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app

# Dependencies first, so a source-only change reuses the cached layer.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./

# The Vite build needs no API access, so the backend URL is a placeholder.
ENV VITE_BACKEND_ORIGIN=/api
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2 — serve
# ---------------------------------------------------------------------------
FROM node:22-alpine AS runtime

WORKDIR /app

# The server has no dependencies beyond Node itself, so there is nothing to
# install here. Copy the built assets and the server.
COPY --from=build /app/dist ./dist
COPY frontend/server.mjs ./server.mjs

ENV NODE_ENV=production
ENV PORT=8003
ENV BACKEND_ORIGIN=http://backend:8002

EXPOSE 8003

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
  CMD node -e "fetch('http://127.0.0.1:8003/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.mjs"]
