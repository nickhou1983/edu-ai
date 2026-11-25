FROM python:3.12-slim-bookworm AS python-base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false \
    AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED=false
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
RUN uv sync --locked --extra hosted --no-dev --no-install-project

FROM python-base AS runtime
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH="/app/services/foundry-agent:/app/packages/domain" \
    OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false \
    AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED=false
WORKDIR /app
RUN groupadd --system --gid 10001 edu \
    && useradd --system --uid 10001 --gid edu --home-dir /nonexistent --shell /usr/sbin/nologin edu

COPY --from=python-build /opt/venv /opt/venv
COPY packages/domain/edu_domain /app/packages/domain/edu_domain
COPY services/foundry-agent/edu_agent /app/services/foundry-agent/edu_agent

USER 10001:10001
EXPOSE 8088
CMD ["python", "-m", "edu_agent.host"]
