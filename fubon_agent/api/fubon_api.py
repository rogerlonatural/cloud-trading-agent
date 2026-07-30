import collections
import json
import threading
import time
import traceback

# Verify these import paths against the actual installed fubon_neo wheel before
# relying on this module in production (package is not on PyPI, see plan doc).
from fubon_neo.sdk import FubonSDK, FutOptOrder
from fubon_neo.constant import (
    BSAction,
    FutOptMarketType,
    FutOptOrderType,
    FutOptPriceType,
    TimeInForce,
)

from fubon_agent.api.base import (
    OrderAgentBase,
    NeedRetryError,
    NotEnoughMoneyError,
    logger,
    feedback_execution_result,
    AgentCommand,
)

ORDER_BATCH_QTY = 20
ORDER_TYPE_BUY = "B"
ORDER_TYPE_SELL = "S"

# Order verification / chase-limit pricing (CloseAndBuy / CloseAndSell)
ORDER_CHECK_MAX_RETRY = 6  # was 3; longer wait for fill before bumping price
ORDER_CHECK_SLEEP_CAP_SEC = 15
CLOSE_OPEN_MAX_OUTER_RETRY = 5  # was 2 (3 attempts total)
PRICE_RETRY_STEP = 50  # was 25; larger step when chasing market
PRICE_RETRY_LIMIT_OFFSET = 200  # was 90 from initial strategy price
PRICE_CHASE_BUFFER = 30  # place at least market ± this many points

# Backward-compatible aliases (older tests/docs may still reference these)
PRICE_CHASE_STEP = PRICE_RETRY_STEP
PRICE_CHASE_CAP = PRICE_RETRY_LIMIT_OFFSET

# Login: avoid death spiral on Fubon connection limit (max 10 per app)
LOGIN_MAX_RETRY = 2
LOGIN_CONN_LIMIT_SLEEP_SEC = 60

# Long-lived Cloud Run singleton: proactively re-login before session rot.
# (Heuristic port of etensword-agent SESSION_MAX_AGE_SEC; Fubon does not
# document a fixed JWT TTL the same way Shioaji does.)
SESSION_MAX_AGE_SEC = 23 * 3600
# Within one API call, only reconnect once on session-dead errors.
SESSION_RECONNECT_ONCE = 1

# Fubon order-status codes confirmed via docs (error-codes.txt): only these two
# are known meanings; everything else is treated as "still open/pending".
ORDER_STATUS_CANCELLED = "30"
ORDER_STATUS_FILLED = "50"
ORDER_STATUS_FAILED = "90"

# Event codes from docs/trading/guide/error-codes.txt + advance/reconnect.txt.
# 300 disconnect, 301 pong timeout, 302 user logout disconnect, 304 API key revoked.
DISCONNECT_EVENT_CODES = ("300", "301", "302", "304", 300, 301, 302, 304)

# Strategy product root -> Fubon accounting symbol (HybridPosition.symbol).
# Order symbols (e.g. TXFD4) come from futopt.convert_symbol(acct_sym, expiry).
ACCOUNTING_SYMBOL_BY_ROOT = {
    "TXF": "FITX",
    "MXF": "FIMTX",
    "TMF": "FITM",  # confirmed live: HybridPosition.symbol=FITM -> TMFH6
}

_singleton_agent = None


class TooManyConnectionsError(RuntimeError):
    """Raised when Fubon login is rejected for connection quota."""

    pass


def _is_connection_limit_error(ex: BaseException) -> bool:
    """Detect Fubon login/connection quota errors (docs/trade-rate-limit.txt)."""
    text = str(ex)
    return (
        "超過本應用程式連線限制" in text
        or "業務系統流量控管" in text
        or "Too Many Connections" in text
    )


def _is_session_dead_error(ex: BaseException) -> bool:
    """
    Detect errors that mean the singleton Fubon session is unusable and needs
    reconnect (logout + login), not mere API retries.

    Ported from etensword-agent's Shioaji session-dead detector; markers are
    Fubon/WS oriented (docs: reconnect event 300/301/304 + common disconnect text).
    Connection-quota errors are excluded so we never reconnect-spin into the
    10-connection limit.
    """
    if _is_connection_limit_error(ex):
        return False
    text = str(ex)
    low = text.lower()
    markers = (
        "token is expired",
        "not ready",
        "not connected",
        "connection closed",
        "connection reset",
        "websocket is closed",
        "websocket closed",
        "not login",
        "not logged in",
        "未登入",
        "請先登入",
        "連線已中斷",
        "斷線",
        "強制登出",
        "unauthorized",
        "status_code': 401",
        'status_code": 401',
        "status_code: 401",
    )
    for marker in markers:
        if marker in text or marker in low:
            return True
    return False


def get_or_create_agent() -> "OrderAgent":
    """Return the process-wide Fubon OrderAgent singleton (login if needed).

    This is the only agent entry point — this repo is Fubon-only (no factory /
    order_agent_type plug-in).
    """
    global _singleton_agent
    if _singleton_agent is None:
        _singleton_agent = OrderAgent()
    _singleton_agent.ensure_logged_in()
    return _singleton_agent


# Alias used by callers that historically expected get_order_agent().
get_order_agent = get_or_create_agent


def strbool(val) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).lower() in ["true", "yes", "y", "1"]


def _iter_batches(qty, batch_size=ORDER_BATCH_QTY):
    remaining = qty
    while remaining > 0:
        batch = min(remaining, batch_size)
        yield batch
        remaining -= batch


class RateLimiter:
    """Simple sliding-window limiter shared across accounting/query calls.

    Fubon caps accounting queries at 5 req/s (docs/trade-rate-limit.txt) -- this
    is a real behavioral difference from Shioaji (unlimited in the reference repo),
    so every accounting-style call must go through this instead of firing freely.
    """

    def __init__(self, max_calls, period=1.0):
        self.max_calls = max_calls
        self.period = period
        self._calls = collections.deque()
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] > self.period:
                self._calls.popleft()
            if len(self._calls) >= self.max_calls:
                sleep_time = self.period - (now - self._calls[0])
                if sleep_time > 0:
                    time.sleep(sleep_time)
                now = time.monotonic()
                while self._calls and now - self._calls[0] > self.period:
                    self._calls.popleft()
            self._calls.append(time.monotonic())


