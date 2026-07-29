# No official Fubon-provided base image exists (unlike Shioaji's sinotrade/shioaji)
# -- build off a plain slim Python image and install the fubon_neo wheel that
# cloud_build_agent_base.sh stages into this directory before the build (see
# plan doc: the wheel is not on PyPI, must be manually downloaded once from
# Fubon's authenticated SDK portal and staged in a private GCS bucket).
FROM --platform=linux/amd64 python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    build-essential \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Staged by cloud_build_agent_base.sh via `gsutil cp` immediately before
# `gcloud builds submit` -- not committed to git (see .gitignore).
COPY fubon_neo-*.whl /tmp/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir /tmp/fubon_neo-*.whl \
    && rm -f /tmp/fubon_neo-*.whl

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
