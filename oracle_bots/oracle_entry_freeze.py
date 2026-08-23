from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
STATE_PATH = Path(__file__).resolve().with_name("oracle_entry_freeze_state.json")


def _missing_status(bot_name: str, account: str, path: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "bot": bot_name,
        "account": account,
        "frozen": False,
        "scope": None,
        "reason": "freeze file missing",
        "state_present": False,
        "state_path": str(path),
    }


def _fail_closed_status(bot_name: str, account: str, path: Path, reason: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "bot": bot_name,
        "account": account,
        "frozen": True,
        "scope": "corrupted",
        "reason": reason,
        "state_present": True,
        "state_path": str(path),
    }


def load_freeze_state(path: Path = STATE_PATH) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"unreadable freeze state: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("freeze state must be a JSON object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("freeze state schema_version mismatch")
    accounts = payload.get("accounts")
    bots = payload.get("bots")
    if not isinstance(accounts, dict) or not isinstance(bots, dict):
        raise ValueError("freeze state missing accounts/bots maps")
    return payload


def entry_freeze_status(bot_name: str, account: str = "BAA", path: Path = STATE_PATH) -> dict[str, Any]:
    account_key = str(account).upper()
    try:
        payload = load_freeze_state(path)
    except Exception as exc:
        return _fail_closed_status(bot_name, account_key, path, f"corrupted/unreadable freeze state: {exc}")
    if payload is None:
        return _missing_status(bot_name, account_key, path)

    accounts = payload["accounts"]
    bots = payload["bots"]
    account_record = accounts.get(account_key) or accounts.get(account.lower()) or accounts.get(account)
    bot_record = bots.get(bot_name) or bots.get(bot_name.lower()) or bots.get(bot_name.upper())
    if isinstance(account_record, dict) and account_record.get("frozen") is True:
        return {
            "schema_version": SCHEMA_VERSION,
            "bot": bot_name,
            "account": account_key,
            "frozen": True,
            "scope": "account",
            "reason": account_record.get("reason") or f"{account_key} account freeze active",
            "state_present": True,
            "state_path": str(path),
        }
    if isinstance(bot_record, dict) and bot_record.get("frozen") is True:
        return {
            "schema_version": SCHEMA_VERSION,
            "bot": bot_name,
            "account": account_key,
            "frozen": True,
            "scope": "bot",
            "reason": bot_record.get("reason") or f"{bot_name} freeze active",
            "state_present": True,
            "state_path": str(path),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "bot": bot_name,
        "account": account_key,
        "frozen": False,
        "scope": None,
        "reason": "freeze state present but no matching freeze",
        "state_present": True,
        "state_path": str(path),
    }


def is_new_entry_frozen(bot_name: str, account: str = "BAA", path: Path = STATE_PATH) -> bool:
    return bool(entry_freeze_status(bot_name, account=account, path=path).get("frozen"))


def describe_entry_freeze(bot_name: str, account: str = "BAA", path: Path = STATE_PATH) -> str:
    status = entry_freeze_status(bot_name, account=account, path=path)
    state = "FROZEN" if status["frozen"] else "OPEN"
    return f"{state} scope={status.get('scope') or 'none'} reason={status.get('reason')}"

