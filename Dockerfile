FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install Node.js (required for codex CLI which is mounted from the host)
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY bot ./bot
COPY server ./server

RUN pip install --no-cache-dir .

RUN mkdir -p /app/logs

CMD ["python", "-m", "bot.executor"]
