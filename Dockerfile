# Fubon agent app image (linux/amd64 only — official fubon_neo Linux wheel is
# manylinux x86_64; no arm64 Linux wheel).
#
# Official Fubon docs do not provide a Dockerfile. Stage the Linux wheel from
# the SDK portal before build:
#   fubon_neo-*-cp37-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl
# cloud_build_agent_app.sh pulls it from GCS into the build context.
FROM --platform=linux/amd64 python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    build-essential \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# fubon_neo is not on PyPI — staged into the build context (gitignored).
COPY fubon_neo-*.whl /tmp/
RUN set -eux; \
    whl=$(ls /tmp/fubon_neo-*.whl); \
    echo "Installing wheel: $whl"; \
    case "$whl" in \
      *manylinux*x86_64*.whl) ;; \
      *) echo "ERROR: need Linux manylinux x86_64 fubon_neo wheel, got: $whl" >&2; exit 1 ;; \
    esac; \
    pip install --no-cache-dir --upgrade pip; \
    pip install --no-cache-dir "$whl"; \
    rm -f /tmp/fubon_neo-*.whl; \
    python -c "from fubon_neo.sdk import FubonSDK; print('fubon_neo OK', FubonSDK)"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir /app/

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
