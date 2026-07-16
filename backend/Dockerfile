FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN python -m pip install --no-cache-dir .

COPY deploy/worker-loop.sh /usr/local/bin/agentic-os-worker-loop
RUN chmod 0755 /usr/local/bin/agentic-os-worker-loop

CMD ["uvicorn", "agentic_os.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
