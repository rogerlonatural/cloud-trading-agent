import json
import time
import traceback

import requests

from fubon_agent import get_config
from fubon_agent.agent_commands import AgentCommand
from fubon_agent.agent_logging import get_logger

logger = get_logger("FubonAgent")
_config = get_config()
_cmd_history = {}

FEEDBACK_URL = "https://asia-east2-etensword.cloudfunctions.net/api_send_agent_feedback"


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
    retry = 0
    status_code = None
    response_text = None
    while True:
        try:
            response = requests.post(FEEDBACK_URL, data=json.dumps(payload))
            status_code = response.status_code
            response_text = response.text
            if status_code == 204:
                logger.info("[%s] Feedback sent OK" % command_id)
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
        logger.info(f"[{command_id}] Feedback command result: {result}")
        msg_object = {
            "command": command,
            "message": {"execution_result": json.dumps(result)},
            "agent": agent_id,
            "command_id": command_id,
        }
        _send_agent_feedback(msg_object, command_id)
    except Exception as e:
        logger.info(f"[{command_id}] Failed to feedback execution result, ignored", e)
        pass
