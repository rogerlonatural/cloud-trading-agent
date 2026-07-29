FROM asia-east1-docker.pkg.dev/etensword-order-agent/agents/agent-base:latest

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN pip install /app/

ENV FUBON_AGENT_CONF=/app/config/agent_settings.yaml
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

# workers=1: single process = single Fubon session.
# timeout aligns with Cloud Run --timeout 600; concurrency=50 holds long requests.
CMD gunicorn --workers 1 \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:${PORT} \
    --timeout 600 \
    --keep-alive 5 \
    main:app
