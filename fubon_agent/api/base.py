import dataclasses
import json
import time
import traceback
from enum import Enum

import requests

from fubon_agent import get_config
from fubon_agent.agent_commands import AgentCommand
from fubon_agent.agent_logging import get_logger

logger = get_logger("FubonAgent")
_config = get_config()
_cmd_history = {}

FEEDBACK_URL = "https://asia-east2-etensword.cloudfunctions.net/api_send_agent_feedback"


def to_jsonable(obj, _depth: int = 0):
    """Best-effort convert Fubon SDK models (OrderResult, FilledData, …) to
    JSON-serializable plain data.

    SDK objects are not dataclasses and often only expose a multi-line ``str``;
    walking public attributes / ``__dict__`` keeps logs and feedback usable.
    """
    if _depth > 8:
        return " ".join(str(obj).split())
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v, _depth + 1) for v in obj]
    if isinstance(obj, Enum):
        return obj.value
    for meth_name in ("to_dict", "dict", "model_dump", "as_dict"):
        meth = getattr(obj, meth_name, None)
        if callable(meth):
            try:
                return to_jsonable(meth(), _depth + 1)
            except Exception:
                pass
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        try:
            return to_jsonable(dataclasses.asdict(obj), _depth + 1)
        except Exception:
            pass
    data = getattr(obj, "__dict__", None)
    if isinstance(data, dict) and data:
        out = {"_type": type(obj).__name__}
        for key, val in data.items():
            if str(key).startswith("_"):
                continue
            out[str(key)] = to_jsonable(val, _depth + 1)
        return out
    # Slotted / C-extension style models: public non-callable attributes.
    out = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            val = getattr(obj, name)
        except Exception:
            continue
        if callable(val):
            continue
        out[name] = to_jsonable(val, _depth + 1)
    if out:
        out["_type"] = type(obj).__name__
        return out
    return " ".join(str(obj).split())


