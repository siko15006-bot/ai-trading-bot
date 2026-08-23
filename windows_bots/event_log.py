"""event_log.py -- shared append-only event store for all trading bots.
Design doc: C:\\TradingBot\\Bot_Active\\EVENT_STORE_DESIGN.md (v3, approved
by Ahmed 2026-08-06). One function, log_event(), imported everywhere.

Hard rule: a logging failure must NEVER stop trading. Every path in this
module that can fail is caught and downgraded to a printed warning.
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "events.db")

# --- schema migrations (§12 of the design doc) ---------------------------
# Additive only. Never edit an already-shipped entry -- append a new
# (version, statements) tuple instead.
MIGRATIONS = [
    (1, [
        "PRAGMA journal_mode = WAL",
        "PRAGMA busy_timeout = 5000",
        """CREATE TABLE IF NOT EXISTS schema_meta (
               key   TEXT PRIMARY KEY,
               value TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS events (
               id              INTEGER PRIMARY KEY AUTOINCREMENT,
               schema_version  INTEGER NOT NULL DEFAULT 1,
               timestamp_utc   TEXT    NOT NULL,
               correlation_id  TEXT,
               account         TEXT,
               strategy        TEXT,
               symbol          TEXT,
               event_type      TEXT    NOT NULL,
               ticket          INTEGER,
               retcode         INTEGER,
               reason          TEXT,
               details         TEXT    NOT NULL DEFAULT '{}'
           )""",
        "CREATE INDEX IF NOT EXISTS idx_events_correlation      ON events(correlation_id)",
        "CREATE INDEX IF NOT EXISTS idx_events_time             ON events(timestamp_utc)",
        "CREATE INDEX IF NOT EXISTS idx_events_type             ON events(event_type)",
        "CREATE INDEX IF NOT EXISTS idx_events_account_strategy ON events(account, strategy)",
        "CREATE INDEX IF NOT EXISTS idx_events_ticket           ON events(ticket)",
        "CREATE INDEX IF NOT EXISTS idx_events_reason           ON events(reason)",
        """CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
           BEGIN SELECT RAISE(ABORT, 'events table is append-only: UPDATE forbidden'); END""",
        """CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
           BEGIN SELECT RAISE(ABORT, 'events table is append-only: DELETE forbidden'); END""",
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_events_once_per_ticket
           ON events(ticket, event_type)
           WHERE event_type IN ('ORDER_FILLED', 'POSITION_CLOSED')""",
        """CREATE VIEW IF NOT EXISTS event_store_health AS
           SELECT (SELECT COUNT(*) FROM events)                AS total_events,
                  (SELECT MAX(timestamp_utc) FROM events)       AS last_event_ts,
                  (SELECT COUNT(DISTINCT strategy) FROM events) AS distinct_strategies,
                  (SELECT COUNT(DISTINCT account) FROM events)  AS distinct_accounts""",
        """CREATE VIEW IF NOT EXISTS last_event_per_strategy AS
           SELECT strategy, MAX(timestamp_utc) AS last_event_ts, COUNT(*) AS event_count
           FROM events GROUP BY strategy""",
        """CREATE VIEW IF NOT EXISTS last_event_per_account AS
           SELECT account, MAX(timestamp_utc) AS last_event_ts, COUNT(*) AS event_count
           FROM events GROUP BY account""",
    ]),
]


def ensure_schema(conn):
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone() if _table_exists(conn, "schema_meta") else None
    current = int(row[0]) if row else 0
    for version, statements in MIGRATIONS:
        if version > current:
            with conn:
                for stmt in statements:
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(version),))


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


_local = threading.local()


def _get_conn():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        ensure_schema(conn)
        _local.conn = conn
    return conn


def log_event(event_type, correlation_id=None, account=None, strategy=None,
              symbol=None, ticket=None, retcode=None, reason=None, details=None):
    """Never raises. A failure here must never be allowed to stop trading."""
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO events "
            "(schema_version, timestamp_utc, correlation_id, account, strategy, "
            "symbol, event_type, ticket, retcode, reason, details) "
            "VALUES (1,?,?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
             correlation_id, account, strategy, symbol, event_type,
             ticket, retcode, reason, json.dumps(details or {}, default=str)))
        conn.commit()
    except Exception as e:
        print(f"[event_log] WARNING: failed to log {event_type}: {e}", flush=True)


def _demo():
    """ponytail: smallest useful self-check, not a test suite."""
    import tempfile
    global DB_PATH
    orig = DB_PATH
    DB_PATH = os.path.join(tempfile.gettempdir(), "event_log_demo.db")
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    _local.conn = None

    log_event("BOT_STARTED", account="EA", strategy="demo_bot")
    log_event("SIGNAL_DETECTED", correlation_id="BTCUSDm-123", account="EA",
              strategy="demo_bot", symbol="BTCUSDm", reason="anomaly+h1_agree")
    log_event("ORDER_FILLED", correlation_id="BTCUSDm-123", ticket=999,
              account="EA", strategy="demo_bot", symbol="BTCUSDm",
              details={"temp_id": "BTCUSDm-123", "fill_price": 64000.0})
    # duplicate ORDER_FILLED for the same ticket -- must be silently ignored
    log_event("ORDER_FILLED", correlation_id="BTCUSDm-123", ticket=999,
              account="EA", strategy="demo_bot", symbol="BTCUSDm",
              details={"temp_id": "BTCUSDm-123", "fill_price": 64000.0})
    log_event("POSITION_CLOSED", ticket=999, account="EA", strategy="demo_bot",
              symbol="BTCUSDm", reason="tp", details={"pnl": 12.34})

    conn = _get_conn()
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 4, f"expected 4 rows (dup ORDER_FILLED must be ignored), got {count}"

    # append-only must be enforced
    try:
        conn.execute("UPDATE events SET reason='x' WHERE id=1")
        conn.commit()
        raise AssertionError("UPDATE should have been rejected by the trigger")
    except sqlite3.IntegrityError:
        pass
    try:
        conn.execute("DELETE FROM events WHERE id=1")
        conn.commit()
        raise AssertionError("DELETE should have been rejected by the trigger")
    except sqlite3.IntegrityError:
        pass

    health = conn.execute("SELECT total_events FROM event_store_health").fetchone()[0]
    assert health == 4, f"health view mismatch: {health}"

    # a logging failure must never raise out to the caller
    log_event("ERROR", details={"whatever": object()})  # not JSON-serializable by default,
                                                          # but default=str saves it

    conn.close()
    os.remove(DB_PATH)
    DB_PATH = orig
    print("event_log._demo(): all checks passed")


if __name__ == "__main__":
    _demo()