# Runtime login methods. API Key is the production default (SDK >= v2.2.7);
# password+cert is retained only for first-time 連線測試 / account activation.
LOGIN_METHOD_APIKEY = "apikey"
LOGIN_METHOD_PASSWORD = "password"


def _resolve_api_key(fubon_cfg: dict) -> str:
    """Load API key from inline config, api_key_path, or empty string."""
    raw = fubon_cfg.get("api_key")
    if raw:
        return str(raw).strip()
    key_path = fubon_cfg.get("api_key_path")
    if key_path:
        import os
        from pathlib import Path

        path = Path(str(key_path)).expanduser()
        if not path.is_absolute():
            # relative to process cwd (Cloud Run / local); callers may use abs path
            path = Path(os.getcwd()) / path
        if path.is_file():
            return "".join(path.read_text(encoding="utf-8").split())
    return ""


class OrderAgent(OrderAgentBase):
    def __init__(self):
        super().__init__()

        fubon_cfg = self.config["fubon_api"]
        self.person_id = fubon_cfg["person_id"]
        self.password = fubon_cfg.get("password") or ""
        self.cert_path = fubon_cfg["cert_path"]
        self.cert_password = fubon_cfg["cert_password"]
        self.api_key = _resolve_api_key(fubon_cfg)
        self.login_method = str(
            fubon_cfg.get("login_method") or LOGIN_METHOD_APIKEY
        ).strip().lower()
        if self.login_method not in (LOGIN_METHOD_APIKEY, LOGIN_METHOD_PASSWORD):
            raise ValueError(
                f"fubon_api.login_method must be '{LOGIN_METHOD_APIKEY}' or "
                f"'{LOGIN_METHOD_PASSWORD}', got {self.login_method!r}"
            )
        self.is_dry_run = strbool(fubon_cfg.get("is_dry_run", False))
        logger.info("Set is_dry_run = %s" % self.is_dry_run)
        logger.info(
            f"Login method = {self.login_method} "
            f"(api_key={'set' if self.api_key else 'missing'})"
        )

        self.agent_account_mapping = dict(self.config.get("agent_account_mapping") or {})

        self.sdk = None
        self.accounts = []
        self.current_account = None
        self._connected = False
        self._login_at = 0.0  # time.time() of last successful login
        self._reconnect_lock = threading.Lock()
        self._rate_limiter = RateLimiter(max_calls=5, period=1.0)
        self.trace_id = ""

    # -- session lifecycle -------------------------------------------------

    def is_session_alive(self) -> bool:
        """
        Best-effort liveness check.

        Mirrors etensword-agent: flag alone is insufficient after WS death or
        long idle; also treat sessions older than SESSION_MAX_AGE_SEC as stale
        so Cloud Run min-instances=1 singletons re-auth proactively.
        """
        if not self._connected or not self.sdk:
            return False
        if self._login_at and (time.time() - self._login_at) >= SESSION_MAX_AGE_SEC:
            logger.info(
                f"[{self.trace_id}] Session age "
                f"{time.time() - self._login_at:.0f}s >= {SESSION_MAX_AGE_SEC}s; "
                f"treat as stale"
            )
            return False
        if not self.accounts:
            return False
        return True

    def ensure_logged_in(self):
        """Login if needed; reconnect when session is dead or past max age."""
        if self._connected and self.is_session_alive():
            return
        if self._connected or self.sdk is not None:
            logger.warning(
                f"[{self.trace_id}] Session not alive (expired/stale/disconnected); "
                f"reconnecting"
            )
            self.reconnect()
            return
        self._do_login()

    def _safe_reset_sdk(self):
        """Logout and clear SDK so the next login starts from a clean client."""
        try:
            if self.sdk:
                self.sdk.logout()
        except Exception as e:
            logger.warning(f"[{self.trace_id}] _safe_reset_sdk logout ignored: {e}")
        self.sdk = None
        self.accounts = []
        self.current_account = None
        self._connected = False
        self._login_at = 0.0

    def _sdk_authenticate(self, sdk):
        """Authenticate via API Key (default) or password+cert (activation only)."""
        if self.login_method == LOGIN_METHOD_PASSWORD:
            logger.info(
                f"[{self.trace_id}] Authenticating with password+cert "
                f"(login_method=password)"
            )
            return sdk.login(
                self.person_id, self.password, self.cert_path, self.cert_password
            )

        # Default / production path: apikey_login (SDK >= v2.2.7)
        if not self.api_key:
            raise RuntimeError(
                "login_method=apikey requires fubon_api.api_key (or api_key_path). "
                "Password login is only for first-time 連線測試; set login_method=password "
                "explicitly if you still need it."
            )
        if not hasattr(sdk, "apikey_login"):
            raise RuntimeError(
                "Installed fubon_neo does not expose apikey_login (need SDK >= v2.2.7)"
            )
        logger.info(
            f"[{self.trace_id}] Authenticating with API Key "
            f"(login_method=apikey, person_id={self.person_id[:3]}***)"
        )
        return sdk.apikey_login(
            self.person_id, self.api_key, self.cert_path, self.cert_password
        )

    def _do_login(self, no_retry=False):
        retry = 0
        logger.info(
            f"[{self.trace_id}] Start login (method={self.login_method}) ..."
        )
        while True:
            try:
                self.sdk = FubonSDK()
                result = self._sdk_authenticate(self.sdk)
                # Honor is_success when present (loginPassword / apikey docs).
                is_success = getattr(result, "is_success", None)
                if is_success is False:
                    raise RuntimeError(
                        f"Login rejected: is_success=False, "
                        f"message={getattr(result, 'message', None)!r}"
                    )
                self.accounts = list(getattr(result, "data", None) or [])
                if not self.accounts:
                    raise RuntimeError(
                        f"Login returned no accounts, message="
                        f"{getattr(result, 'message', None)!r}"
                    )
                self._register_callbacks()
                self._connected = True
                self._login_at = time.time()
                logger.info(
                    f"[{self.trace_id}] Login succeed! method={self.login_method}, "
                    f"accounts={len(self.accounts)}"
                )
                return
            except Exception as ex:
                too_many = _is_connection_limit_error(ex)
                if too_many:
                    logger.error(
                        f"[{self.trace_id}] Login rejected: connection limit "
                        f"(person_id={self.person_id[:3]}***). "
                        f"Avoid aggressive retry to prevent connection death spiral. "
                        f"ex={ex}"
                    )

                if no_retry or retry >= LOGIN_MAX_RETRY:
                    logger.error(
                        f"[{self.trace_id}] Login failed after retry. {ex}, "
                        f"{traceback.format_exc()}"
                    )
                    if too_many:
                        raise TooManyConnectionsError(str(ex)) from ex
                    raise

                # Reset client before retry — do not reuse a failed login session
                try:
                    self._safe_reset_sdk()
                except Exception:
                    pass

                retry += 1
                if too_many:
                    sleep_sec = LOGIN_CONN_LIMIT_SLEEP_SEC * retry
                else:
                    sleep_sec = retry * 30
                logger.warning(
                    f"[{self.trace_id}] Login failed and start retry {retry} "
                    f"after {sleep_sec}s, too_many={too_many}, {ex}"
                )
                time.sleep(sleep_sec)

    def reconnect(self):
        """Logout, rebuild SDK, login again; restore account if agent_id known.

        If another thread is already reconnecting, wait for it to finish
        (rather than skipping and leaving the caller on a dead session).
        """
        acquired = self._reconnect_lock.acquire(blocking=False)
        if not acquired:
            logger.info(
                f"[{self.trace_id}] Reconnect already in progress; waiting for it"
            )
            with self._reconnect_lock:
                return
        try:
            logger.warning(f"[{self.trace_id}] reconnect: resetting Fubon session")
            prev_agent_id = getattr(self, "agent_id", None)
            self._safe_reset_sdk()
            self._do_login()
            if prev_agent_id and prev_agent_id in self.agent_account_mapping:
                try:
                    self._set_account(prev_agent_id)
                except Exception as e:
                    logger.warning(
                        f"[{self.trace_id}] reconnect restored login but "
                        f"_set_account failed (will retry on InitAgent): {e}"
                    )
        finally:
            self._reconnect_lock.release()

    def shutdown(self):
        try:
            if self.sdk:
                self.sdk.logout()
                logger.info(f"[{self.trace_id}] Logout and leave")
        except Exception as e:
            logger.warning(f"[{self.trace_id}] Error on shutdown, ignored {e}")
        self.sdk = None
        self.accounts = []
        self.current_account = None
        self._connected = False
        self._login_at = 0.0

    def _register_callbacks(self):
        """Re-bind trade callbacks after every login (Fubon reconnect docs)."""
        self.sdk.set_on_order(self._on_order)
        self.sdk.set_on_order_changed(self._on_order_changed)
        self.sdk.set_on_filled(self._on_filled)
        self.sdk.set_on_event(self._on_event)

    # Callback payload shape: official reconnect sample uses on_event(code, content);
    # some call paths may pass a dict body. Accept both.
    def _on_order(self, err, content):
        logger.info(f"[{self.trace_id}] on_order > err: {err}, content: {content}")

    def _on_order_changed(self, err, content):
        logger.info(f"[{self.trace_id}] on_order_changed > err: {err}, content: {content}")

    def _on_filled(self, err, content):
        try:
            logger.info(f"[{self.trace_id}] on_filled > err: {err}, content: {content}")
            feedback_execution_result(
                self.agent_id, AgentCommand.DEAL_CALLBACK, self.trace_id, content
            )
        except Exception as e:
            logger.warning(f"[{self.trace_id}] on_filled error but ignore: {e}")

    def _extract_event_code(self, code_or_err, content):
        """Normalize Fubon on_event(code, content) vs dict-shaped payloads."""
        if isinstance(code_or_err, (str, int)) and not isinstance(code_or_err, bool):
            return code_or_err
        if isinstance(content, dict):
            return content.get("code")
        return getattr(content, "code", None)

    def _on_event(self, code_or_err, content=None):
        try:
            code = self._extract_event_code(code_or_err, content)
            logger.info(
                f"[{self.trace_id}] on_event > code: {code}, "
                f"content: {content}, raw0: {code_or_err}"
            )
            if code in DISCONNECT_EVENT_CODES:
                # Mark dead; actual reconnect runs on ensure_logged_in / API
                # session-dead recovery so we do not race the order lock from
                # the SDK callback thread (etensword-agent pattern).
                logger.warning(
                    f"[{self.trace_id}] disconnect event {code}; "
                    f"mark session dead for next ensure_logged_in/reconnect"
                )
                self._connected = False
        except Exception as e:
            logger.warning(f"[{self.trace_id}] on_event handler error but ignore: {e}")

    def _set_account(self, agent_id):
        self.account_id = self.agent_account_mapping[agent_id]
        logger.info(
            f"[{self.trace_id}] Set account {self.account_id} for agent: {self.agent_id}"
        )
        for account in self.accounts:
            account_number = getattr(account, "account", getattr(account, "account_id", None))
            if account_number == self.account_id:
                self.current_account = account
                break
        if not self.current_account:
            raise Exception("No account for account_id: %s" % self.account_id)
        logger.info(f"[{self.trace_id}] Set default account: {self.current_account}")

    def InitAgent(self, agent_id):
        logger.info(f"[{self.trace_id}] InitAgent > agent: {agent_id}")
        self.agent_id = agent_id
        self.ensure_logged_in()
        self._set_account(agent_id)

    def CloseAgent(self):
        logger.info(f"[{self.trace_id}] CloseAgent > no-op (singleton session)")

    # -- symbol / margin helpers --------------------------------------------

    def _unwrap_sdk_list(self, result):
        """Normalize Fubon Result{is_success,data} or bare list/None to a list."""
        if result is None:
            return []
        is_success = getattr(result, "is_success", None)
        if is_success is False:
            raise RuntimeError(
                f"SDK call failed: message={getattr(result, 'message', None)!r}"
            )
        data = getattr(result, "data", result)
        if data is None:
            return []
        if isinstance(data, (list, tuple)):
            return list(data)
        return [data]

    def _product_root(self, product: str) -> str:
        p = str(product or "").upper()
        for root in ACCOUNTING_SYMBOL_BY_ROOT:
            if p.startswith(root):
                return root
        return p

    def _accounting_symbol_for_product(self, product: str) -> str:
        root = self._product_root(product)
        return ACCOUNTING_SYMBOL_BY_ROOT.get(root, root)

    def _convert_symbol(self, product, expiry_date=None, strike_price=None, call_put=None):
        """Map accounting symbol(+expiry) to order symbol, or pass-through.

        Official API: convert_symbol(symbol, expiry_date, strike_price=None, call_put=None)
        e.g. convert_symbol("FITX", "202404") -> "TXFD4"
        Strategy codes like "TXF" alone cannot convert without expiry; return as-is.
        """
        try:
            if expiry_date is not None:
                return self.sdk.futopt.convert_symbol(
                    product, str(expiry_date), strike_price, call_put
                )
            # Already looks like an order contract (e.g. TXFD4) — keep as-is.
            p = str(product)
            if len(p) > 3 and p[:3].upper() in ACCOUNTING_SYMBOL_BY_ROOT:
                # TXF / MXF / TMF roots with month suffix
                if p.upper() not in ACCOUNTING_SYMBOL_BY_ROOT:
                    return p
            return p
        except Exception as e:
            logger.warning(
                f"[{self.trace_id}] convert_symbol failed for {product}, "
                f"expiry={expiry_date}, fallback to raw code: {e}"
            )
            return product

    def _position_order_symbol(self, position):
        """Best-effort order symbol for a HybridPosition/Position row."""
        sym = getattr(position, "symbol", None)
        expiry = getattr(position, "expiry_date", None)
        if sym and expiry not in (None, ""):
            try:
                return self.sdk.futopt.convert_symbol(
                    str(sym),
                    str(expiry),
                    getattr(position, "strike_price", None),
                    getattr(position, "call_put", None),
                )
            except Exception as e:
                logger.warning(
                    f"[{self.trace_id}] position convert_symbol failed "
                    f"sym={sym} expiry={expiry}: {e}"
                )
        return sym

    def _position_qty(self, position) -> int:
        for attr in ("tradable_lot", "tradable_lots", "orig_lots", "lot", "quantity"):
            val = getattr(position, attr, None)
            if val in (None, ""):
                continue
            # Skip auto-created unittest.mock children (hasattr is always True).
            if type(val).__name__ in ("MagicMock", "Mock", "AsyncMock"):
                continue
            try:
                return int(val)
            except (TypeError, ValueError):
                continue
        return 0

    def _check_margin_sufficient(self, symbol, qty) -> bool:
        """Proactive margin check via query_estimate_margin(account, order).

        Best-effort: any lookup failure is treated as "proceed".
        """
        try:
            order = self._create_futures_order(ORDER_TYPE_BUY, 0, qty, symbol)
            self._rate_limiter.acquire()
            result = self.sdk.futopt.query_estimate_margin(
                self.current_account, order
            )
            # Some SDK versions put a flag on the Result; others only return
            # EstimateMargin data. Treat explicit is_success=False as fail.
            if getattr(result, "is_success", None) is False:
                return False
            data = getattr(result, "data", result)
            sufficient = getattr(data, "is_sufficient", None)
            if sufficient is False:
                return False
        except Exception as e:
            logger.warning(
                f"[{self.trace_id}] query_estimate_margin check failed, "
                f"proceeding without pre-check: {e}"
            )
        return True

    def _classify_order_failure(self, status_code, message) -> Exception:
        """Fallback classifier for post-hoc order failures. No confirmed
        Fubon sub-code for insufficient margin exists yet (only generic status
        90) -- default to NeedRetryError (price-chase) rather than
        NotEnoughMoneyError (qty reduction) until real codes are found.
        """
        return NeedRetryError(f"Order failed, status={status_code}, message={message}")

    # -- position / order queries --------------------------------------------

    def _list_positions(self, product="__ALL__"):
        """Query positions via futopt_accounting (not futopt.*).

        Docs: sdk.futopt_accounting.query_hybrid_position(account)
              sdk.futopt_accounting.query_single_position(account)
        Both return all account positions; we filter by product family client-side.
        """
        accounting = self.sdk.futopt_accounting
        self._rate_limiter.acquire()
        if product == "__ALL__":
            raw = accounting.query_hybrid_position(self.current_account)
        else:
            # single_position also returns the full book; filter below.
            raw = accounting.query_single_position(self.current_account)
        positions = self._unwrap_sdk_list(raw)
        if product != "__ALL__":
            positions = [
                p for p in positions if self._position_matches_product(p, product)
            ]
        return positions

    def _list_working_orders(self, product="__ALL__"):
        self._rate_limiter.acquire()
        raw = self.sdk.futopt.get_order_results(self.current_account)
        order_results = self._unwrap_sdk_list(raw)
        trades = []
        for o in order_results:
            status = str(getattr(o, "status", getattr(o, "order_status", "")))
            if status in (ORDER_STATUS_CANCELLED, ORDER_STATUS_FILLED):
                continue
            if product != "__ALL__" and not self._order_matches_product(o, product):
                continue
            trades.append(o)
        return trades

    def _order_matches_product(self, order, product) -> bool:
        order_sym = str(
            getattr(order, "symbol", getattr(order, "stock_no", "")) or ""
        ).upper()
        if not order_sym:
            return True
        product_u = str(product).upper()
        root = self._product_root(product_u)
        if order_sym == product_u or order_sym.startswith(root):
            return True
        acct = self._accounting_symbol_for_product(product_u)
        if order_sym == acct or order_sym.startswith(acct):
            return True
        return False

    def _wrap_list_positions(self, positions, trades) -> str:
        wrap_results = []
        for position in positions:
            expiry = getattr(position, "expiry_date", None)
            wrap_results.append(
                dict(
                    product=getattr(
                        position, "symbol", getattr(position, "stock_no", None)
                    ),
                    expiry_date=expiry,
                    order_symbol=self._position_order_symbol(position),
                    action=getattr(
                        position, "buy_sell", getattr(position, "direction", None)
                    ),
                    qty=self._position_qty(position),
                    average_price=getattr(
                        position, "price", getattr(position, "avg_price", 0)
                    ),
                    unrealized_profit=getattr(
                        position,
                        "profit_or_loss",
                        getattr(
                            position, "pnl", getattr(position, "unrealized_profit", 0)
                        ),
                    ),
                    market_price=getattr(position, "market_price", None),
                    type="position",
                )
            )
        for trade in trades:
            wrap_results.append(
                dict(
                    product=getattr(
                        trade, "symbol", getattr(trade, "stock_no", None)
                    ),
                    action=getattr(trade, "buy_sell", None),
                    qty=int(
                        getattr(trade, "lot", getattr(trade, "quantity", 0)) or 0
                    ),
                    average_price=getattr(trade, "price", 0),
                    unrealized_profit=0,
                    type="trade",
                )
            )
        return json.dumps(wrap_results, default=str)

    def _has_open_interest(self, product):
        if not self.current_account:
            return (
                dict(
                    api="list_positions",
                    success=False,
                    result="Not login yet, account: %s" % self.current_account,
                ),
                None,
                None,
            )
        retry = 0
        session_reconnects = 0
        while True:
            try:
                positions = self._list_positions(product)
                trades = self._list_working_orders(product)
                return (
                    dict(
                        api="list_positions",
                        success=True,
                        result=self._wrap_list_positions(positions, trades),
                    ),
                    trades,
                    positions,
                )
            except Exception as e:
                if (
                    _is_session_dead_error(e)
                    and session_reconnects < SESSION_RECONNECT_ONCE
                ):
                    session_reconnects += 1
                    logger.warning(
                        f"[{self.trace_id}] _has_open_interest session-dead error "
                        f"(reconnect {session_reconnects}/{SESSION_RECONNECT_ONCE}): {e}"
                    )
                    try:
                        self.reconnect()
                        retry = 0
                        continue
                    except Exception as re_ex:
                        logger.error(
                            f"[{self.trace_id}] reconnect failed during "
                            f"_has_open_interest: {re_ex}, {traceback.format_exc()}"
                        )
                if retry > 3:
                    logger.error(
                        f"[{self.trace_id}] Error after retry _has_open_interest {e}, "
                        f"{traceback.format_exc()}"
                    )
                    return (
                        dict(api="list_positions", success=False, result=str(e)),
                        None,
                        None,
                    )
                retry += 1
                time.sleep(retry * 3)
                logger.info(f"[{self.trace_id}] _has_open_interest start retry {retry}")

    def _get_market_ref_price(self, product):
        """Best-effort market reference for chase-limit pricing (position market_price)."""
        try:
            if not self.current_account:
                return None
            positions = self._list_positions(product)
            for position in positions:
                for attr in (
                    "market_price",
                    "last_price",
                    "mark_price",
                    "current_price",
                ):
                    val = getattr(position, attr, None)
                    if val not in (None, "", 0, "0"):
                        try:
                            return float(val)
                        except (TypeError, ValueError):
                            continue
        except Exception as e:
            logger.warning(
                f"[{self.trace_id}] _get_market_ref_price positions failed: {e}"
            )
        return None

    def _chase_limit_price(self, product, price, order_type: str) -> int:
        """
        Ensure limit price is aggressive enough vs market so cover/open orders fill
        in a moving market (fixes lag behind last_price).
        """
        price_i = int(price)
        market = self._get_market_ref_price(product)
        if market is None:
            return price_i
        market_i = int(market)
        if order_type == ORDER_TYPE_BUY:
            chased = max(price_i, market_i + PRICE_CHASE_BUFFER)
        else:
            chased = min(price_i, market_i - PRICE_CHASE_BUFFER)
        if chased != price_i:
            logger.info(
                f"[{self.trace_id}] _chase_limit_price > product={product} "
                f"order_type={order_type} strategy={price_i} market={market_i} "
                f"chased={chased}"
            )
        return chased

    def _next_chase_price(
        self, product, price, order_type: str, initial_price: int
    ) -> int:
        """Bump price on NeedRetryError: step + re-chase market, soft-capped from initial."""
        if order_type == ORDER_TYPE_BUY:
            stepped = int(price) + PRICE_RETRY_STEP
            soft_cap = int(initial_price) + PRICE_RETRY_LIMIT_OFFSET
            market = self._get_market_ref_price(product)
            if market is not None:
                stepped = max(stepped, int(market) + PRICE_CHASE_BUFFER)
            # Prefer filling over soft cap when market already ran past it
            if market is not None and stepped > soft_cap:
                logger.info(
                    f"[{self.trace_id}] _next_chase_price > allowing above soft_cap "
                    f"soft_cap={soft_cap} market={int(market)} price={stepped}"
                )
                return stepped
            return min(stepped, soft_cap)

        stepped = int(price) - PRICE_RETRY_STEP
        soft_floor = int(initial_price) - PRICE_RETRY_LIMIT_OFFSET
        market = self._get_market_ref_price(product)
        if market is not None:
            stepped = min(stepped, int(market) - PRICE_CHASE_BUFFER)
        if market is not None and stepped < soft_floor:
            logger.info(
                f"[{self.trace_id}] _next_chase_price > allowing below soft_floor "
                f"soft_floor={soft_floor} market={int(market)} price={stepped}"
            )
            return stepped
        return max(stepped, soft_floor)

    def _check_order_info(self, product, expected_action, target_order=""):
        logger.info(
            f"[{self.trace_id}] _check_order_info > [{product}] expected: {expected_action}, "
            f"order_id: {target_order}"
        )
        retry = 0
        while True:
            response, trades, positions = self._has_open_interest(product)
            if not response["success"]:
                raise NeedRetryError(
                    f"[{self.trace_id}] Failed to call _has_open_interest, retry!"
                )

            # expected_action, when given, must be a BSAction member (Buy/Sell) to
            # match the SDK-returned position.buy_sell/direction value below.
            is_expected = False
            if not expected_action:
                if not positions:
                    is_expected = True
            else:
                for position in positions:
                    direction = getattr(
                        position, "buy_sell", getattr(position, "direction", None)
                    )
                    if direction == expected_action:
                        is_expected = True
                        break
            if is_expected:
                return dict(
                    api="check_order_info", success=True, result=response["result"]
                )

            if target_order:
                for trade in trades or []:
                    order_id = getattr(
                        trade, "order_no", getattr(trade, "order_id", None)
                    )
                    status = str(
                        getattr(trade, "status", getattr(trade, "order_status", ""))
                    )
                    if order_id == target_order and status == ORDER_STATUS_FAILED:
                        raise self._classify_order_failure(
                            status, getattr(trade, "message", "")
                        )

            if retry >= ORDER_CHECK_MAX_RETRY:
                raise NeedRetryError(
                    f"[{self.trace_id}] Check order info is not as expected, "
                    f"retry with better price!"
                )
            retry += 1
            sleep_sec = min(retry * 3, ORDER_CHECK_SLEEP_CAP_SEC)
            logger.info(
                f"[{self.trace_id}] _check_order_info start retry {retry} after {sleep_sec}s"
            )
            time.sleep(sleep_sec)

    def _cancel_ongoing_trades(self, trades):
        for trade in trades:
            try:
                self.sdk.futopt.cancel_order(self.current_account, trade)
            except Exception as e:
                logger.error(f"[{self.trace_id}] _cancel_ongoing_trades > error {e}")

    # -- order construction / placement --------------------------------------

    def _create_futures_order(self, buy_sell, price, qty, symbol):
        return FutOptOrder(
            buy_sell=BSAction.Buy if buy_sell == ORDER_TYPE_BUY else BSAction.Sell,
            symbol=symbol,
            price=str(price) if price else "0",
            lot=qty,
            market_type=FutOptMarketType.Future,
            price_type=FutOptPriceType.Market if not price else FutOptPriceType.Limit,
            time_in_force=TimeInForce.ROD,
            order_type=FutOptOrderType.Auto,
            user_def=self.trace_id,
        )

    def _wrap_place_order_result(self, trade) -> str:
        return json.dumps(
            dict(
                order_id=getattr(trade, "order_no", getattr(trade, "order_id", None)),
                status=str(getattr(trade, "status", getattr(trade, "order_status", ""))),
            ),
            default=str,
        )

    def _retry_place_order(self, order):
        retry = 0
        session_reconnects = 0
        while True:
            try:
                trade = self.sdk.futopt.place_order(self.current_account, order)
                if not trade:
                    raise Exception("Failed to place order: empty result")
                return trade
            except Exception as e:
                logger.error(f"[{self.trace_id}] _retry_place_order > Error {e}")
                if (
                    _is_session_dead_error(e)
                    and session_reconnects < SESSION_RECONNECT_ONCE
                ):
                    session_reconnects += 1
                    logger.warning(
                        f"[{self.trace_id}] _retry_place_order session-dead error "
                        f"(reconnect {session_reconnects}/{SESSION_RECONNECT_ONCE}): {e}"
                    )
                    try:
                        self.reconnect()
                        retry = 0
                        continue
                    except Exception as re_ex:
                        logger.error(
                            f"[{self.trace_id}] reconnect failed during "
                            f"_retry_place_order: {re_ex}, {traceback.format_exc()}"
                        )
                if retry > 2:
                    raise Exception(f"[{self.trace_id}] Failed after retry _retry_place_order")
                retry += 1
                time.sleep(retry * 3)

    def _place_order(self, product, buy_sell, price=0, qty=1):
        try:
            symbol = self._convert_symbol(product)
            if price and not self._check_margin_sufficient(symbol, qty):
                raise NotEnoughMoneyError("Estimated margin insufficient for qty=%s" % qty)

            if self.is_dry_run:
                logger.info(
                    f"[{self.trace_id}] DRY RUN _place_order > symbol: {symbol}, buy_sell: {buy_sell}, price: {price}, qty: {qty}"
                )
                return dict(api="place_order", success=True, result="dry_run", order_id="dry_run")

            order = self._create_futures_order(buy_sell, price, qty, symbol)
            trade = self._retry_place_order(order)
            status = str(getattr(trade, "status", getattr(trade, "order_status", "")))
            success = status != ORDER_STATUS_FAILED
            return dict(
                api="place_order",
                success=success,
                result=self._wrap_place_order_result(trade),
                order_id=getattr(trade, "order_no", getattr(trade, "order_id", None)),
            )
        except NotEnoughMoneyError:
            raise
        except Exception as e:
            logger.error(f"[{self.trace_id}] Error on _place_order > {e}, {traceback.format_exc()}")
            return dict(api="place_order", success=False, result=str(e))

    # -- command implementations (futures only; stock is TODO) --------------

    def ListProfitLoss(self, begin_date: str, end_date: str):
        # TODO: implement via sdk.futopt accounting APIs once field shapes are confirmed.
        return [dict(api="ListProfitLoss", success=False, result="Not implemented yet")]

    def HasOpenInterest(self, product: str):
        result = []
        response, _, _ = self._has_open_interest(product)
        result.append(response)
        if product.startswith("MXF"):
            response, _, _ = self._has_open_interest(product.replace("MXF", "TXF"))
            result.append(response)
        return result

    def _mayday_futures(self, product, price=None):
        results = []
        response, trades, positions = self._has_open_interest(product)
        results.append(response)
        if not response["success"]:
            return results

        if trades:
            self._cancel_ongoing_trades(trades)
            response, trades, positions = self._has_open_interest(product)
            results.append(response)

        if not positions:
            return results

        orders = []
        for position in positions:
            position_product = getattr(position, "symbol", getattr(position, "stock_no", None))
            if position_product != product and position_product != self._convert_symbol(product):
                continue
            direction = getattr(position, "buy_sell", getattr(position, "direction", None))
            qty = self._position_qty(position)
            for batch_qty in _iter_batches(qty):
                order_response = self._place_order(
                    product=product,
                    buy_sell=ORDER_TYPE_BUY if direction == BSAction.Sell else ORDER_TYPE_SELL,
                    qty=batch_qty,
                    price=price or 0,
                )
                results.append(order_response)
                orders.append(order_response)
                if not order_response["success"]:
                    return results

        for order_response in orders:
            results.append(self._check_order_info(product, None, order_response.get("order_id")))
        return results

    def MayDay(self, product, price=None):
        retry = 0
        while True:
            if product.isnumeric():
                return [dict(api="MayDay", success=False, result="Stock MayDay is not implemented yet (TODO)")]
            try:
                return self._mayday_futures(product, price)
            except NeedRetryError as e:
                if retry > 2:
                    logger.error(f"[{self.trace_id}] Error after retry MayDay {e}")
                    return [dict(api="MayDay", success=False, result=str(e))]
                retry += 1

    def _position_matches_product(self, position, product, converted=None) -> bool:
        """Match HybridPosition/Position to strategy product (TXF/MXF/TMF/...).

        Accounting uses FITX/FIMTX/FTMX + expiry; order book may use TXFD4.
        """
        pos_sym = getattr(position, "symbol", getattr(position, "stock_no", None))
        if pos_sym is None:
            return True
        pos_u = str(pos_sym).upper()
        product_u = str(product).upper()
        root = self._product_root(product_u)
        acct = self._accounting_symbol_for_product(product_u)
        if pos_u == product_u or pos_u == acct or pos_u.startswith(root):
            return True
        if product_u.startswith(pos_u):
            return True
        # Optional: converted order symbol from caller
        if converted and pos_u == str(converted).upper():
            return True
        order_sym = self._position_order_symbol(position)
        if order_sym:
            ou = str(order_sym).upper()
            if ou == product_u or ou.startswith(root):
                return True
        return False

    def _count_exist_buy_qty(self, product, positions) -> int:
        """Sum existing long qty for product (positions only, not working orders)."""
        total = 0
        for position in positions or []:
            if not self._position_matches_product(position, product):
                continue
            direction = getattr(
                position, "buy_sell", getattr(position, "direction", None)
            )
            if direction != BSAction.Buy:
                continue
            total += self._position_qty(position)
        return total

    def _max_qty_noop_result(self, exist_buy_qty, max_qty):
        return dict(
            api="Buy",
            success=True,
            result=f"No-op: exist_buy_qty={exist_buy_qty} >= max_qty={max_qty}",
        )

    def _buy_futures(self, product, price, qty=1, max_qty=0):
        """Open (or flip to) long. When max_qty > 0, final long is capped.

        e.g. exist_buy=3, qty=3, max_qty=5 => place 2 (final long 5).
        Omit max_qty / 0 keeps previous uncapped BUY behavior
        (etensword-agent a5009bd / d45a159).

        Important (d45a159): if already at/over max_qty, return no-op *before*
        canceling working orders or closing shorts — leave the book untouched.
        """
        logger.info(
            f"[{self.trace_id}] _buy_futures > product: {product}, price: {price}, "
            f"qty: {qty}, max_qty: {max_qty}"
        )
        result = []
        response, trades, positions = self._has_open_interest(product)
        result.append(response)
        if not response["success"]:
            return result

        # Count existing long first. If already at/over max_qty, do nothing
        # (no cancel of working orders, no close short, no open).
        exist_buy_qty = self._count_exist_buy_qty(product, positions)
        if max_qty and max_qty > 0 and exist_buy_qty >= max_qty:
            logger.info(
                f"[{self.trace_id}] _buy_futures > no-op: "
                f"exist_buy_qty={exist_buy_qty} >= max_qty={max_qty} "
                f"(leave existing orders/positions untouched)"
            )
            result.append(self._max_qty_noop_result(exist_buy_qty, max_qty))
            return result

        # Only cancel working orders when we still need to act under max_qty.
        if trades:
            self._cancel_ongoing_trades(trades)
            response, trades, positions = self._has_open_interest(product)
            result.append(response)
            # Re-count after cancel/refresh in case positions changed.
            exist_buy_qty = self._count_exist_buy_qty(product, positions)
            if max_qty and max_qty > 0 and exist_buy_qty >= max_qty:
                logger.info(
                    f"[{self.trace_id}] _buy_futures > no-op after refresh: "
                    f"exist_buy_qty={exist_buy_qty} >= max_qty={max_qty}"
                )
                result.append(self._max_qty_noop_result(exist_buy_qty, max_qty))
                return result

        # Close existing shorts first.
        close_orders = []
        for position in positions or []:
            if not self._position_matches_product(position, product):
                continue
            direction = getattr(
                position, "buy_sell", getattr(position, "direction", None)
            )
            if direction != BSAction.Sell:
                continue
            position_qty = self._position_qty(position)
            for batch_qty in _iter_batches(position_qty):
                order_response = self._place_order(
                    product=product,
                    buy_sell=ORDER_TYPE_BUY,
                    price=price,
                    qty=batch_qty,
                )
                result.append(order_response)
                close_orders.append(order_response)
                if not order_response["success"]:
                    return result

        for order_response in close_orders:
            self._check_order_info(product, None, order_response.get("order_id"))

        # Cap open qty so final long does not exceed max_qty when set.
        # Early no-op above already handled room <= 0.
        target_qty = int(qty)
        if max_qty and max_qty > 0:
            room = int(max_qty) - exist_buy_qty
            target_qty = min(target_qty, max(room, 0))
            logger.info(
                f"[{self.trace_id}] _buy_futures > max_qty cap: "
                f"exist_buy_qty={exist_buy_qty}, qty={qty}, max_qty={max_qty}, "
                f"place_qty={target_qty}"
            )

        buy_orders = []
        for batch_qty in _iter_batches(target_qty):
            order_response = self._place_order(
                product=product, buy_sell=ORDER_TYPE_BUY, price=price, qty=batch_qty
            )
            result.append(order_response)
            buy_orders.append(order_response)
            if not order_response["success"]:
                return result

        for order_response in buy_orders:
            result.append(
                self._check_order_info(
                    product, BSAction.Buy, order_response.get("order_id")
                )
            )
        return result

    def Buy(self, product, price, qty=1, max_qty=0):
        retry = 0
        while True:
            logger.info(
                f"[{self.trace_id}] Buy > product: {product}, price: {price}, "
                f"qty: {qty}, max_qty: {max_qty}"
            )
            if product.isnumeric():
                return [
                    dict(
                        api="Buy",
                        success=False,
                        result="Stock Buy is not implemented yet (TODO)",
                    )
                ]
            if qty < 1:
                return [dict(api="Buy", success=False, result=f"Invalid qty: {qty}")]
            try:
                return self._buy_futures(product, price, qty, max_qty=max_qty)
            except NotEnoughMoneyError:
                qty -= 1
                retry += 1
                logger.info(
                    f"[{self.trace_id}] Buy start retry {retry} with new qty:{qty}"
                )
                if qty < 1:
                    return [
                        dict(
                            api="Buy",
                            success=False,
                            result="NotEnoughMoneyError after retry",
                        )
                    ]
            except NeedRetryError:
                return [
                    dict(api="Buy", success=False, result="Order is not completed yet")
                ]

    def _close_and_buy_futures(self, product, price, qty=1):
        response, trades, positions = self._has_open_interest(product)
        result = [response]
        if not response["success"]:
            return result

        if trades:
            self._cancel_ongoing_trades(trades)
            response, trades, positions = self._has_open_interest(product)
            result.append(response)

        # Chase market so limit covers are not left behind last_price
        place_price = self._chase_limit_price(product, price, ORDER_TYPE_BUY)

        exist_buy_qty = 0
        orders = []
        for position in positions:
            if not self._position_matches_product(position, product):
                continue
            direction = getattr(position, "buy_sell", getattr(position, "direction", None))
            position_qty = self._position_qty(position)
            if direction == BSAction.Sell:
                for batch_qty in _iter_batches(position_qty):
                    order_response = self._place_order(
                        product=product,
                        buy_sell=ORDER_TYPE_BUY,
                        price=place_price,
                        qty=batch_qty,
                    )
                    result.append(order_response)
                    orders.append(order_response)
                    if not order_response["success"]:
                        return result
            elif direction == BSAction.Buy:
                exist_buy_qty += position_qty

        if exist_buy_qty == 0 and orders:
            for order_response in orders:
                self._check_order_info(product, None, order_response.get("order_id"))
        else:
            logger.info(
                f"[{self.trace_id}] _close_and_buy_futures > existing buy order > {exist_buy_qty}"
            )

        # then buy (re-chase in case market moved during close wait)
        place_price = self._chase_limit_price(product, price, ORDER_TYPE_BUY)
        buy_orders = []
        if qty > exist_buy_qty:
            for batch_qty in _iter_batches(qty - exist_buy_qty):
                order_response = self._place_order(
                    product=product,
                    buy_sell=ORDER_TYPE_BUY,
                    price=place_price,
                    qty=batch_qty,
                )
                result.append(order_response)
                buy_orders.append(order_response)
                if not order_response["success"]:
                    return result

        if qty > 0 and buy_orders:
            for order_response in buy_orders:
                result.append(
                    self._check_order_info(
                        product, BSAction.Buy, order_response.get("order_id")
                    )
                )
        return result

    def CloseAndBuy(self, product, price, qty=1):
        retry = 0
        initial_price = int(price)
        price = initial_price
        while True:
            if product.isnumeric():
                return [
                    dict(
                        api="CloseAndBuy",
                        success=False,
                        result="Stock CloseAndBuy is not implemented yet (TODO)",
                    )
                ]
            try:
                return self._close_and_buy_futures(product, price, qty)
            except NotEnoughMoneyError as e:
                if qty == 1:
                    return [dict(api="CloseAndBuy", success=False, result=str(e))]
                qty -= 1
                logger.info(
                    f"[{self.trace_id}] CloseAndBuy start retry with new qty:{qty}"
                )
            except NeedRetryError as e:
                if retry >= CLOSE_OPEN_MAX_OUTER_RETRY:
                    return [dict(api="CloseAndBuy", success=False, result=str(e))]
                retry += 1
                price = self._next_chase_price(
                    product, price, ORDER_TYPE_BUY, initial_price
                )
                logger.info(
                    f"[{self.trace_id}] CloseAndBuy start retry {retry} with new price {price}"
                )

    def _close_and_sell_futures(self, product, price, qty=1):
        response, trades, positions = self._has_open_interest(product)
        result = [response]
        if not response["success"]:
            return result

        if trades:
            self._cancel_ongoing_trades(trades)
            response, trades, positions = self._has_open_interest(product)
            result.append(response)

        place_price = self._chase_limit_price(product, price, ORDER_TYPE_SELL)

        exist_sell_qty = 0
        orders = []
        for position in positions:
            if not self._position_matches_product(position, product):
                continue
            direction = getattr(position, "buy_sell", getattr(position, "direction", None))
            position_qty = self._position_qty(position)
            if direction == BSAction.Buy:
                for batch_qty in _iter_batches(position_qty):
                    order_response = self._place_order(
                        product=product,
                        buy_sell=ORDER_TYPE_SELL,
                        price=place_price,
                        qty=batch_qty,
                    )
                    result.append(order_response)
                    orders.append(order_response)
                    if not order_response["success"]:
                        return result
            elif direction == BSAction.Sell:
                exist_sell_qty += position_qty

        if exist_sell_qty == 0 and orders:
            for order_response in orders:
                self._check_order_info(product, None, order_response.get("order_id"))
        else:
            logger.info(
                f"[{self.trace_id}] _close_and_sell_futures > existing sell order > {exist_sell_qty}"
            )

        # then sell (re-chase in case market moved during close wait)
        place_price = self._chase_limit_price(product, price, ORDER_TYPE_SELL)
        sell_orders = []
        if qty > exist_sell_qty:
            for batch_qty in _iter_batches(qty - exist_sell_qty):
                order_response = self._place_order(
                    product=product,
                    buy_sell=ORDER_TYPE_SELL,
                    price=place_price,
                    qty=batch_qty,
                )
                result.append(order_response)
                sell_orders.append(order_response)
                if not order_response["success"]:
                    return result

        if qty > 0 and sell_orders:
            for order_response in sell_orders:
                result.append(
                    self._check_order_info(
                        product, BSAction.Sell, order_response.get("order_id")
                    )
                )
        return result

    def CloseAndSell(self, product, price, qty=1):
        retry = 0
        initial_price = int(price)
        price = initial_price
        while True:
            if product.isnumeric():
                return [
                    dict(
                        api="CloseAndSell",
                        success=False,
                        result="Stock CloseAndSell is not implemented yet (TODO)",
                    )
                ]
            try:
                return self._close_and_sell_futures(product, price, qty)
            except NotEnoughMoneyError as e:
                if qty == 1:
                    return [dict(api="CloseAndSell", success=False, result=str(e))]
                qty -= 1
                logger.info(
                    f"[{self.trace_id}] CloseAndSell start retry with new qty:{qty}"
                )
            except NeedRetryError as e:
                if retry >= CLOSE_OPEN_MAX_OUTER_RETRY:
                    return [dict(api="CloseAndSell", success=False, result=str(e))]
                retry += 1
                price = self._next_chase_price(
                    product, price, ORDER_TYPE_SELL, initial_price
                )
                logger.info(
                    f"[{self.trace_id}] CloseAndSell start retry {retry} with new price {price}"
                )

    def Sell(self, product, price, qty=1):
        return [dict(api="Sell", success=False, result="Sell is not implemented (matches reference repo scope)")]
