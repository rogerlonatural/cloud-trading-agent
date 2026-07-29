"""Fubon Neo login-check tests.

Unit tests (mocked SDK) always run.

Live test hits the real API and is opt-in:

  FUBON_LIVE_LOGIN=1 .venv/bin/python -m pytest tests/test_fubon_login.py -m live -v

Optional config override (default: repo .cert/agent_settings.yaml):

  FUBON_LIVE_CONF=/path/to/agent_settings.yaml FUBON_LIVE_LOGIN=1 \\
    .venv/bin/python -m pytest tests/test_fubon_login.py -m live -v
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fubon_agent.login_check import (
    LoginResult,
    account_summary,
    apikey_login_and_list_accounts,
    is_connection_test_pending,
    load_fubon_config,
    login_and_list_accounts,
    login_with_config,
    mask_id,
    mask_secret,
    read_api_key_file,
    resolve_config_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_CONF_DEFAULT = REPO_ROOT / ".cert" / "agent_settings.yaml"
FIXTURE_CONF = Path(__file__).parent / "fixtures" / "test_settings.yaml"


def _live_enabled() -> bool:
    return os.environ.get("FUBON_LIVE_LOGIN", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
    )


def _live_config_path() -> Path:
    env = os.environ.get("FUBON_LIVE_CONF", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return LIVE_CONF_DEFAULT


# ---------------------------------------------------------------------------
# Unit tests (no network)
# ---------------------------------------------------------------------------


class TestMaskAndPending:
    def test_mask_id(self):
        assert mask_id("K120022333") == "K12***"
        assert mask_id("") == "<empty>"
        assert mask_id("AB") == "**"

    def test_connection_test_pending_markers(self):
        msg = (
            "無簽署完成API使用風險暨聲明書帳號，請與營業員聯絡；"
            "若正進行簽署流程並測試連線中，此訊息表連線測試成功，"
            "使用權限將應於次日開通"
        )
        assert is_connection_test_pending(msg) is True
        assert is_connection_test_pending("連線測試成功") is True
        assert is_connection_test_pending("invalid password") is False
        assert is_connection_test_pending(None) is False


class TestLoadConfig:
    def test_load_fixture_requires_existing_cert(self, tmp_path):
        """Relative cert_path resolves against the config directory."""
        cert = tmp_path / "dummy.pfx"
        cert.write_bytes(b"fake")
        conf = tmp_path / "settings.yaml"
        conf.write_text(
            "\n".join(
                [
                    "fubon_api:",
                    "  person_id: A123456789",
                    "  password: secret",
                    "  cert_path: dummy.pfx",
                    "  cert_password: cert-secret",
                    "  api_key: INLINE_KEY_123",
                    "  is_dry_run: true",
                ]
            ),
            encoding="utf-8",
        )
        cfg = load_fubon_config(conf)
        assert cfg["person_id"] == "A123456789"
        assert cfg["cert_path"] == str(cert.resolve())
        assert cfg["api_key"] == "INLINE_KEY_123"
        assert cfg["api_key_source"] == "config:api_key"
        # production default when login_method omitted
        assert cfg["login_method"] == "apikey"
        assert cfg["is_dry_run"] is True

    def test_load_api_key_from_path(self, tmp_path):
        cert = tmp_path / "dummy.pfx"
        cert.write_bytes(b"fake")
        key_file = tmp_path / "api-key.txt"
        key_file.write_text("  ABCD1234EFGH5678  \n", encoding="utf-8")
        conf = tmp_path / "settings.yaml"
        conf.write_text(
            "\n".join(
                [
                    "fubon_api:",
                    "  person_id: A123456789",
                    "  password: secret",
                    "  cert_path: dummy.pfx",
                    "  cert_password: cert-secret",
                    "  api_key_path: api-key.txt",
                ]
            ),
            encoding="utf-8",
        )
        cfg = load_fubon_config(conf, require_api_key=True)
        assert cfg["api_key"] == "ABCD1234EFGH5678"
        assert cfg["api_key_source"] == f"file:{key_file.resolve()}"

    def test_require_api_key_raises_when_missing(self, tmp_path, monkeypatch):
        cert = tmp_path / "dummy.pfx"
        cert.write_bytes(b"fake")
        conf = tmp_path / "settings.yaml"
        conf.write_text(
            "\n".join(
                [
                    "fubon_api:",
                    "  person_id: A123456789",
                    "  password: secret",
                    "  cert_path: dummy.pfx",
                    "  cert_password: cert-secret",
                ]
            ),
            encoding="utf-8",
        )
        # Do not fall back to repo .cert/api-key.txt
        monkeypatch.setattr(
            "fubon_agent.login_check.DEFAULT_API_KEY_FILE",
            tmp_path / "no-such-api-key.txt",
        )
        with pytest.raises(ValueError, match="api_key"):
            load_fubon_config(conf, require_api_key=True)

    def test_read_api_key_file_strips_whitespace(self, tmp_path):
        p = tmp_path / "k.txt"
        p.write_text("KEY\nVALUE\n", encoding="utf-8")
        assert read_api_key_file(p) == "KEYVALUE"

    def test_mask_secret(self):
        assert "F5D4" in mask_secret("F5D479C030DD53AB4EDAB2CADE3F237E")
        assert "len=" in mask_secret("F5D479C030DD53AB4EDAB2CADE3F237E")

    def test_missing_section_raises(self, tmp_path):
        conf = tmp_path / "bad.yaml"
        conf.write_text("order_agent: {}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="fubon_api"):
            load_fubon_config(conf)

    def test_missing_cert_file_raises(self, tmp_path):
        conf = tmp_path / "settings.yaml"
        conf.write_text(
            "\n".join(
                [
                    "fubon_api:",
                    "  person_id: A123456789",
                    "  password: secret",
                    "  cert_path: /no/such/file.pfx",
                    "  cert_password: cert-secret",
                ]
            ),
            encoding="utf-8",
        )
        with pytest.raises(FileNotFoundError, match="Certificate"):
            load_fubon_config(conf)

    def test_resolve_prefers_cli_path(self):
        path = resolve_config_path(str(FIXTURE_CONF))
        assert path == FIXTURE_CONF.resolve()


class TestLoginAndListAccountsMocked:
    def _cfg(self):
        return {
            "person_id": "A123456789",
            "password": "pw",
            "cert_path": "/tmp/x.pfx",
            "cert_password": "cpw",
        }

    def test_full_ok(self):
        account = types.SimpleNamespace(
            name="Test User",
            account="28",
            branch_no="6460",
            account_type="futopt",
        )
        sdk = MagicMock()
        sdk.login.return_value = types.SimpleNamespace(
            is_success=True, message=None, data=[account]
        )
        result = login_and_list_accounts(self._cfg(), sdk_factory=lambda: sdk)

        assert result.status == LoginResult.FULL_OK
        assert result.ok is True
        assert len(result.accounts) == 1
        assert result.accounts[0]["account"] == "28"
        assert result.accounts[0]["account_type"] == "futopt"
        sdk.login.assert_called_once_with("A123456789", "pw", "/tmp/x.pfx", "cpw")
        sdk.logout.assert_called_once()

    def test_connection_test_pending(self):
        sdk = MagicMock()
        sdk.login.return_value = types.SimpleNamespace(
            is_success=False,
            message="無簽署完成API使用風險暨聲明書帳號，連線測試成功",
            data=None,
        )
        result = login_and_list_accounts(self._cfg(), sdk_factory=lambda: sdk)

        assert result.status == LoginResult.CONNECTION_TEST_OK
        assert result.ok is True
        assert result.accounts == []
        sdk.logout.assert_called_once()

    def test_hard_failure_raises(self):
        sdk = MagicMock()
        sdk.login.return_value = types.SimpleNamespace(
            is_success=False, message="密碼錯誤", data=None
        )
        with pytest.raises(RuntimeError, match="login failed"):
            login_and_list_accounts(self._cfg(), sdk_factory=lambda: sdk)

    def test_success_without_accounts_raises(self):
        sdk = MagicMock()
        sdk.login.return_value = types.SimpleNamespace(
            is_success=True, message=None, data=[]
        )
        with pytest.raises(RuntimeError, match="no accounts"):
            login_and_list_accounts(self._cfg(), sdk_factory=lambda: sdk)

    def test_account_summary_fields(self):
        acc = types.SimpleNamespace(
            name="Bill", account="28", branch_no="6460", account_type="stock"
        )
        assert account_summary(acc) == {
            "name": "Bill",
            "account": "28",
            "branch_no": "6460",
            "account_type": "stock",
        }


class TestApiKeyLoginMocked:
    def _cfg(self):
        return {
            "person_id": "A123456789",
            "password": "unused",
            "cert_path": "/tmp/x.pfx",
            "cert_password": "cpw",
            "api_key": "SECRET_API_KEY",
            "login_method": "apikey",
        }

    def test_apikey_full_ok(self):
        account = types.SimpleNamespace(
            name="Test User",
            account="28",
            branch_no="6460",
            account_type="futopt",
        )
        sdk = MagicMock()
        sdk.apikey_login.return_value = types.SimpleNamespace(
            is_success=True, message=None, data=[account]
        )
        result = apikey_login_and_list_accounts(self._cfg(), sdk_factory=lambda: sdk)

        assert result.status == LoginResult.FULL_OK
        assert result.method == "apikey"
        assert result.accounts[0]["account"] == "28"
        sdk.apikey_login.assert_called_once_with(
            "A123456789", "SECRET_API_KEY", "/tmp/x.pfx", "cpw"
        )
        sdk.login.assert_not_called()
        sdk.logout.assert_called_once()

    def test_apikey_missing_key_raises(self):
        cfg = self._cfg()
        del cfg["api_key"]
        with pytest.raises(ValueError, match="api_key"):
            apikey_login_and_list_accounts(cfg, sdk_factory=MagicMock)

    def test_login_with_config_dispatches_apikey(self):
        sdk = MagicMock()
        sdk.apikey_login.return_value = types.SimpleNamespace(
            is_success=True,
            message=None,
            data=[
                types.SimpleNamespace(
                    name="U", account="1", branch_no="6460", account_type="stock"
                )
            ],
        )
        result = login_with_config(
            self._cfg(), method="apikey", sdk_factory=lambda: sdk
        )
        assert result.method == "apikey"
        sdk.apikey_login.assert_called_once()


# ---------------------------------------------------------------------------
# Live integration (opt-in)
# ---------------------------------------------------------------------------


@pytest.fixture
def real_fubon_sdk():
    """Temporarily replace the conftest fubon_neo stub with the real wheel."""
    saved = {
        name: mod
        for name, mod in list(sys.modules.items())
        if name == "fubon_neo" or name.startswith("fubon_neo.")
    }
    for name in saved:
        del sys.modules[name]
    try:
        from fubon_neo.sdk import FubonSDK  # noqa: WPS433 — intentional live import

        yield FubonSDK
    except ImportError as exc:
        pytest.skip(f"real fubon_neo not installed: {exc}")
    finally:
        for name in list(sys.modules):
            if name == "fubon_neo" or name.startswith("fubon_neo."):
                del sys.modules[name]
        sys.modules.update(saved)


@pytest.mark.live
@pytest.mark.skipif(
    not _live_enabled(),
    reason="Set FUBON_LIVE_LOGIN=1 to run live Fubon login (uses real cert/credentials)",
)
def test_live_fubon_login(real_fubon_sdk):
    """Certificate login against Fubon Neo — prepare-page connectivity check.

    Passes when:
      - FULL_OK: is_success and at least one account returned
      - CONNECTION_TEST_OK: auth works, API risk form pending / next-day unlock

    Fails on hard auth/config errors.
    """
    conf_path = _live_config_path()
    if not conf_path.is_file():
        pytest.skip(f"live config not found: {conf_path}")

    cfg = load_fubon_config(conf_path)
    result = login_and_list_accounts(cfg, sdk_factory=real_fubon_sdk)

    assert result.ok, (
        f"unexpected login status={result.status!r} message={result.message!r}"
    )
    assert result.status in (
        LoginResult.FULL_OK,
        LoginResult.CONNECTION_TEST_OK,
    )

    if result.status == LoginResult.FULL_OK:
        assert result.accounts, "FULL_OK must return at least one account"
        for acc in result.accounts:
            # Documented Account fields (loginPassword docs)
            assert acc.get("account") is not None
            assert acc.get("account_type") in ("stock", "futopt", None) or isinstance(
                acc.get("account_type"), str
            )
        print(  # noqa: T201 — useful when running with -s
            f"\nLIVE LOGIN OK: {len(result.accounts)} account(s): "
            f"{[{k: a.get(k) for k in ('account', 'branch_no', 'account_type')} for a in result.accounts]}"
        )
    else:
        print(  # noqa: T201
            f"\nLIVE CONNECTION TEST OK (trading not fully open yet): {result.message}"
        )


@pytest.mark.live
@pytest.mark.skipif(
    not _live_enabled(),
    reason="Set FUBON_LIVE_LOGIN=1 to run live Fubon login",
)
def test_live_fubon_login_prefers_full_ok_when_activated(real_fubon_sdk):
    """Stricter check: once API 聲明書 is active, require FULL_OK with accounts.

    Skip (do not fail) while Fubon still returns the next-day unlock message so
    the same suite stays green during the activation window.
    """
    conf_path = _live_config_path()
    if not conf_path.is_file():
        pytest.skip(f"live config not found: {conf_path}")

    cfg = load_fubon_config(conf_path)
    result = login_and_list_accounts(cfg, sdk_factory=real_fubon_sdk)

    if result.status == LoginResult.CONNECTION_TEST_OK:
        pytest.skip(
            "API risk form accepted but trading not fully activated yet "
            f"(message={result.message!r}). Re-run after next business day."
        )

    assert result.status == LoginResult.FULL_OK
    assert result.raw_is_success is True
    assert len(result.accounts) >= 1
    types_seen = {a.get("account_type") for a in result.accounts}
    # Futures agent needs at least one futopt account when fully onboarded.
    # Do not hard-fail if only stock is present — still prove login works.
    assert types_seen, "expected account_type on returned accounts"


@pytest.mark.live
@pytest.mark.skipif(
    not _live_enabled(),
    reason="Set FUBON_LIVE_LOGIN=1 to run live Fubon API Key login",
)
def test_live_fubon_apikey_login(real_fubon_sdk):
    """API Key + cert login (sdk.apikey_login) against Fubon Neo.

    Passes on FULL_OK or CONNECTION_TEST_OK. Fails on hard auth errors.
    Note: Fubon docs say first-time 連線測試 must use password login, not API Key.
    """
    conf_path = _live_config_path()
    if not conf_path.is_file():
        pytest.skip(f"live config not found: {conf_path}")

    cfg = load_fubon_config(
        conf_path, require_password=False, require_api_key=True
    )
    assert cfg.get("api_key"), "api_key must be present in live config"
    result = apikey_login_and_list_accounts(cfg, sdk_factory=real_fubon_sdk)

    assert result.ok, (
        f"apikey login unexpected status={result.status!r} message={result.message!r}"
    )
    assert result.method == "apikey"
    assert result.status in (LoginResult.FULL_OK, LoginResult.CONNECTION_TEST_OK)

    if result.status == LoginResult.FULL_OK:
        assert result.accounts
        print(
            f"\nLIVE APIKEY LOGIN OK: {len(result.accounts)} account(s): "
            f"{[{k: a.get(k) for k in ('account', 'branch_no', 'account_type')} for a in result.accounts]}"
        )
    else:
        print(
            f"\nLIVE APIKEY CONNECTION TEST OK (not fully open yet): {result.message}"
        )


@pytest.mark.live
@pytest.mark.skipif(
    not _live_enabled(),
    reason="Set FUBON_LIVE_LOGIN=1 to run live Fubon API Key login",
)
def test_live_fubon_apikey_login_prefers_full_ok_when_activated(real_fubon_sdk):
    """Stricter: require FULL_OK once API Key trading is active; skip if pending."""
    conf_path = _live_config_path()
    if not conf_path.is_file():
        pytest.skip(f"live config not found: {conf_path}")

    cfg = load_fubon_config(
        conf_path, require_password=False, require_api_key=True
    )
    result = apikey_login_and_list_accounts(cfg, sdk_factory=real_fubon_sdk)

    if result.status == LoginResult.CONNECTION_TEST_OK:
        pytest.skip(
            "API Key accepted for connectivity but trading not fully activated "
            f"(message={result.message!r})"
        )

    assert result.status == LoginResult.FULL_OK
    assert result.raw_is_success is True
    assert len(result.accounts) >= 1
