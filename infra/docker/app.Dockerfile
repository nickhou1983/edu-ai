FROM node:22-bookworm-slim AS web-build

WORKDIR /app/apps/web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci

COPY apps/web/index.html ./index.html
COPY apps/web/tsconfig.json apps/web/vite.config.ts ./
COPY apps/web/src ./src
COPY apps/web/public ./public
RUN npm run build

FROM python:3.12-slim-bookworm AS python-base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

FROM python-base AS python-build
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /uvx /bin/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --extra azure --no-dev --no-install-project

FROM python-base AS runtime
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH="/app/services/app:/app/services/foundry-agent:/app/packages/domain" \
    EDU_MODE=azure \
    EDU_WEB_DIR="/app/apps/web/dist"
WORKDIR /app
RUN groupadd --system --gid 10001 edu \
    && useradd --system --uid 10001 --gid edu --home-dir /nonexistent --shell /usr/sbin/nologin edu

COPY --from=python-build /opt/venv /opt/venv
COPY --from=web-build /app/apps/web/dist /app/apps/web/dist
COPY packages/domain/edu_domain /app/packages/domain/edu_domain
COPY services/app/edu_app /app/services/app/edu_app
COPY services/foundry-agent/edu_agent/__init__.py services/foundry-agent/edu_agent/demo.py services/foundry-agent/edu_agent/english_demo.py /app/services/foundry-agent/edu_agent/
COPY infra/release/bootstrap_db.py /app/bootstrap_db.py
RUN python -c "import edu_app.api; import edu_app.jobs; import edu_agent.english_demo"

USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "edu_app", "serve"]
