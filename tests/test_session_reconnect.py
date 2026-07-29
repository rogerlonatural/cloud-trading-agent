"""Unit tests for Fubon session-dead detection and auto-reconnect helpers.

Ported patterns from etensword-agent commit
`fix: auto-reconnect Shioaji session on token expiry` (03940ab).
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from fubon_agent.api.fubon_api import (
    SESSION_MAX_AGE_SEC,
    OrderAgent,
    _is_connection_limit_error,
    _is_session_dead_error,
)


class TestIsSessionDeadError(unittest.TestCase):
    def test_disconnect_chinese(self):
        self.assertTrue(_is_session_dead_error(Exception("連線已中斷")))
        self.assertTrue(_is_session_dead_error(Exception("請先登入")))
        self.assertTrue(_is_session_dead_error(Exception("未登入")))

    def test_websocket_closed(self):
        self.assertTrue(_is_session_dead_error(Exception("WebSocket is closed")))
        self.assertTrue(_is_session_dead_error(Exception("connection reset")))

    def test_token_expired_and_401(self):
        self.assertTrue(_is_session_dead_error(Exception("Token is expired")))
        self.assertTrue(_is_session_dead_error(Exception("status_code': 401")))
        self.assertTrue(_is_session_dead_error(Exception('status_code": 401')))

    def test_connection_limit_is_not_session_dead(self):
        """Must not reconnect-spin into Fubon's 10-connection quota."""
        ex = Exception("Login Error, 超過本應用程式連線限制==>[10]")
        self.assertTrue(_is_connection_limit_error(ex))
        self.assertFalse(_is_session_dead_error(ex))

    def test_unrelated_order_error(self):
        self.assertFalse(_is_session_dead_error(Exception("insufficient margin")))
        self.assertFalse(_is_session_dead_error(ValueError("bad price")))


class TestSessionAliveAndEnsureLoggedIn(unittest.TestCase):
    def _make_agent(self):
        with patch.object(OrderAgent, "__init__", return_value=None):
            agent = OrderAgent.__new__(OrderAgent)
        agent.sdk = MagicMock()
        agent.accounts = [MagicMock(account="28")]
        agent.current_account = None
        agent._connected = False
        agent._login_at = 0.0
        agent._reconnect_lock = __import__("threading").Lock()
        agent.agent_account_mapping = {"test-agent": "28"}
        agent.agent_id = None
        agent.trace_id = "test-trace"
        agent.person_id = "K120022333"
        return agent

    def test_is_session_alive_false_when_not_connected(self):
        agent = self._make_agent()
        agent._connected = False
        self.assertFalse(agent.is_session_alive())

    def test_is_session_alive_false_when_stale_age(self):
        agent = self._make_agent()
        agent._connected = True
        agent._login_at = time.time() - SESSION_MAX_AGE_SEC - 10
        self.assertFalse(agent.is_session_alive())

    def test_is_session_alive_false_when_no_accounts(self):
        agent = self._make_agent()
        agent._connected = True
        agent._login_at = time.time()
        agent.accounts = []
        self.assertFalse(agent.is_session_alive())

    def test_is_session_alive_true_when_fresh(self):
        agent = self._make_agent()
        agent._connected = True
        agent._login_at = time.time()
        agent.accounts = [MagicMock()]
        self.assertTrue(agent.is_session_alive())

    def test_ensure_logged_in_reconnects_when_stale(self):
        agent = self._make_agent()
        agent._connected = True
        agent._login_at = time.time() - SESSION_MAX_AGE_SEC - 1
        agent.accounts = [MagicMock()]
        with patch.object(agent, "reconnect") as mock_re:
            agent.ensure_logged_in()
            mock_re.assert_called_once()

    def test_ensure_logged_in_calls_do_login_when_never_connected(self):
        agent = self._make_agent()
        agent._connected = False
        agent.sdk = None
        with patch.object(agent, "_do_login") as mock_login, patch.object(
            agent, "reconnect"
        ) as mock_re:
            agent.ensure_logged_in()
            mock_login.assert_called_once()
            mock_re.assert_not_called()

    def test_has_open_interest_reconnects_on_session_dead(self):
        agent = self._make_agent()
        agent._connected = True
        agent._login_at = time.time()
        agent.current_account = MagicMock()
        agent.agent_id = "test-agent"
        agent._rate_limiter = MagicMock()

        call_count = {"n": 0}

        def query_side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise Exception("連線已中斷")
            return []

        agent.sdk.futopt.query_single_position.side_effect = query_side_effect
        agent.sdk.futopt.get_order_results.return_value = []
        agent.sdk.futopt.convert_symbol.side_effect = lambda p: p

        with patch.object(agent, "reconnect") as mock_re:

            def do_reconnect():
                agent._connected = True
                agent._login_at = time.time()
                agent.current_account = MagicMock()

            mock_re.side_effect = do_reconnect
            resp, trades, positions = agent._has_open_interest("MXFH6")

        mock_re.assert_called_once()
        self.assertTrue(resp["success"])
        self.assertGreaterEqual(call_count["n"], 2)

    def test_retry_place_order_reconnects_on_session_dead(self):
        agent = self._make_agent()
        agent._connected = True
        agent._login_at = time.time()
        agent.current_account = MagicMock()
        order = MagicMock()

        call_count = {"n": 0}

        def place_side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise Exception("WebSocket is closed")
            return MagicMock(order_no="ord-1", status="10")

        agent.sdk.futopt.place_order.side_effect = place_side_effect

        with patch.object(agent, "reconnect") as mock_re:
            mock_re.side_effect = lambda: None
            trade = agent._retry_place_order(order)

        mock_re.assert_called_once()
        self.assertEqual(getattr(trade, "order_no"), "ord-1")
        self.assertEqual(call_count["n"], 2)

    def test_reconnect_restores_account_when_agent_id_known(self):
        agent = self._make_agent()
        agent.agent_id = "test-agent"
        agent.sdk = MagicMock()
        with patch.object(agent, "_do_login") as mock_login, patch.object(
            agent, "_set_account"
        ) as mock_set, patch.object(agent, "_safe_reset_sdk"):
            def after_login():
                agent.accounts = [MagicMock(account="28")]

            mock_login.side_effect = after_login
            agent.reconnect()
            mock_set.assert_called_once_with("test-agent")


if __name__ == "__main__":
    unittest.main()
