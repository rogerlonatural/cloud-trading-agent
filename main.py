import asyncio
import base64
import json
import logging
import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request
from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage
from pydantic import BaseModel

from fubon_agent import get_config
from fubon_agent.api.base import is_command_for_other_agent, process_order
from fubon_agent.api.fubon_api import get_or_create_agent

# Cloud Run concurrency target: accept many in-flight requests on a single instance.
# Fubon session is not safe for parallel place/query; serialize agent API use.
MAX_IN_FLIGHT_REQUESTS = 50
_request_executor = ThreadPoolExecutor(
    max_workers=MAX_IN_FLIGHT_REQUESTS,
    thread_name_prefix="pubsub-req",
)
_order_agent_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    agent = get_or_create_agent()  # startup: login once
    yield
    agent.shutdown()  # shutdown: logout
    _request_executor.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

conf_file = os.getenv("FUBON_AGENT_CONF")
logger.info(
    f"Get conf_file path from environment variable FUBON_AGENT_CONF: {conf_file}"
)

# Load config to set GOOGLE_APPLICATION_CREDENTIALS before creating storage client
config = get_config()
logger.info(
    f"Loaded config and set GOOGLE_APPLICATION_CREDENTIALS: "
    f"{os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')}"
)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "etensword-order-agent")
logger.info(
    f"Get PROJECT_ID from environment variable GOOGLE_CLOUD_PROJECT: {PROJECT_ID}"
)

storage_client = storage.Client(project=PROJECT_ID)
SLASHFUTURES_ORDERS_BUCKET = "slashfutures_orders"
logger.info(f"Get storage client for bucket {SLASHFUTURES_ORDERS_BUCKET}")


class PubSubMessage(BaseModel):
    message: Dict[str, Any]
    subscription: str


def upload_blob(command_id: str, content: str):
    try:
        bucket = storage_client.bucket(SLASHFUTURES_ORDERS_BUCKET)
        blob = bucket.blob(command_id)
        blob.upload_from_string(content)
        logger.info(
            f"[{command_id}] upload blob to bucket {SLASHFUTURES_ORDERS_BUCKET} successfully"
        )
    except Exception:
        logger.error(
            f"[{command_id}] failed to upload blob to bucket {SLASHFUTURES_ORDERS_BUCKET}, "
            f"error: {traceback.format_exc()}"
        )


def is_blob_exist(command_id: str):
    try:
        bucket = storage_client.bucket(SLASHFUTURES_ORDERS_BUCKET)
        blob = bucket.blob(command_id)
        return blob.exists()
    except Exception:
        logger.error(
            f"[{command_id}] failed to check blob exist {SLASHFUTURES_ORDERS_BUCKET}, "
            f"error: {traceback.format_exc()}"
        )
    return False


def try_claim_command(command_id: str, content: str) -> bool:
    """
    Atomically claim a command_id in GCS so only one instance processes it.

    Uses if_generation_match=0 (create only if the object does not exist).
    Returns True if this instance won the claim; False if already claimed.
    Raises on unexpected GCS errors so Pub/Sub can retry.
    """
    bucket = storage_client.bucket(SLASHFUTURES_ORDERS_BUCKET)
    blob = bucket.blob(command_id)
    try:
        blob.upload_from_string(content, if_generation_match=0)
        logger.info(
            f"[{command_id}] claimed command in bucket {SLASHFUTURES_ORDERS_BUCKET}"
        )
        return True
    except PreconditionFailed:
        logger.info(
            f"[{command_id}] command already claimed/processed "
            f"(GCS generation precondition)"
        )
        return False
    except Exception as e:
        err = str(e)
        if "412" in err or "Precondition Failed" in err or "precondition" in err.lower():
            logger.info(
                f"[{command_id}] command already claimed/processed (GCS {e})"
            )
            return False
        logger.error(
            f"[{command_id}] claim failed unexpectedly: {traceback.format_exc()}"
        )
        raise


def _handle_pubsub_payload(decoded_data: str) -> str:
    """
    Synchronous handler run in a worker thread.

    Claim is concurrent-safe (GCS). Fubon order path is under _order_agent_lock
    so max-instances=1 + concurrency=50 does not race the singleton session.
    Returns HTTP-style status marker: "204" or raises.
    """
    order_payload = json.loads(decoded_data)
    if "command_id" not in order_payload or "agent" not in order_payload:
        logger.info(f"invalid payload: {decoded_data}")
        return "204"

    command_id = order_payload["command_id"]
    agent_id = order_payload["agent"]

    if is_command_for_other_agent(agent_id):
        logger.info(f"[{command_id}] Skip command of other agent (pre-claim)")
        return "204"

    if not try_claim_command(command_id, decoded_data):
        return "204"

    with _order_agent_lock:
        order_agent = get_or_create_agent()
        try:
            process_order(order_agent, order_payload)
        finally:
            order_agent.CloseAgent()

    return "204"


@app.post("/")
async def push_message_from_pubsub(request: Request, body: PubSubMessage):
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(status_code=400, detail="Invalid content type")

    try:
        pubsub_data = body.message.get("data")
        if not pubsub_data:
            raise ValueError("No data field in message")

        decoded_data = base64.b64decode(pubsub_data).decode("utf-8")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            _request_executor, _handle_pubsub_payload, decoded_data
        )
    except Exception:
        logger.error(f"error on process order but ignored: {traceback.format_exc()}")
        return "", 500

    return "", 204


@app.get("/health")
async def health_check():
    """Cloud Run health check (does not take the order lock; safe under concurrency)."""
    return {"status": "healthy"}
