import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import fubon_agent.api.fubon_api as fubon_api_module
from fubon_agent.api.base import NeedRetryError, NotEnoughMoneyError
from fubon_agent.api.fubon_api import BSAction, OrderAgent, RateLimiter


def make_agent():
    agent = OrderAgent()
    agent.sdk = MagicMock()
    agent.current_account = MagicMock()
    agent.accounts = [MagicMock(account="0000001")]
    agent._connected = True
    agent._login_at = time.time()
    agent.trace_id = "test-trace"
    agent.agent_id = "test-agent"
    agent.is_dry_run = False
    agent.sdk.futopt.convert_symbol.side_effect = lambda product: product
    return agent


class TestOrderBatching(unittest.TestCase):
    def test_mayday_splits_large_qty_into_batches_of_20(self):
        agent = make_agent()
        position = MagicMock(symbol="TXF", buy_sell=BSAction.Buy, lot=45)
        agent._has_open_interest = MagicMock(
            return_value=(dict(api="list_positions", success=True, result="[]"), [], [position])
        )
        agent._check_order_info = MagicMock(return_value=dict(api="check_order_info", success=True, result="{}"))
        agent.sdk.futopt.place_order.return_value = MagicMock(order_no="ord-1", status="10")

        results = agent._mayday_futures("TXF")

        self.assertEqual(agent.sdk.futopt.place_order.call_count, 3)
        qtys = [call.args[1].lot for call in agent.sdk.futopt.place_order.call_args_list]
        self.assertEqual(qtys, [20, 20, 5])
        placed = [r for r in results if r.get("api") == "place_order"]
        self.assertTrue(all(r["success"] for r in placed))

    def test_place_order_converts_symbol_before_calling_sdk(self):
        agent = make_agent()
        agent.sdk.futopt.convert_symbol.side_effect = None
        agent.sdk.futopt.convert_symbol.return_value = "TXFD4"
        agent.sdk.futopt.place_order.return_value = MagicMock(order_no="ord-1", status="10")

        response = agent._place_order("TXF", "B", price=0, qty=1)

        agent.sdk.futopt.convert_symbol.assert_called_once_with("TXF")
        self.assertTrue(response["success"])
        order_arg = agent.sdk.futopt.place_order.call_args.args[1]
        self.assertEqual(order_arg.symbol, "TXFD4")

    def test_place_order_marks_failure_on_failed_status(self):
        agent = make_agent()
        agent.sdk.futopt.place_order.return_value = MagicMock(order_no="ord-1", status="90")

        response = agent._place_order("TXF", "B", price=0, qty=1)

        self.assertFalse(response["success"])

    def test_dry_run_short_circuits_without_calling_sdk_place_order(self):
        agent = make_agent()
        agent.is_dry_run = True

        response = agent._place_order("TXF", "B", price=0, qty=1)

        self.assertTrue(response["success"])
        agent.sdk.futopt.place_order.assert_not_called()

    def test_has_open_interest_also_checks_txf_for_mxf(self):
        agent = make_agent()
        agent._has_open_interest = MagicMock(
            side_effect=[
                (dict(api="list_positions", success=True, result="[]"), [], []),
                (dict(api="list_positions", success=True, result="[]"), [], []),
            ]
        )

        agent.HasOpenInterest("MXF202404")

        self.assertEqual(agent._has_open_interest.call_count, 2)
        self.assertEqual(agent._has_open_interest.call_args_list[1].args[0], "TXF202404")


