#!/usr/bin/env python3
"""Live Fubon Neo *password* login connectivity test CLI.

Password+cert login is for first-time 連線測試 / account activation only.
Runtime OrderAgent defaults to API Key login — use scripts/test_fubon_apikey_login.py
for the production path.

Docs:
  https://www.fbs.com.tw/TradeAPI/docs/trading/prepare
  https://www.fbs.com.tw/TradeAPI/docs/trading/library/python/login/loginPassword

Usage:
  .venv/bin/python scripts/test_fubon_login.py
  .venv/bin/python scripts/test_fubon_login.py /path/to/agent_settings.yaml

This script only logs in and lists accounts — it does not place orders.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from fubon_agent.login_check import (
    LoginResult,
    load_fubon_config,
    login_and_list_accounts,
    mask_id,
    resolve_config_path,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Test Fubon Neo API login using agent_settings.yaml credentials."
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
        cfg = load_fubon_config(config_path)
    except (OSError, ValueError) as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    print("=== Fubon Neo login test ===")
    print(f"config     : {config_path}")
    print(f"person_id  : {mask_id(cfg['person_id'])}")
    print(f"cert_path  : {cfg['cert_path']}")
    print(f"cert exists: {Path(cfg['cert_path']).is_file()}")
    print(f"is_dry_run : {cfg.get('is_dry_run')}")
    print("logging in ...")

    try:
        result = login_and_list_accounts(cfg)
    except ImportError as exc:
        print(f"SDK ERROR: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print("LOGIN FAILED", file=sys.stderr)
        print(f"  {type(exc).__name__}: {exc}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        return 1

    if result.status == LoginResult.CONNECTION_TEST_OK:
        print("CONNECTION TEST OK (trading not fully activated yet)")
        print(f"  is_success : {result.raw_is_success}")
        print(f"  message    : {result.message}")
        print()
        print(
            "Meaning (from Fubon prepare / 連線測試):\n"
            "  - person_id / password / cert / cert_password are accepted\n"
            "  - API 使用風險暨聲明書 may still be pending, or just signed\n"
            "  - trading permission typically opens the next business day\n"
            "  - re-run this script tomorrow to confirm full login"
        )
        print("logout complete")
        return 0

    print(f"LOGIN OK — {len(result.accounts)} account(s)")
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
