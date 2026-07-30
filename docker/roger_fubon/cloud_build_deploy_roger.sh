#!/usr/bin/env bash
# Deploy roger's Fubon Cloud Run agent.
# Requires docker/roger_fubon/.cert/ (agent_settings.yaml + .pfx) before build.

set -euo pipefail

# Accept AGENT_APP_VERSION as parameter, or generate if not provided
if [ -z "${1:-}" ]; then
    AGENT_APP_VERSION=$(date '+%Y%m%d%H%M%S')
    echo "No AGENT_APP_VERSION provided, generated: $AGENT_APP_VERSION"
else
    AGENT_APP_VERSION=$1
    echo "Using provided AGENT_APP_VERSION: $AGENT_APP_VERSION"
fi

BUILD_VERSION=$(date '+%Y%m%d%H%M%S')
echo "roger-fubon agent BUILD_VERSION: $BUILD_VERSION"

PROJECT_ID=etensword-order-agent
SERVICE_NAME=roger-fubon
REGION=asia-northeast1
WORK_DIR=~/Workspace/cloud-trading-agent/docker/roger_fubon/

cd "$WORK_DIR"

if [ ! -d .cert ] || [ -z "$(ls -A .cert 2>/dev/null || true)" ]; then
  echo "ERROR: docker/roger_fubon/.cert/ is missing or empty." >&2
  echo "Copy agent_settings.yaml + .pfx (and api-key if used) into .cert/ before deploy." >&2
  exit 1
fi

# Update Dockerfile to use the specified agent-app version
sed -i.bak "s|FROM asia-east1-docker.pkg.dev/$PROJECT_ID/agents/fubon-agent-app:.*|FROM asia-east1-docker.pkg.dev/$PROJECT_ID/agents/fubon-agent-app:$AGENT_APP_VERSION|" Dockerfile
echo "Updated Dockerfile to use fubon-agent-app:$AGENT_APP_VERSION"

# Use cloud build for cross-platform compatibility (builds on GCP x86_64 infrastructure)
gcloud builds submit --timeout=3600 --tag asia-east1-docker.pkg.dev/$PROJECT_ID/agents/$SERVICE_NAME:$BUILD_VERSION

# NOTE:
#    After first deploy, run scripts/add_cloud_run_permission.sh so Pub/Sub
#    push can invoke this service, then coordinate with the slash-futures repo
#    owner to add its subscription.
#  Fubon: max 10 concurrent connections per app. Keep max-instances=1 so
#  Cloud Run never opens multiple Fubon logins for the same agent service.
#  min-instances=1 avoids cold-start login stampede at market open.
#  concurrency>=50: one instance can accept >=50 in-flight HTTP requests (Pub/Sub
#  burst). Order execution is serialized in-process by a lock (see main.py).
gcloud run deploy $SERVICE_NAME \
       --image asia-east1-docker.pkg.dev/$PROJECT_ID/agents/$SERVICE_NAME:$BUILD_VERSION \
       --cpu 2 \
       --memory 4G \
       --region $REGION \
       --platform managed \
       --no-allow-unauthenticated \
       --timeout 600 \
       --concurrency=50 \
       --min-instances 1 \
       --max-instances 1
