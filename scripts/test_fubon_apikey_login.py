#!/usr/bin/env python3
"""Live Fubon Neo API Key login test CLI.

Docs:
  https://www.fbs.com.tw/TradeAPI/docs/trading/library/python/login/loginAPIKey
  https://www.fbs.com.tw/TradeAPI/docs/trading/api-key-apply

Usage:
  .venv/bin/python scripts/test_fubon_apikey_login.py
  .venv/bin/python scripts/test_fubon_apikey_login.py .cert/agent_settings.yaml

  # pytest live
  FUBON_LIVE_LOGIN=1 .venv/bin/python -m pytest tests/test_fubon_login.py -m live -k apikey -v -s

Requires fubon_neo >= 2.2.7. Does not place orders.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from fubon_agent.login_check import (
    LoginResult,
    apikey_login_and_list_accounts,
    load_fubon_config,
    mask_id,
    mask_secret,
    resolve_config_path,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Test Fubon Neo API Key login using agent_settings.yaml."
    )
    parser.add_argument(
        "config",
        nargs="?",
        default=None,
        help="Path to agent_settings.yaml (default: FUBON_AGENT_CONF or .cert/agent_settings.yaml)",
    )
    args = parser.parse_args(argv)

    try:
        config_path = resolve_config_path(args.config)
        cfg = load_fubon_config(
            config_path, require_password=False, require_api_key=True
        )
    except (OSError, ValueError) as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    print("=== Fubon Neo API Key login test ===")
    print(f"config     : {config_path}")
    print(f"person_id  : {mask_id(cfg['person_id'])}")
    print(f"cert_path  : {cfg['cert_path']}")
    print(f"cert exists: {Path(cfg['cert_path']).is_file()}")
    print(f"api_key    : {mask_secret(cfg.get('api_key') or '')}")
    print(f"key source : {cfg.get('api_key_source')}")
    print(f"is_dry_run : {cfg.get('is_dry_run')}")
    print("apikey_login ...")

    try:
        result = apikey_login_and_list_accounts(cfg)
    except ImportError as exc:
        print(f"SDK ERROR: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print("APIKEY LOGIN FAILED", file=sys.stderr)
        print(f"  {type(exc).__name__}: {exc}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        return 1

    if result.status == LoginResult.CONNECTION_TEST_OK:
        print("CONNECTION TEST OK (trading not fully activated yet)")
        print(f"  is_success : {result.raw_is_success}")
        print(f"  message    : {result.message}")
        print()
        print(
            "Note (Fubon docs):\n"
            "  - first-time 連線測試 should use password+cert, not API Key\n"
            "  - if API 聲明書 was just signed, trading may open next day\n"
            "  - re-run after activation to confirm FULL_OK"
        )
        print("logout complete")
        return 0

    print(f"APIKEY LOGIN OK — {len(result.accounts)} account(s)")
    for i, acc in enumerate(result.accounts):
        print(
            f"  [{i}] name={acc.get('name')!r} "
            f"account={acc.get('account')!r} "
            f"branch_no={acc.get('branch_no')!r} "
            f"account_type={acc.get('account_type')!r}"
        )
    print("logout complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
