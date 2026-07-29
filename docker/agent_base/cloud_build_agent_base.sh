# NOTE
# After cloud build completed, be sure to update the agent_base version on
#  /Users/roger_lo/Workspace/cloud-trading-agent/Dockerfile
#BUILD_VERSION=$(date '+%Y%m%d%H%M%S')
BUILD_VERSION=latest
echo $BUILD_VERSION

PROJECT_ID=etensword-order-agent
SERVICE_NAME=fubon-agent-base
WORK_DIR=~/Workspace/cloud-trading-agent/docker/agent_base

# fubon_neo isn't on PyPI -- it's a manually-downloaded wheel from Fubon's
# authenticated SDK portal, staged once in a private bucket (check Fubon's SDK
# redistribution terms before treating this as routine CI input -- see plan doc).
WHEEL_BUCKET=gs://etensword-order-agent-fubon-sdk

# NOTE: execute following commands to authenticate docker build/push to GCP Container Registry
# gcloud auth login
# gcloud auth configure-docker

cd $WORK_DIR

# Pull the staged wheel into the build context (not committed to git)
gsutil cp $WHEEL_BUCKET/fubon_neo-*.whl .

# Use cloud build for cross-platform compatibility (builds on GCP x86_64 infrastructure)
# This avoids emulation issues when building linux/amd64 on ARM64 Mac
gcloud builds submit --timeout=3600 --tag asia-east1-docker.pkg.dev/$PROJECT_ID/agents/$SERVICE_NAME:$BUILD_VERSION

rm -f fubon_neo-*.whl