class TestBuyMaxQty(unittest.TestCase):
    """Pure BUY props.max_qty (etensword-agent a5009bd + d45a159)."""

    def test_buy_without_max_qty_places_full_qty(self):
        agent = make_agent()
        agent._has_open_interest = MagicMock(
            return_value=(dict(api="list_positions", success=True, result="[]"), [], [])
        )
        agent._check_order_info = MagicMock(
            return_value=dict(api="check_order_info", success=True, result="{}")
        )
        agent.sdk.futopt.place_order.return_value = MagicMock(
            order_no="ord-1", status="10"
        )

        agent._buy_futures("TXF", price=100, qty=3, max_qty=0)

        self.assertEqual(agent.sdk.futopt.place_order.call_count, 1)
        self.assertEqual(agent.sdk.futopt.place_order.call_args.args[1].lot, 3)

    def test_buy_max_qty_caps_open_size_given_existing_long(self):
        # exist_buy=3, qty=3, max_qty=5 => place 2
        agent = make_agent()
        existing = MagicMock(symbol="TXF", buy_sell=BSAction.Buy, lot=3)
        agent._has_open_interest = MagicMock(
            return_value=(
                dict(api="list_positions", success=True, result="[]"),
                [],
                [existing],
            )
        )
        agent._check_order_info = MagicMock(
            return_value=dict(api="check_order_info", success=True, result="{}")
        )
        agent.sdk.futopt.place_order.return_value = MagicMock(
            order_no="ord-1", status="10"
        )

        agent._buy_futures("TXF", price=100, qty=3, max_qty=5)

        self.assertEqual(agent.sdk.futopt.place_order.call_count, 1)
        self.assertEqual(agent.sdk.futopt.place_order.call_args.args[1].lot, 2)

    def test_buy_max_qty_noop_when_already_at_cap(self):
        agent = make_agent()
        existing = MagicMock(symbol="TXF", buy_sell=BSAction.Buy, lot=5)
        agent._has_open_interest = MagicMock(
            return_value=(
                dict(api="list_positions", success=True, result="[]"),
                [],
                [existing],
            )
        )
        agent.sdk.futopt.place_order.return_value = MagicMock(
            order_no="ord-1", status="10"
        )

        results = agent._buy_futures("TXF", price=100, qty=3, max_qty=5)

        agent.sdk.futopt.place_order.assert_not_called()
        noop = [r for r in results if r.get("api") == "Buy"]
        self.assertEqual(len(noop), 1)
        self.assertTrue(noop[0]["success"])
        self.assertIn("No-op", noop[0]["result"])

    def test_buy_max_qty_noop_leaves_existing_working_orders_untouched(self):
        """d45a159: at max_qty must not cancel working orders."""
        agent = make_agent()
        existing = MagicMock(symbol="TXF", buy_sell=BSAction.Buy, lot=5)
        working_trade = MagicMock(
            symbol="TXF", buy_sell=BSAction.Buy, lot=1, order_no="open-1", status="10"
        )
        agent._has_open_interest = MagicMock(
            return_value=(
                dict(api="list_positions", success=True, result="[]"),
                [working_trade],
                [existing],
            )
        )
        agent._cancel_ongoing_trades = MagicMock()
        agent.sdk.futopt.place_order.return_value = MagicMock(
            order_no="ord-1", status="10"
        )

        results = agent._buy_futures("TXF", price=100, qty=2, max_qty=5)

        agent._cancel_ongoing_trades.assert_not_called()
        agent.sdk.futopt.place_order.assert_not_called()
        agent.sdk.futopt.cancel_order.assert_not_called()
        noop = [r for r in results if r.get("api") == "Buy"]
        self.assertTrue(noop[0]["success"])
        self.assertIn("No-op", noop[0]["result"])

    def test_buy_max_qty_noop_after_refresh_if_long_reaches_cap(self):
        """After cancel/refresh, re-check max_qty before close/open."""
        agent = make_agent()
        # First snapshot: long=4, working trade present → cancel allowed
        # Refresh: long already 5 → no-op, no open
        long_under = MagicMock(symbol="TXF", buy_sell=BSAction.Buy, lot=4)
        long_at_cap = MagicMock(symbol="TXF", buy_sell=BSAction.Buy, lot=5)
        working = MagicMock(symbol="TXF", order_no="w1", status="10")
        agent._has_open_interest = MagicMock(
            side_effect=[
                (
                    dict(api="list_positions", success=True, result="[]"),
                    [working],
                    [long_under],
                ),
                (
                    dict(api="list_positions", success=True, result="[]"),
                    [],
                    [long_at_cap],
                ),
            ]
        )
        agent._cancel_ongoing_trades = MagicMock()
        agent.sdk.futopt.place_order.return_value = MagicMock(
            order_no="ord-1", status="10"
        )

        results = agent._buy_futures("TXF", price=100, qty=3, max_qty=5)

        agent._cancel_ongoing_trades.assert_called_once()
        agent.sdk.futopt.place_order.assert_not_called()
        noop = [r for r in results if r.get("api") == "Buy"]
        self.assertEqual(len(noop), 1)
        self.assertIn("No-op", noop[0]["result"])

    def test_buy_max_qty_still_closes_shorts_before_cap(self):
        agent = make_agent()
        short = MagicMock(symbol="TXF", buy_sell=BSAction.Sell, lot=2)
        agent._has_open_interest = MagicMock(
            return_value=(
                dict(api="list_positions", success=True, result="[]"),
                [],
                [short],
            )
        )
        agent._check_order_info = MagicMock(
            return_value=dict(api="check_order_info", success=True, result="{}")
        )
        agent.sdk.futopt.place_order.return_value = MagicMock(
            order_no="ord-1", status="10"
        )

        # no existing long, max_qty=5, qty=4 => close 2 + open 4
        agent._buy_futures("TXF", price=100, qty=4, max_qty=5)

        lots = [c.args[1].lot for c in agent.sdk.futopt.place_order.call_args_list]
        self.assertEqual(lots, [2, 4])

    def test_buy_dispatch_passes_max_qty(self):
        agent = make_agent()
        with patch.object(agent, "_buy_futures", return_value=[]) as mock_buy:
            agent.Buy("TXF", price=100, qty=2, max_qty=7)
            mock_buy.assert_called_once_with("TXF", 100, 2, max_qty=7)


