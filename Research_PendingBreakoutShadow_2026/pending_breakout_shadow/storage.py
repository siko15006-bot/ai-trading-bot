"""Phase 1A: path preparation. Phase 1B: the actual (isolated-directory-
only) JSONL append writer and atomic heartbeat writer. No SQLite yet."""

import os
import json
import time

PROJECT_ROOT = r"C:\TradingBot"
DATA_DIR = os.environ.get(
    "PENDING_BREAKOUT_SHADOW_DATA_DIR",
    os.path.join(PROJECT_ROOT, "Research_PendingBreakoutShadow_2026", "data", "pending_breakout_shadow"),
)

SETUPS_PATH = os.path.join(DATA_DIR, "setups.jsonl")
TRADES_PATH = os.path.join(DATA_DIR, "trades.jsonl")
TICKS_SUMMARY_PATH = os.path.join(DATA_DIR, "ticks_summary.jsonl")
HEARTBEAT_PATH = os.path.join(DATA_DIR, "heartbeat.json")
STATE_PATH = os.path.join(DATA_DIR, "engine_state.json")


def ensure_data_dir():
    """The only side effect this module is allowed: creating the (already
    isolated, already-created) data directory if it's somehow missing.
    Never touches anything outside DATA_DIR."""
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR


def _assert_inside_data_dir(path):
    real_data_dir = os.path.realpath(DATA_DIR)
    real_path = os.path.realpath(path)
    if os.path.commonpath([real_path, real_data_dir]) != real_data_dir:
        raise RuntimeError(f"refusing to write outside isolated data dir: {path}")


def append_jsonl(path, record):
    """Appends one JSON line to `path`. Fails loudly (refuses to write) if
    `path` somehow resolves outside DATA_DIR -- the isolation boundary is
    enforced here, not just by convention."""
    _assert_inside_data_dir(path)
    ensure_data_dir()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def write_heartbeat(fields):
    """Atomic write (tmp file + os.replace, same pattern already used
    elsewhere in this codebase) so a reader never sees a half-written
    heartbeat.json."""
    _assert_inside_data_dir(HEARTBEAT_PATH)
    ensure_data_dir()
    payload = dict(fields)
    payload.setdefault("timestamp", time.time())
    tmp = HEARTBEAT_PATH + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, default=str)
    os.replace(tmp, HEARTBEAT_PATH)


def save_state(snapshot):
    """Atomic write, same tmp+os.replace pattern as write_heartbeat."""
    _assert_inside_data_dir(STATE_PATH)
    ensure_data_dir()
    tmp = STATE_PATH + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, default=str)
    os.replace(tmp, STATE_PATH)


def load_state():
    """Returns the parsed dict, or None if the file is missing/unreadable/
    invalid JSON -- caller (ShadowEngine.restore_from_snapshot) treats None
    the same as 'no prior state', defaulting safely to a fresh IDLE engine."""
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
