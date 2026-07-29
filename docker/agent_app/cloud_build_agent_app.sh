# NOTE:
# Be sure to update the root Dockerfile to the latest agent-base version if the
# fubon_neo SDK version changed

# Accept BUILD_VERSION as parameter, or generate if not provided
if [ -z "$1" ]; then
    BUILD_VERSION=$(date '+%Y%m%d%H%M%S')
    echo "No BUILD_VERSION provided, generated: $BUILD_VERSION"
else
    BUILD_VERSION=$1
    echo "Using provided BUILD_VERSION: $BUILD_VERSION"
fi

PROJECT_ID=etensword-order-agent
SERVICE_NAME=fubon-agent-app
WORK_DIR=~/Workspace/cloud-trading-agent

cd $WORK_DIR

# NOTE: execute following commands to authenticate docker build/push to GCP Container Registry
# gcloud auth login
# gcloud auth configure-docker

# Use cloud build for cross-platform compatibility (builds on GCP x86_64 infrastructure)
# This avoids emulation issues when building linux/amd64 on ARM64 Mac
gcloud builds submit --timeout=3600 --tag asia-east1-docker.pkg.dev/$PROJECT_ID/agents/$SERVICE_NAME:$BUILD_VERSION