class TestPriceChasingRetry(unittest.TestCase):
    def test_close_and_buy_chases_price_up_on_retry_then_succeeds(self):
        agent = make_agent()
        call_prices = []
        # No market ref → pure step chase (PRICE_RETRY_STEP=50)
        agent._get_market_ref_price = MagicMock(return_value=None)

        def fake_close_and_buy(product, price, qty=1):
            call_prices.append(price)
            if len(call_prices) < 3:
                raise NeedRetryError("not filled yet")
            return [dict(api="place_order", success=True, result="ok")]

        agent._close_and_buy_futures = MagicMock(side_effect=fake_close_and_buy)
        result = agent.CloseAndBuy("TXF", price=100, qty=1)

        self.assertEqual(call_prices, [100, 150, 200])
        self.assertTrue(result[-1]["success"])

    def test_close_and_buy_gives_up_after_bounded_retries_price_never_exceeds_cap(self):
        agent = make_agent()
        call_prices = []
        agent._get_market_ref_price = MagicMock(return_value=None)

        def fake_close_and_buy(product, price, qty=1):
            call_prices.append(price)
            raise NeedRetryError("still not filled")

        agent._close_and_buy_futures = MagicMock(side_effect=fake_close_and_buy)
        result = agent.CloseAndBuy("TXF", price=100, qty=1)

        self.assertFalse(result[-1]["success"])
        soft_cap = 100 + fubon_api_module.PRICE_RETRY_LIMIT_OFFSET
        self.assertTrue(all(p <= soft_cap for p in call_prices))
        # initial + CLOSE_OPEN_MAX_OUTER_RETRY retries
        self.assertEqual(len(call_prices), fubon_api_module.CLOSE_OPEN_MAX_OUTER_RETRY + 1)

    def test_close_and_sell_chases_price_down_on_retry(self):
        agent = make_agent()
        call_prices = []
        agent._get_market_ref_price = MagicMock(return_value=None)

        def fake_close_and_sell(product, price, qty=1):
            call_prices.append(price)
            if len(call_prices) < 2:
                raise NeedRetryError("not filled yet")
            return [dict(api="place_order", success=True, result="ok")]

        agent._close_and_sell_futures = MagicMock(side_effect=fake_close_and_sell)
        result = agent.CloseAndSell("TXF", price=100, qty=1)

        self.assertEqual(call_prices, [100, 50])
        self.assertTrue(result[-1]["success"])

    def test_close_and_buy_chases_above_soft_cap_when_market_runs(self):
        agent = make_agent()
        call_prices = []
        # Market far above strategy → allow above soft_cap to fill
        agent._get_market_ref_price = MagicMock(return_value=400)

        def fake_close_and_buy(product, price, qty=1):
            call_prices.append(price)
            if len(call_prices) < 2:
                raise NeedRetryError("not filled yet")
            return [dict(api="place_order", success=True, result="ok")]

        agent._close_and_buy_futures = MagicMock(side_effect=fake_close_and_buy)
        result = agent.CloseAndBuy("TXF", price=100, qty=1)

        soft_cap = 100 + fubon_api_module.PRICE_RETRY_LIMIT_OFFSET
        self.assertEqual(call_prices[0], 100)
        self.assertGreater(call_prices[1], soft_cap)
        self.assertTrue(result[-1]["success"])

    def test_close_and_buy_qty_decrements_on_not_enough_money(self):
        agent = make_agent()
        qtys_seen = []

        def fake_close_and_buy(product, price, qty=1):
            qtys_seen.append(qty)
            if qty > 1:
                raise NotEnoughMoneyError("insufficient margin")
            return [dict(api="place_order", success=True, result="ok")]

        agent._close_and_buy_futures = MagicMock(side_effect=fake_close_and_buy)
        result = agent.CloseAndBuy("TXF", price=100, qty=3)

        self.assertEqual(qtys_seen, [3, 2, 1])
        self.assertTrue(result[-1]["success"])


