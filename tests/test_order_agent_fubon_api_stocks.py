import unittest
from unittest.mock import MagicMock

from fubon_agent.api.fubon_api import OrderAgent


def make_agent():
    agent = OrderAgent()
    agent.sdk = MagicMock()
    agent.current_account = MagicMock()
    agent._connected = True
    agent.trace_id = "test-trace"
    agent.agent_id = "test-agent"
    return agent


class TestStockOrderPathsAreTodoStubs(unittest.TestCase):
    """Stock order placement is explicitly out of scope for this phase (see plan
    doc) -- these tests guard the scope boundary: every stock command path
    (product.isnumeric()) must return a clear not-implemented result rather than
    silently doing nothing, crashing, or accidentally placing a futures order.
    """

    def setUp(self):
        self.agent = make_agent()

    def test_buy_stock_returns_not_implemented(self):
        result = self.agent.Buy("2330", price=500, qty=1)
        self.assertFalse(result[0]["success"])
        self.agent.sdk.futopt.place_order.assert_not_called()

    def test_sell_returns_not_implemented(self):
        # Sell was never implemented for stock OR futures in the reference repo either.
        result = self.agent.Sell("2330", price=500, qty=1)
        self.assertFalse(result[0]["success"])

    def test_close_and_buy_stock_returns_not_implemented(self):
        result = self.agent.CloseAndBuy("2330", price=500, qty=1)
        self.assertFalse(result[-1]["success"])
        self.agent.sdk.futopt.place_order.assert_not_called()

    def test_close_and_sell_stock_returns_not_implemented(self):
        result = self.agent.CloseAndSell("2330", price=500, qty=1)
        self.assertFalse(result[-1]["success"])
        self.agent.sdk.futopt.place_order.assert_not_called()

    def test_mayday_stock_returns_not_implemented(self):
        result = self.agent.MayDay("2330")
        self.assertFalse(result[0]["success"])
        self.agent.sdk.futopt.place_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
