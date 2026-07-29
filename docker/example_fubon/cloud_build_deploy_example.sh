# Template for onboarding a new trader: copy this directory to
# docker/<trader>_fubon/, add its .cert/ (agent_settings.yaml + .pfx),
# rename SERVICE_NAME below, and add a line to ../../deploy_all_agents.sh.
# No code changes are needed to onboard a new account.

# Accept AGENT_APP_VERSION as parameter, or generate if not provided
if [ -z "$1" ]; then
    AGENT_APP_VERSION=$(date '+%Y%m%d%H%M%S')
    echo "No AGENT_APP_VERSION provided, generated: $AGENT_APP_VERSION"
else
    AGENT_APP_VERSION=$1
    echo "Using provided AGENT_APP_VERSION: $AGENT_APP_VERSION"
fi

BUILD_VERSION=$(date '+%Y%m%d%H%M%S')
echo "example agent BUILD_VERSION: $BUILD_VERSION"

PROJECT_ID=etensword-order-agent
SERVICE_NAME=example-fubon
REGION=asia-northeast1
WORK_DIR=~/Workspace/cloud-trading-agent/docker/example_fubon/

cd $WORK_DIR

# Update Dockerfile to use the specified agent-app version
sed -i.bak "s|FROM asia-east1-docker.pkg.dev/$PROJECT_ID/agents/fubon-agent-app:.*|FROM asia-east1-docker.pkg.dev/$PROJECT_ID/agents/fubon-agent-app:$AGENT_APP_VERSION|" Dockerfile
echo "Updated Dockerfile to use fubon-agent-app:$AGENT_APP_VERSION"

# Use cloud build for cross-platform compatibility (builds on GCP x86_64 infrastructure)
gcloud builds submit --timeout=3600 --tag asia-east1-docker.pkg.dev/$PROJECT_ID/agents/$SERVICE_NAME:$BUILD_VERSION

# NOTE:
#    After first deploy, run scripts/add_cloud_run_permission.sh for this
#    SERVICE_NAME/REGION so Pub/Sub push can invoke it, then coordinate with
#    the slash-futures repo owner to add its subscription.
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
