#!/usr/bin/env bash
set -euo pipefail

BUILD_VERSION=$(date '+%Y%m%d%H%M%S')
echo "==================================="
echo "BUILD_VERSION: $BUILD_VERSION"
echo "==================================="

# Build fubon-agent-app with shared BUILD_VERSION
sh /Users/roger_lo/Workspace/cloud-trading-agent/docker/agent_app/cloud_build_agent_app.sh $BUILD_VERSION

# Deploy individual agents using the same fubon-agent-app BUILD_VERSION.
# Add one line per onboarded trader (copy docker/roger_fubon/ as a template).
sh /Users/roger_lo/Workspace/cloud-trading-agent/docker/roger_fubon/cloud_build_deploy_roger.sh $BUILD_VERSION