class TestLoginMethod(unittest.TestCase):
    def test_sdk_authenticate_uses_apikey_by_default(self):
        agent = make_agent()
        agent.login_method = "apikey"
        agent.api_key = "SECRET_KEY"
        agent.person_id = "A123456789"
        agent.cert_path = "/tmp/x.pfx"
        agent.cert_password = "cpw"
        sdk = MagicMock()
        sdk.apikey_login.return_value = MagicMock(is_success=True, data=[MagicMock()])

        agent._sdk_authenticate(sdk)

        sdk.apikey_login.assert_called_once_with(
            "A123456789", "SECRET_KEY", "/tmp/x.pfx", "cpw"
        )
        sdk.login.assert_not_called()

    def test_sdk_authenticate_password_when_configured(self):
        agent = make_agent()
        agent.login_method = "password"
        agent.password = "pw"
        agent.person_id = "A123456789"
        agent.cert_path = "/tmp/x.pfx"
        agent.cert_password = "cpw"
        sdk = MagicMock()
        sdk.login.return_value = MagicMock(is_success=True, data=[MagicMock()])

        agent._sdk_authenticate(sdk)

        sdk.login.assert_called_once_with("A123456789", "pw", "/tmp/x.pfx", "cpw")
        sdk.apikey_login.assert_not_called()

    def test_sdk_authenticate_apikey_requires_key(self):
        agent = make_agent()
        agent.login_method = "apikey"
        agent.api_key = ""
        with self.assertRaises(RuntimeError) as ctx:
            agent._sdk_authenticate(MagicMock())
        self.assertIn("api_key", str(ctx.exception))

    def test_do_login_wires_apikey_and_accounts(self):
        agent = make_agent()
        agent.login_method = "apikey"
        agent.api_key = "SECRET_KEY"
        agent.person_id = "A123456789"
        agent.cert_path = "/tmp/x.pfx"
        agent.cert_password = "cpw"
        agent._connected = False
        agent._login_at = 0.0
        agent.accounts = []
        agent.sdk = None

        account = MagicMock(account="28")
        mock_sdk = MagicMock()
        mock_sdk.apikey_login.return_value = MagicMock(
            is_success=True, message=None, data=[account]
        )

        with patch.object(fubon_api_module, "FubonSDK", return_value=mock_sdk):
            agent._do_login()

        mock_sdk.apikey_login.assert_called_once()
        mock_sdk.login.assert_not_called()
        self.assertTrue(agent._connected)
        self.assertEqual(agent.accounts, [account])
        self.assertGreater(agent._login_at, 0)
        mock_sdk.set_on_event.assert_called()


