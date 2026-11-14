# Settlewell hosted demo: one container serves the FastAPI backend and the built React UI.
# Build:  docker build -t settlewell .
# Run:    docker run -p 8080:8080 -e SETTLEWELL_DEMO_PASSCODE=... settlewell
# No secrets are baked in; every credential arrives as a runtime environment variable.

FROM node:20-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS app
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ backend/
COPY settlewell/ settlewell/
COPY --from=ui /ui/dist frontend/dist
RUN useradd --create-home --uid 10001 settlewell \
    && mkdir -p /data /tmp/settlewell \
    && chown -R settlewell /data /tmp/settlewell
USER settlewell
ENV SETTLEWELL_MODE=mock \
    SETTLEWELL_STATIC_DIR=/app/frontend/dist \
    PORT=8080
EXPOSE 8080
CMD ["python", "-m", "backend.hosted"]