def format_inline(obj) -> str:
    """Single-line JSON (or collapsed str) for Cloud Logging readability.

    Multi-line SDK ``__str__`` dumps (OrderResult / FilledData / Account) split
    into separate log entries and reverse field order in the console — always
    emit one compact line instead.
    """
    try:
        return json.dumps(
            to_jsonable(obj),
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
    except Exception:
        return " ".join(str(obj).split())


class OrderAgentBase(object):
    def __init__(self, config=None):
        if not config:
            config = get_config()
        self.config = config
        self.trace_id = None
        self.agent_id = None
        self.account_id = None

    def run(self, payload):
        command = payload["command"]
        self.trace_id = payload["command_id"]
        self.agent_id = payload["agent"]
        try:
            props = payload["props"] if "props" in payload else {}
            expire_at = (
                payload["expire_at"]
                if "expire_at" in payload
                else payload["publish_time"] + AgentCommand.COMMAND_TIMEOUT
            )
            if time.time() > expire_at and command != AgentCommand.CHECK_OPEN_INTEREST:
                return [
                    dict(
                        api=command,
                        success=False,
                        result="Command is not executed because it is expired (Timeout=%s sec)"
                        % AgentCommand.COMMAND_TIMEOUT,
                    )
                ]

            if command == AgentCommand.CHECK_AGENT:
                return [dict(api=command, success=True, result="I am great!")]

            # Real agent related commands below
            self.InitAgent(self.agent_id)

            product = props["product"]
            price = props["price"] if "price" in props else None
            qty = props["qty"] if "qty" in props else 0
            # Cap pure BUY so final long never exceeds max_qty when set
            # (etensword-agent a5009bd). Omit / 0 => previous uncapped BUY.
            max_qty = (
                int(props["max_qty"])
                if props.get("max_qty") not in (None, "")
                else 0
            )

            if command == AgentCommand.CHECK_OPEN_INTEREST:
                return self.HasOpenInterest(product)

            if command == AgentCommand.MAYDAY:
                return self.MayDay(product, price)

            if command == AgentCommand.CLOSE_AND_SELL:
                return self.CloseAndSell(product, price, qty)

            if command == AgentCommand.CLOSE_AND_BUY:
                return self.CloseAndBuy(product, price, qty)

            if command == AgentCommand.SELL:
                return self.Sell(product, price, qty)

            if command == AgentCommand.BUY:
                return self.Buy(product, price, qty, max_qty=max_qty)

        except Exception:
            msg = "Failed to execute command %s. Error: %s" % (
                command,
                traceback.format_exc().replace("\n", " | "),
            )
            logger.error("[%s] %s" % (self.trace_id, msg))
            return [
                dict(
                    api=command,
                    success=False,
                    result=traceback.format_exc().replace("\n", " | "),
                )
            ]

    def ListProfitLoss(self, begin_date: str, end_date: str):
        raise NotImplementedError()

    def HasOpenInterest(self, product):
        raise NotImplementedError()

    def MayDay(self, product, price=None):
        raise NotImplementedError()

    def CloseAndSell(self, product, price, qty=1):
        raise NotImplementedError()

    def CloseAndBuy(self, product, price, qty=1):
        raise NotImplementedError()

    def Sell(self, product, price, qty=1):
        raise NotImplementedError()

    def Buy(self, product, price, qty=1, max_qty=0):
        raise NotImplementedError()

    def InitAgent(self, agent_id):
        pass

    def CloseAgent(self):
        pass


class NeedRetryError(RuntimeError):
    pass


class NotEnoughMoneyError(RuntimeError):
    pass


def process_order(order_agent: OrderAgentBase, order_payload: dict) -> bool:
    agent_id = order_payload["agent"] if "agent" in order_payload else ""
    command_id = order_payload["command_id"]
    command = order_payload["command"]
    logger.info(f"[{command_id}] Process order: {order_payload}")

    if is_command_for_other_agent(agent_id):
        logger.info(f"[{command_id}] Skip command of other agent")
        return False

    if not _register_command(command_id):
        logger.info(f"[{command_id}] Skip handled command")
        return False

    responses = order_agent.run(order_payload)

    logger.info(f"[{command_id}] Publish feedback: {responses}")
    feedback_execution_result(
        agent_id,
        command,
        command_id,
        {"success": responses[-1]["success"], "results": responses},
    )
    return responses[-1]["success"]


def is_command_for_other_agent(agent_id):
    """Return True if this process is not configured to handle agent_id."""
    mapping = _config.get("agent_account_mapping")
    if mapping:
        agent_ids = list(mapping.keys())
        if agent_id and agent_id not in agent_ids:
            return True
    elif agent_id != _config["order_agent"]["agent_id"]:
        return True
    return False


# Backward-compatible private alias
_is_command_for_other_agent = is_command_for_other_agent


def _register_command(command_id: str) -> bool:
    if command_id in _cmd_history:
        return False
    logger.info(f"[{command_id}] was registered")
    _cmd_history[command_id] = 1
    return True


def _send_agent_feedback(payload, command_id=""):
    """POST execution result to slash-futures feedback CF.

    Expected 204. 4xx is usually permanent (e.g. FuturesBot never logged the
    command_id — live 2026-08-05: MySQL command_id VARCHAR overflow for long
    agent names like roger_fubon) so we fail fast instead of holding the order
    lock for ~15s of useless retries.
    """
    retry = 0
    status_code = None
    response_text = None
    headers = {"Content-Type": "application/json"}
    while True:
        try:
            response = requests.post(
                FEEDBACK_URL,
                data=json.dumps(payload),
                headers=headers,
                timeout=15,
            )
            status_code = response.status_code
            response_text = response.text
            if status_code == 204:
                logger.info("[%s] Feedback sent OK" % command_id)
                return
            # Permanent client errors: do not burn order-lock time retrying.
            if 400 <= status_code < 500:
                logger.info(
                    "[%s] Failed to feedback (no retry) %s %s"
                    % (command_id, status_code, response_text)
                )
                return
        except Exception:
            logger.info(
                "[%s] Failed to send feedback, retry, %s"
                % (command_id, traceback.format_exc().replace("\n", ">>"))
            )

        if retry > 3:
            logger.info(
                "[%s] Failed to feedback after retry %s %s"
                % (command_id, status_code, response_text)
            )
            return
        retry += 1
        time.sleep(retry)


def feedback_execution_result(agent_id, command, command_id, result):
    try:
        # SDK callback payloads (FilledData/OrderResult) are not natively JSON
        # serializable — convert first so feedback CF + logs do not explode.
        plain = to_jsonable(result)
        logger.info(
            "[%s] Feedback command result: %s", command_id, format_inline(plain)
        )
        msg_object = {
            "command": command,
            "message": {
                "execution_result": json.dumps(plain, ensure_ascii=False, default=str)
            },
            "agent": agent_id,
            "command_id": command_id,
        }
        _send_agent_feedback(msg_object, command_id)
    except Exception as e:
        # Do not pass exception as logger args after an f-string (causes
        # "--- Logging error ---" on record.getMessage()).
        logger.info(
            "[%s] Failed to feedback execution result, ignored: %s",
            command_id,
            e,
        )
