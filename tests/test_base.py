import time
import unittest
from unittest.mock import patch

from fubon_agent.agent_commands import AgentCommand
from fubon_agent.api import base as base_module
from fubon_agent.api.base import OrderAgentBase, process_order


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


if __name__ == "__main__":
    unittest.main()
