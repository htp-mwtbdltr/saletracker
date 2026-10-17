"""SQLite storage. All access goes through a single connection guarded by a lock;
calls are fast enough to run directly from async code."""
import json
import sqlite3
import threading
import time
from typing import Any, Optional

from . import config

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS trackers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    site TEXT NOT NULL,              -- microcenter | amazon
    kind TEXT NOT NULL,              -- search | product
    enabled INTEGER NOT NULL DEFAULT 1,
    interval_min INTEGER,            -- NULL = use default
    channel_id TEXT,                 -- NULL = use default
    options TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    last_checked REAL,
    last_ok REAL,
    last_error TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0,
    baseline_done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    tracker_id INTEGER NOT NULL REFERENCES trackers(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    name TEXT,
    url TEXT,
    image TEXT,
    first_seen REAL,
    last_seen REAL,
    present INTEGER NOT NULL DEFAULT 1,
    ignored INTEGER NOT NULL DEFAULT 0,
    prices TEXT NOT NULL DEFAULT '{}',
    extra TEXT NOT NULL DEFAULT '{}',
    UNIQUE(tracker_id, key)
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    series TEXT NOT NULL,
    price REAL,                      -- NULL = became unavailable
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hist_item ON price_history(item_id, series, ts);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    tracker_id INTEGER NOT NULL REFERENCES trackers(id) ON DELETE CASCADE,
    item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,  -- NULL = all items
    series TEXT NOT NULL DEFAULT 'lowest',
    type TEXT NOT NULL DEFAULT 'below',  -- below | drop_pct | all_time_low
    value REAL,
    enabled INTEGER NOT NULL DEFAULT 1,
    note TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS alert_state (
    alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    fired_price REAL,                -- NULL = armed
    fired_at REAL,
    PRIMARY KEY (alert_id, item_id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    tracker_id INTEGER REFERENCES trackers(id) ON DELETE SET NULL,
    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
    type TEXT NOT NULL,
    title TEXT,
    message TEXT,
    url TEXT,
    sent INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""

JSON_COLS = {"options", "prices", "extra"}


def _row(r: Optional[sqlite3.Row]) -> Optional[dict]:
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS & d.keys():
        d[k] = json.loads(d[k] or "{}")
    return d


def init():
    global _conn
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.executescript(SCHEMA)
    for k, v in config.DEFAULT_SETTINGS.items():
        _conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))


def q(sql: str, params: tuple = ()) -> list[dict]:
    with _lock:
        return [_row(r) for r in _conn.execute(sql, params).fetchall()]


def q1(sql: str, params: tuple = ()) -> Optional[dict]:
    with _lock:
        return _row(_conn.execute(sql, params).fetchone())


def ex(sql: str, params: tuple = ()) -> int:
    """Execute a write; returns lastrowid."""
    with _lock:
        return _conn.execute(sql, params).lastrowid


def _enc(v: Any) -> Any:
    return json.dumps(v) if isinstance(v, (dict, list)) else v


def update(table: str, row_id: int, fields: dict):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    ex(f"UPDATE {table} SET {cols} WHERE id = ?", (*map(_enc, fields.values()), row_id))


def insert(table: str, fields: dict) -> int:
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    return ex(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(map(_enc, fields.values())))


# ---------- settings ----------
def get_settings() -> dict:
    return {r["key"]: r["value"] for r in q("SELECT key, value FROM settings")}


def get_setting(key: str, default: str = "") -> str:
    r = q1("SELECT value FROM settings WHERE key = ?", (key,))
    return r["value"] if r and r["value"] is not None else default


def set_settings(values: dict):
    for k, v in values.items():
        ex("INSERT INTO settings(key, value) VALUES (?, ?) "
           "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, "" if v is None else str(v)))


# ---------- trackers ----------
def get_tracker(tid: int) -> Optional[dict]:
    return q1("SELECT * FROM trackers WHERE id = ?", (tid,))


def list_trackers() -> list[dict]:
    return q("SELECT * FROM trackers ORDER BY id")


def tracker_interval(t: dict) -> int:
    return int(t.get("interval_min") or get_setting("default_interval_min", "15") or 15)


# ---------- items ----------
def get_item(iid: int) -> Optional[dict]:
    return q1("SELECT * FROM items WHERE id = ?", (iid,))


def tracker_items(tid: int) -> list[dict]:
    return q("SELECT * FROM items WHERE tracker_id = ? ORDER BY present DESC, last_seen DESC, id", (tid,))


def record_price(item_id: int, series: str, price: Optional[float], now: float):
    """Append to history when the price changed or the heartbeat interval passed."""
    last = q1("SELECT price, ts FROM price_history WHERE item_id = ? AND series = ? "
              "ORDER BY ts DESC LIMIT 1", (item_id, series))
    if last is None:
        if price is None:
            return
    elif last["price"] == price and now - last["ts"] < config.HISTORY_HEARTBEAT_SEC:
        return
    elif last["price"] is None and price is None:
        return
    ex("INSERT INTO price_history(item_id, series, price, ts) VALUES (?, ?, ?, ?)",
       (item_id, series, price, now))


def history(item_id: int, since: float = 0) -> list[dict]:
    return q("SELECT series, price, ts FROM price_history WHERE item_id = ? AND ts >= ? ORDER BY ts",
             (item_id, since))


def lows(item_id: int) -> dict:
    """All-time low and high per series."""
    rows = q("SELECT series, MIN(price) AS low, MAX(price) AS high FROM price_history "
             "WHERE item_id = ? AND price IS NOT NULL GROUP BY series", (item_id,))
    return {r["series"]: {"low": r["low"], "high": r["high"]} for r in rows}


# ---------- alerts ----------
def tracker_alerts(tid: int) -> list[dict]:
    return q("SELECT * FROM alerts WHERE tracker_id = ? ORDER BY id", (tid,))


def get_alert_state(alert_id: int, item_id: int) -> Optional[dict]:
    return q1("SELECT * FROM alert_state WHERE alert_id = ? AND item_id = ?", (alert_id, item_id))


def set_alert_state(alert_id: int, item_id: int, fired_price: Optional[float], fired_at: Optional[float]):
    ex("INSERT INTO alert_state(alert_id, item_id, fired_price, fired_at) VALUES (?, ?, ?, ?) "
       "ON CONFLICT(alert_id, item_id) DO UPDATE SET fired_price = excluded.fired_price, "
       "fired_at = excluded.fired_at", (alert_id, item_id, fired_price, fired_at))


# ---------- events ----------
def add_event(type_: str, title: str, message: str = "", tracker_id=None, item_id=None,
              url: str = "", sent: bool = False) -> int:
    return insert("events", {"ts": time.time(), "tracker_id": tracker_id, "item_id": item_id,
                             "type": type_, "title": title, "message": message, "url": url,
                             "sent": int(sent)})


def recent_events(limit: int = 100, tracker_id: Optional[int] = None) -> list[dict]:
    if tracker_id:
        return q("SELECT * FROM events WHERE tracker_id = ? ORDER BY ts DESC LIMIT ?", (tracker_id, limit))
    return q("SELECT e.*, t.name AS tracker_name FROM events e LEFT JOIN trackers t ON t.id = e.tracker_id "
             "ORDER BY e.ts DESC LIMIT ?", (limit,))
