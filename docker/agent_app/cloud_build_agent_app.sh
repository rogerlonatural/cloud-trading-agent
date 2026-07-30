#!/usr/bin/env bash
# Build/push the fubon-agent-app image from the repo-root Dockerfile.
# Base: python:3.12-slim-bookworm (linux/amd64) + staged Linux fubon_neo wheel.
#
# Usage:
#   sh docker/agent_app/cloud_build_agent_app.sh [BUILD_VERSION]

set -euo pipefail

if [ -z "${1:-}" ]; then
    BUILD_VERSION=$(date '+%Y%m%d%H%M%S')
    echo "No BUILD_VERSION provided, generated: $BUILD_VERSION"
else
    BUILD_VERSION=$1
    echo "Using provided BUILD_VERSION: $BUILD_VERSION"
fi

PROJECT_ID=etensword-order-agent
SERVICE_NAME=fubon-agent-app
WORK_DIR=~/Workspace/cloud-trading-agent

# fubon_neo is not on PyPI. Use the official Linux manylinux x86_64 wheel:
#   fubon_neo-*-cp37-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl
# Stage once in a private bucket (check Fubon redistribution terms).
WHEEL_BUCKET=gs://etensword-order-agent-fubon-sdk

# NOTE: authenticate docker/GCR once:
# gcloud auth login
# gcloud auth configure-docker

cd "$WORK_DIR"

# Stage wheel into build context (gitignored; not committed).
rm -f fubon_neo-*.whl
if gsutil -q ls "$WHEEL_BUCKET/fubon_neo-"*manylinux*x86_64*.whl >/dev/null 2>&1; then
  gsutil cp "$WHEEL_BUCKET/fubon_neo-"*manylinux*x86_64*.whl .
else
  echo "WARN: no manylinux x86_64 wheel prefix in bucket; copying fubon_neo-*.whl"
  gsutil cp $WHEEL_BUCKET/fubon_neo-*.whl .
fi

ls -la fubon_neo-*.whl
case "$(ls fubon_neo-*.whl)" in
  *manylinux*x86_64*.whl) echo "Wheel platform OK (manylinux x86_64)" ;;
  *) echo "ERROR: staged wheel is not Linux manylinux x86_64:" >&2
     ls -la fubon_neo-*.whl >&2
     exit 1
     ;;
esac

# Cloud Build runs on GCP x86_64 — avoids arm64 Mac emulation issues.
gcloud builds submit --timeout=3600 \
  --tag asia-east1-docker.pkg.dev/$PROJECT_ID/agents/$SERVICE_NAME:$BUILD_VERSION

rm -f fubon_neo-*.whl
echo "Pushed asia-east1-docker.pkg.dev/$PROJECT_ID/agents/$SERVICE_NAME:$BUILD_VERSION"
