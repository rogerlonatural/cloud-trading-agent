import json
import time
import unittest
from enum import Enum
from unittest.mock import patch

from fubon_agent.agent_commands import AgentCommand
from fubon_agent.api import base as base_module
from fubon_agent.api.base import (
    OrderAgentBase,
    feedback_execution_result,
    format_inline,
    process_order,
    to_jsonable,
)


class FakeOrderAgent(OrderAgentBase):
    def __init__(self):
        super().__init__(config={"order_agent": {"agent_id": "test-agent"}})
        self.init_called_with = None
        self.buy_calls = []

    def InitAgent(self, agent_id):
        self.init_called_with = agent_id

    def HasOpenInterest(self, product):
        return [dict(api="HasOpenInterest", success=True, result=product)]

    def MayDay(self, product, price=None):
        return [dict(api="MayDay", success=True, result=product)]

    def Buy(self, product, price, qty=1, max_qty=0):
        self.buy_calls.append(
            dict(product=product, price=price, qty=qty, max_qty=max_qty)
        )
        return [dict(api="Buy", success=True, result="ok")]


def make_payload(command, **overrides):
    payload = dict(
        command_id="cmd-1",
        agent="test-agent",
        command=command,
        props={"product": "TXF"},
        publish_time=time.time(),
    )
    payload.update(overrides)
    return payload


class TestCommandDispatch(unittest.TestCase):
    def setUp(self):
        self.agent = FakeOrderAgent()

    def test_check_agent_bypasses_init_agent(self):
        result = self.agent.run(make_payload(AgentCommand.CHECK_AGENT))
        self.assertTrue(result[0]["success"])
        self.assertIsNone(self.agent.init_called_with)

    def test_dispatches_check_open_interest_and_calls_init_agent(self):
        result = self.agent.run(make_payload(AgentCommand.CHECK_OPEN_INTEREST))
        self.assertEqual(self.agent.init_called_with, "test-agent")
        self.assertEqual(result[0]["result"], "TXF")

    def test_expired_command_short_circuits_before_init_agent(self):
        payload = make_payload(AgentCommand.MAYDAY, publish_time=time.time() - 1000)
        result = self.agent.run(payload)
        self.assertFalse(result[0]["success"])
        self.assertIn("expired", result[0]["result"])
        self.assertIsNone(self.agent.init_called_with)

    def test_check_open_interest_is_exempt_from_expiry(self):
        payload = make_payload(AgentCommand.CHECK_OPEN_INTEREST, publish_time=time.time() - 1000)
        result = self.agent.run(payload)
        self.assertEqual(self.agent.init_called_with, "test-agent")
        self.assertTrue(result[0]["success"])

    def test_buy_passes_max_qty_from_props(self):
        payload = make_payload(
            AgentCommand.BUY,
            props={"product": "TXF", "price": 100, "qty": 3, "max_qty": 5},
        )
        result = self.agent.run(payload)
        self.assertTrue(result[0]["success"])
        self.assertEqual(len(self.agent.buy_calls), 1)
        self.assertEqual(
            self.agent.buy_calls[0],
            dict(product="TXF", price=100, qty=3, max_qty=5),
        )

    def test_buy_defaults_max_qty_to_zero_when_omitted(self):
        payload = make_payload(
            AgentCommand.BUY,
            props={"product": "TXF", "price": 100, "qty": 2},
        )
        self.agent.run(payload)
        self.assertEqual(self.agent.buy_calls[0]["max_qty"], 0)


class TestProcessOrderDedup(unittest.TestCase):
    def setUp(self):
        base_module._cmd_history.clear()

    @patch("fubon_agent.api.base.requests.post")
    def test_process_order_success_sends_feedback(self, mock_post):
        mock_post.return_value.status_code = 204
        mock_post.return_value.text = ""
        agent = FakeOrderAgent()
        result = process_order(agent, make_payload(AgentCommand.CHECK_AGENT, command_id="cmd-dedup-1"))
        self.assertTrue(result)
        mock_post.assert_called_once()

    @patch("fubon_agent.api.base.requests.post")
    def test_second_call_with_same_command_id_is_skipped(self, mock_post):
        mock_post.return_value.status_code = 204
        mock_post.return_value.text = ""
        agent = FakeOrderAgent()
        payload = make_payload(AgentCommand.CHECK_AGENT, command_id="cmd-dedup-2")
        process_order(agent, payload)
        result = process_order(agent, payload)
        self.assertFalse(result)
        mock_post.assert_called_once()

    @patch("fubon_agent.api.base.requests.post")
    def test_command_for_unmapped_agent_id_is_skipped(self, mock_post):
        agent = FakeOrderAgent()
        payload = make_payload(AgentCommand.CHECK_AGENT, command_id="cmd-dedup-3", agent="unknown-agent")
        result = process_order(agent, payload)
        self.assertFalse(result)
        mock_post.assert_not_called()


class _FakeSide(Enum):
    Buy = "Buy"
    Sell = "Sell"


class FakeFilledData:
    """Mimics multi-line Fubon FilledData (not JSON-serializable via default)."""

    def __init__(self):
        self.stock_no = "2501"
        self.order_no = "o4616"
        self.filled_qty = 1000
        self.filled_price = 22.15
        self.buy_sell = _FakeSide.Sell
        self.account = "71247"

    def __str__(self):
        return (
            "FilledData {\n"
            "    stock_no: \"2501\",\n"
            "    order_no: \"o4616\",\n"
            "    filled_qty: 1000,\n"
            "}"
        )


class TestSdkModelLogging(unittest.TestCase):
    def test_to_jsonable_walks_public_attrs(self):
        plain = to_jsonable(FakeFilledData())
        self.assertEqual(plain["stock_no"], "2501")
        self.assertEqual(plain["filled_qty"], 1000)
        self.assertEqual(plain["buy_sell"], "Sell")
        self.assertEqual(plain["_type"], "FakeFilledData")

    def test_format_inline_is_single_line_json(self):
        line = format_inline(FakeFilledData())
        self.assertNotIn("\n", line)
        parsed = json.loads(line)
        self.assertEqual(parsed["order_no"], "o4616")

    def test_format_inline_collapses_multiline_str_fallback(self):
        class Weird:
            def __str__(self):
                return "OrderResult {\n    status: 10,\n}"

        line = format_inline(Weird())
        self.assertNotIn("\n", line)
        self.assertIn("OrderResult", line)

    @patch("fubon_agent.api.base.requests.post")
    def test_feedback_serializes_sdk_filled_data(self, mock_post):
        mock_post.return_value.status_code = 204
        mock_post.return_value.text = ""
        feedback_execution_result(
            "agent-1",
            AgentCommand.DEAL_CALLBACK,
            "cmd-filled-1",
            FakeFilledData(),
        )
        mock_post.assert_called_once()
        body = json.loads(mock_post.call_args.kwargs["data"])
        # Nested execution_result is a JSON string of the plain dict
        exec_result = json.loads(body["message"]["execution_result"])
        self.assertEqual(exec_result["stock_no"], "2501")
        self.assertEqual(exec_result["buy_sell"], "Sell")


if __name__ == "__main__":
    unittest.main()