class TestReconnect(unittest.TestCase):
    def test_on_event_disconnect_code_flips_connected_flag(self):
        agent = make_agent()
        agent._connected = True
        agent._on_event(None, {"code": "300"})
        self.assertFalse(agent._connected)

    def test_on_event_accepts_code_as_first_arg(self):
        """Fubon reconnect sample uses on_event(code, content)."""
        agent = make_agent()
        agent._connected = True
        agent._on_event("301", "pong timeout")
        self.assertFalse(agent._connected)

    def test_on_event_api_key_revoked_marks_dead(self):
        agent = make_agent()
        agent._connected = True
        agent._on_event("304", "api key revoked")
        self.assertFalse(agent._connected)

    def test_on_event_unrelated_code_keeps_connected_flag(self):
        agent = make_agent()
        agent._connected = True
        agent._on_event(None, {"code": "100"})
        self.assertTrue(agent._connected)

    @patch.object(OrderAgent, "_do_login")
    def test_reconnect_logs_out_and_relogins(self, mock_do_login):
        agent = make_agent()
        sdk = agent.sdk
        agent.reconnect()
        sdk.logout.assert_called_once()
        self.assertIsNone(agent.sdk)  # cleared by _safe_reset_sdk before re-login
        mock_do_login.assert_called_once()

    @patch.object(OrderAgent, "_do_login")
    def test_concurrent_reconnect_second_caller_waits_without_double_login(
        self, mock_do_login
    ):
        agent = make_agent()
        started = threading.Event()
        release_first = threading.Event()

        def slow_login():
            started.set()
            release_first.wait(timeout=2)

        mock_do_login.side_effect = slow_login

        t1 = threading.Thread(target=agent.reconnect)
        t1.start()
        self.assertTrue(started.wait(timeout=1))
        t2 = threading.Thread(target=agent.reconnect)
        t2.start()
        # Give t2 time to block on the reconnect lock
        time.sleep(0.05)
        release_first.set()
        t1.join(timeout=2)
        t2.join(timeout=2)
        self.assertEqual(mock_do_login.call_count, 1)

    def test_get_or_create_agent_uses_ensure_logged_in(self):
        fubon_api_module._singleton_agent = None
        try:
            with patch.object(OrderAgent, "ensure_logged_in") as mock_ensure, patch.object(
                OrderAgent, "__init__", return_value=None
            ):
                # Manually craft a half-init singleton like production path
                agent = OrderAgent.__new__(OrderAgent)
                agent._connected = False
                agent.sdk = None
                fubon_api_module._singleton_agent = agent
                out = fubon_api_module.get_or_create_agent()
                self.assertIs(out, agent)
                mock_ensure.assert_called_once()
        finally:
            fubon_api_module._singleton_agent = None


class TestRateLimiter(unittest.TestCase):
    def test_limiter_throttles_calls_beyond_the_configured_rate(self):
        limiter = RateLimiter(max_calls=5, period=0.2)
        start = time.monotonic()
        for _ in range(6):
            limiter.acquire()
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.2)


if __name__ == "__main__":
    unittest.main()
