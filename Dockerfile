# ── Stage 1: build the React UI ───────────────────────────────────
FROM node:20-slim AS ui-builder
WORKDIR /ui
COPY ui/mira/package.json ui/mira/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/mira/ ./
RUN npm run build

# ── Stage 2: backend + bundled UI ─────────────────────────────────
FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/miracodeai/mira"
LABEL org.opencontainers.image.description="Self-hostable AI code reviewer"
LABEL org.opencontainers.image.licenses="Apache-2.0"

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir "/app[serve]"

# Pull the built UI in from stage 1. webhooks.create_app() picks this up
# automatically and serves it at / with SPA fallback.
COPY --from=ui-builder /ui/dist /app/ui_dist

EXPOSE 8000
CMD ["mira", "serve"]
