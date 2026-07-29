"""Fubon Neo login checks (password + API Key).

Shared by scripts/test_fubon_login.py, scripts/test_fubon_apikey_login.py,
and tests/test_fubon_login.py.

Docs:
  https://www.fbs.com.tw/TradeAPI/docs/trading/prepare
  https://www.fbs.com.tw/TradeAPI/docs/trading/library/python/login/loginPassword
  https://www.fbs.com.tw/TradeAPI/docs/trading/library/python/login/loginAPIKey
  https://www.fbs.com.tw/TradeAPI/docs/trading/api-key-apply
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONF_CANDIDATES = (
    REPO_ROOT / ".cert" / "agent_settings.yaml",
    REPO_ROOT / "config" / "agent_settings.yaml",
)
DEFAULT_API_KEY_FILE = REPO_ROOT / ".cert" / "api-key.txt"

# Fubon returns this when cert/password auth works but API risk disclosure
# is not yet fully activated for trading. Prepare-page 連線測試 treats this
# as connection-test success; trading unlocks the next business day.
CONNECTION_TEST_PENDING_MARKERS = (
    "連線測試成功",
    "使用權限將應於次日開通",
    "無簽署完成API使用風險暨聲明書",
)

LOGIN_METHOD_PASSWORD = "password"
LOGIN_METHOD_APIKEY = "apikey"


class LoginResult:
    """Structured outcome of a live login attempt."""

    FULL_OK = "full_ok"  # is_success + at least one account
    CONNECTION_TEST_OK = "connection_test_ok"  # auth/path OK, trading not open yet
    FAILED = "failed"

    def __init__(
        self,
        status: str,
        accounts: list[dict[str, Any]] | None = None,
        message: str | None = None,
        raw_is_success: Any = None,
        method: str | None = None,
    ):
        self.status = status
        self.accounts = accounts or []
        self.message = message
        self.raw_is_success = raw_is_success
        self.method = method

    @property
    def ok(self) -> bool:
        return self.status in (self.FULL_OK, self.CONNECTION_TEST_OK)


def mask_id(value: str, keep: int = 3) -> str:
    if not value:
        return "<empty>"
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "***"


def mask_secret(value: str, head: int = 4, tail: int = 4) -> str:
    if not value:
        return "<empty>"
    if len(value) <= head + tail:
        return "*" * len(value)
    return f"{value[:head]}...{value[-tail:]} (len={len(value)})"


def resolve_config_path(cli_path: str | None = None) -> Path:
    if cli_path:
        return Path(cli_path).expanduser().resolve()
    env_path = os.environ.get("FUBON_AGENT_CONF")
    if env_path:
        resolved = Path(env_path).expanduser().resolve()
        if resolved.is_file():
            return resolved
    for candidate in DEFAULT_CONF_CANDIDATES:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "No config found. Pass a path, set FUBON_AGENT_CONF, or place "
        f"{DEFAULT_CONF_CANDIDATES[0]}"
    )


def _resolve_path(raw: str | Path, base_dir: Path) -> Path:
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    else:
        path = path.resolve()
    return path


def read_api_key_file(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    # tolerate accidental trailing newlines/spaces only
    key = "".join(text.split())
    if not key:
        raise ValueError(f"API key file is empty: {path}")
    return key


def load_fubon_config(
    config_path: Path,
    *,
    require_password: bool = True,
    require_api_key: bool = False,
) -> dict[str, Any]:
    """Load fubon_api section from agent_settings.yaml.

    API key resolution order:
      1. fubon_api.api_key (inline secret)
      2. fubon_api.api_key_path (file contents)
      3. default .cert/api-key.txt next to repo (if present)
    """
    with config_path.open("r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    fubon = config.get("fubon_api")
    if not isinstance(fubon, dict):
        raise ValueError(f"Missing 'fubon_api' section in {config_path}")

    base_required = ["person_id", "cert_path", "cert_password"]
    if require_password:
        base_required.append("password")
    missing = [k for k in base_required if not fubon.get(k)]
    if missing:
        raise ValueError(f"fubon_api missing required keys: {', '.join(missing)}")

    cert_path = _resolve_path(fubon["cert_path"], config_path.parent)
    if not cert_path.is_file():
        raise FileNotFoundError(f"Certificate file not found: {cert_path}")

    api_key = None
    api_key_source = None
    if fubon.get("api_key"):
        api_key = str(fubon["api_key"]).strip()
        api_key_source = "config:api_key"
    elif fubon.get("api_key_path"):
        key_path = _resolve_path(fubon["api_key_path"], config_path.parent)
        if not key_path.is_file():
            raise FileNotFoundError(f"API key file not found: {key_path}")
        api_key = read_api_key_file(key_path)
        api_key_source = f"file:{key_path}"
    elif DEFAULT_API_KEY_FILE.is_file():
        api_key = read_api_key_file(DEFAULT_API_KEY_FILE)
        api_key_source = f"file:{DEFAULT_API_KEY_FILE}"

    if require_api_key and not api_key:
        raise ValueError(
            "fubon_api.api_key (or api_key_path / .cert/api-key.txt) is required"
        )

    # Production default is API Key; password is for first-time 連線測試 only.
    login_method = str(
        fubon.get("login_method") or LOGIN_METHOD_APIKEY
    ).strip().lower()
    if login_method not in (LOGIN_METHOD_PASSWORD, LOGIN_METHOD_APIKEY):
        raise ValueError(
            f"fubon_api.login_method must be '{LOGIN_METHOD_PASSWORD}' or "
            f"'{LOGIN_METHOD_APIKEY}', got {login_method!r}"
        )

    return {
        "person_id": str(fubon["person_id"]),
        "password": str(fubon.get("password") or ""),
        "cert_path": str(cert_path),
        "cert_password": str(fubon["cert_password"]),
        "api_key": api_key,
        "api_key_source": api_key_source,
        "login_method": login_method,
        "is_dry_run": fubon.get("is_dry_run"),
    }


def account_summary(account: Any) -> dict[str, Any]:
    """Extract documented Account fields without printing secrets."""
    fields = ("name", "account", "branch_no", "account_type")
    return {f: getattr(account, f, None) for f in fields}


def is_connection_test_pending(message: str | None) -> bool:
    if not message:
        return False
    return any(marker in message for marker in CONNECTION_TEST_PENDING_MARKERS)


def safe_logout(sdk: Any, warn: bool = True) -> None:
    try:
        sdk.logout()
    except Exception as logout_err:
        if warn:
            print(f"WARNING: logout failed (ignored): {logout_err}")


def _default_sdk_factory() -> Any:
    try:
        from fubon_neo.sdk import FubonSDK
    except ImportError as exc:
        raise ImportError(
            "fubon_neo is not installed. Download the platform wheel from "
            "https://www.fbs.com.tw/TradeAPI/docs/download/download-sdk "
            "and `pip install` it into this venv."
        ) from exc
    return FubonSDK()


def _interpret_login_result(
    sdk: Any,
    result: Any,
    *,
    method: str,
) -> LoginResult:
    is_success = getattr(result, "is_success", None)
    message = getattr(result, "message", None)
    data = getattr(result, "data", None)

    # Some SDK versions may return a list/tuple of accounts directly.
    if data is None and isinstance(result, (list, tuple)):
        data = result
        is_success = True

    if is_success is False:
        safe_logout(sdk, warn=False)
        if is_connection_test_pending(str(message) if message is not None else None):
            return LoginResult(
                LoginResult.CONNECTION_TEST_OK,
                accounts=[],
                message=str(message),
                raw_is_success=is_success,
                method=method,
            )
        raise RuntimeError(
            f"{method} login failed: is_success=False, message={message!r}"
        )

    accounts = list(data or [])
    if not accounts:
        safe_logout(sdk, warn=False)
        raise RuntimeError(
            f"{method} login returned no accounts. is_success={is_success!r}, "
            f"message={message!r}, result={result!r}"
        )

    summaries = [account_summary(acc) for acc in accounts]
    safe_logout(sdk, warn=False)
    return LoginResult(
        LoginResult.FULL_OK,
        accounts=summaries,
        message=str(message) if message is not None else None,
        raw_is_success=is_success,
        method=method,
    )


def login_and_list_accounts(
    cfg: dict[str, Any],
    sdk_factory: Callable[[], Any] | None = None,
) -> LoginResult:
    """Password + certificate login.

    Official API:
      result = sdk.login(person_id, password, cert_path, cert_password)
    """
    factory = sdk_factory or _default_sdk_factory
    sdk = factory()
    try:
        result = sdk.login(
            cfg["person_id"],
            cfg["password"],
            cfg["cert_path"],
            cfg["cert_password"],
        )
    except Exception:
        try:
            sdk.logout()
        except Exception:
            pass
        raise
    return _interpret_login_result(sdk, result, method=LOGIN_METHOD_PASSWORD)


def apikey_login_and_list_accounts(
    cfg: dict[str, Any],
    sdk_factory: Callable[[], Any] | None = None,
) -> LoginResult:
    """API Key + certificate login (SDK >= v2.2.7).

    Official API:
      result = sdk.apikey_login(person_id, api_key, cert_path, cert_pass)

    Note (Fubon docs): first-time 連線測試 must use password login, not API Key.
    """
    api_key = cfg.get("api_key")
    if not api_key:
        raise ValueError(
            "api_key is required for apikey login "
            "(set fubon_api.api_key or api_key_path / .cert/api-key.txt)"
        )

    factory = sdk_factory or _default_sdk_factory
    sdk = factory()
    try:
        if not hasattr(sdk, "apikey_login"):
            raise RuntimeError(
                "Installed fubon_neo does not expose apikey_login "
                "(need SDK >= v2.2.7)"
            )
        result = sdk.apikey_login(
            cfg["person_id"],
            api_key,
            cfg["cert_path"],
            cfg["cert_password"],
        )
    except Exception:
        try:
            sdk.logout()
        except Exception:
            pass
        raise
    return _interpret_login_result(sdk, result, method=LOGIN_METHOD_APIKEY)


def login_with_config(
    cfg: dict[str, Any],
    *,
    method: str | None = None,
    sdk_factory: Callable[[], Any] | None = None,
) -> LoginResult:
    """Dispatch to password or apikey login based on method / config.

    Default method is apikey (production). Use method='password' only for
    first-time account activation / 連線測試.
    """
    chosen = (method or cfg.get("login_method") or LOGIN_METHOD_APIKEY).lower()
    if chosen == LOGIN_METHOD_APIKEY:
        return apikey_login_and_list_accounts(cfg, sdk_factory=sdk_factory)
    if chosen == LOGIN_METHOD_PASSWORD:
        return login_and_list_accounts(cfg, sdk_factory=sdk_factory)
    raise ValueError(f"Unknown login method: {chosen!r}")
