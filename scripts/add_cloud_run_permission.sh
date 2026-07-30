# NOTE: 每次新增 order agent 的Cloud Run 時使用
# 使用後須去新增order agent的subscription => slash_futures/pub_sub/add_order_agent_subscription.sh

PROJECT_ID=etensword-order-agent
SERVICE_ACCOUNT=cloud-run-pubsub-invoker@etensword.iam.gserviceaccount.com

SERVICE_NAME=roger-fubon
REGION=asia-northeast1

gcloud run services add-iam-policy-binding $SERVICE_NAME \
  --member="serviceAccount:$SERVICE_ACCOUNT" \
  --role="roles/run.invoker" \
  --region=$REGION \
  --project=$PROJECT_ID

gcloud run services get-iam-policy $SERVICE_NAME --region=$REGION --project=$PROJECT_ID
