FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY bot ./bot
COPY server ./server

RUN pip install --no-cache-dir .

RUN mkdir -p /app/logs

CMD ["python", "-m", "bot.executor"]
