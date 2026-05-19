FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

COPY . .

RUN uv sync --frozen --no-dev

EXPOSE 8080

CMD ["uv", "run", "uvicorn", "openbot.entrypoints.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
