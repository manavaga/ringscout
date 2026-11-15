FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY pyproject.toml AGENTS.md ./
COPY src ./src

RUN pip install --no-cache-dir .

EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn ringscout.server:app --host 0.0.0.0 --port ${PORT:-8080} --ws-ping-interval 20 --ws-ping-timeout 30"]
