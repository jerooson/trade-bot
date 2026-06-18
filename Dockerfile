FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install Node.js 18 (must match host version so codex native addons work)
# expect provides `unbuffer` which forces line-buffered stdout from Node.js subprocesses
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates expect \
    && curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY bot ./bot
COPY server ./server

RUN pip install --no-cache-dir .

RUN mkdir -p /app/logs

CMD ["python", "-m", "bot.executor"]
