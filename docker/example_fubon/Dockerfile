FROM asia-east1-docker.pkg.dev/etensword-order-agent/agents/fubon-agent-app:latest

COPY ./.cert/* /app/config/

ENV PORT=8080

# workers=1: single process = single Fubon session.
# timeout aligns with Cloud Run --timeout 600; concurrency=50 holds long requests.
CMD gunicorn --workers 1 \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:${PORT} \
    --timeout 600 \
    --keep-alive 5 \
    main:app
