FROM public.ecr.aws/docker/library/docker:29-cli@sha256:6fae2214c3121a28d5aa639d09032f61310b7c9c75ffbad1025f261a1b621800 AS docker-cli
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS app
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
RUN useradd --uid 10001 --create-home appuser \
    && mkdir -p /app/data/uploads /app/data/parsed \
    && chown -R appuser:appuser /app/data
USER appuser
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

# Only the explicitly selected Run Worker target contains the trusted CLI.
FROM app AS run-worker
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

FROM app AS api
