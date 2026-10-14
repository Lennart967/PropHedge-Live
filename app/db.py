import json
import sqlite3
from datetime import datetime, timezone

DEFAULT_RULES = {
    "daily_drawdown": 1000.0,
    "consistency_pct": 20.0,
    "max_loss": 3000.0,
    "profit_target": 3000.0,
    "initial_balance": 50000.0,
    "hedge_ratio": 0.5,
    "sl_distance": 50.0,
    "atr_enabled": False,        # SL/TP-Abstand = ATR × Faktor statt fester Abstand
    "atr_timeframe": "H1",
    "atr_period": 14,
    "atr_mult": 2.0,
    "target_cap_enabled": True,  # kurz vor dem Profit-Ziel nur so groß, dass der TP das Ziel gerade erreicht
    "target_buffer_pct": 15.0,   # Aufschlag auf den Rest bis zum Ziel (Slippage beim Schließen von Prop)
    "max_lots": 0.0,             # Obergrenze Prop-Lots, 0 = aus
    "risk_pct": 100.0,
    "buffer_pct": 5.0,
    "safety_pct": 5.0,           # Sicherheitspuffer: so viel % vor DD, Consistency und Max Loss aufhören
    "reset_hour": 0,
    "auto_close_orphan": True,
    "eod_enabled": True,
    "eod_time": "17:00",
    "eod_close_min": 5,
    "eod_block_min": 15,
    "max_price_dev_pct": 1.0,
    "commission_per_lot": 0.0,
    "commission_live_per_lot": 0.0,
    "cost_warn_pct": 10.0,
    "cost_max_pct": 20.0,
    "hedge_mode": "fee",         # fee: Kosten als Gebühr wie im Simulator | sl: Prop-SL exakt | mse
    "ratio_correction": True,
    "ratio_scope": "challenge",
    "ratio_correction_max_pct": 50.0,
    "mirror_brackets": True,
    "margin_max_use_pct": 80.0,
    "max_loss_mode": "static",   # static | eod_trailing
    "trail_lock": False,         # Trailing stoppt beim Startkapital
    "weekend_close_enabled": True,
    "weekend_time": "17:00",     # Freitag, New-York-Zeit
    "weekend_close_min": 15,
    "weekend_block_min": 60,
    "max_risk_enabled": True,
    "max_risk_pct": 1.5,
    "direction_mode": "alternate",  # alternate: 1. zufällig, 2. Gegenrichtung, dann zufällig | random
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_day TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    side TEXT NOT NULL,
    sl_distance REAL NOT NULL,
    hedge_ratio REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL REFERENCES trades(id),
    account TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    ticket INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    volume REAL NOT NULL,
    entry REAL NOT NULL,
    sl REAL NOT NULL,
    tp REAL NOT NULL,
    risk_usd REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    pnl REAL,
    close_price REAL,
    close_reason TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pos_day ON positions(account, trading_day);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    data TEXT
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ts_iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def _all(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def get_rules(self):
        row = self.conn.execute("SELECT value FROM settings WHERE key='rules'").fetchone()
        stored = json.loads(row[0]) if row else {}
        if "consistency_limit" in stored and "consistency_pct" not in stored:
            stored["consistency_pct"] = stored["consistency_limit"] / stored.get(
                "profit_target", DEFAULT_RULES["profit_target"]) * 100
        stored.pop("consistency_limit", None)
        stored.pop("leverage", None)  # kommt jetzt vom Broker (order_calc_margin)
        return {**DEFAULT_RULES, **stored}

    def set_rules(self, rules):
        self.conn.execute("INSERT INTO settings(key,value) VALUES('rules',?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(rules),))
        self.log("rules_changed", rules)

    def get_setting(self, key, default=None):
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        self.conn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def log(self, kind, data):
        self.conn.execute("INSERT INTO events(at,kind,data) VALUES(?,?,?)", (now_iso(), kind, json.dumps(data)))

    def add_trade(self, day, opened_at, side, sl_distance, hedge_ratio):
        cur = self.conn.execute("INSERT INTO trades(trading_day,opened_at,side,sl_distance,hedge_ratio) "
                                "VALUES(?,?,?,?,?)", (day, opened_at, side, sl_distance, hedge_ratio))
        return cur.lastrowid

    def add_position(self, trade_id, account, day, ticket, symbol, side, volume, entry, sl, tp, risk_usd, opened_at):
        self.conn.execute("INSERT INTO positions(trade_id,account,trading_day,ticket,symbol,side,volume,entry,sl,tp,"
                          "risk_usd,opened_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (trade_id, account, day, ticket, symbol, side, volume, entry, sl, tp, risk_usd, opened_at))

    def closed_pnl_by_account(self, day, since=None):
        rows = self._all("SELECT account, SUM(pnl) AS pnl FROM positions WHERE trading_day=? AND opened_at>=? "
                         "AND status='closed' GROUP BY account", (day, since or ""))
        return {r["account"]: r["pnl"] or 0.0 for r in rows}

    def closed_pnl_since(self, since):
        rows = self._all("SELECT account, SUM(pnl) AS pnl FROM positions WHERE opened_at>=? AND status='closed' "
                         "GROUP BY account", (since or "",))
        return {r["account"]: r["pnl"] or 0.0 for r in rows}

    def trade_sides_from_id(self, first_id):
        return [r["side"] for r in self._all("SELECT side FROM trades WHERE id>=? ORDER BY id", (first_id or 0,))]

    def next_trade_id(self):
        return (self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM trades").fetchone()[0] or 0) + 1

    def open_positions(self):
        return self._all("SELECT * FROM positions WHERE status='open'")

    def close_position(self, pid, pnl, close_price, reason, closed_at):
        self.conn.execute("UPDATE positions SET status='closed', pnl=?, close_price=?, close_reason=?, closed_at=? "
                          "WHERE id=?", (pnl, close_price, reason, closed_at, pid))

    def open_trades(self):
        return self._all("SELECT * FROM trades WHERE status='open' ORDER BY id")

    def trade_positions(self, trade_id):
        return self._all("SELECT * FROM positions WHERE trade_id=? ORDER BY account DESC", (trade_id,))

    def close_trade(self, trade_id):
        self.conn.execute("UPDATE trades SET status='closed', closed_at=? WHERE id=?", (now_iso(), trade_id))

    def positions_by_day(self, day, account=None):
        if account:
            return self._all("SELECT * FROM positions WHERE trading_day=? AND account=? ORDER BY id DESC",
                             (day, account))
        return self._all("SELECT * FROM positions WHERE trading_day=? ORDER BY id DESC", (day,))

    def day_summary(self, limit=30):
        return self._all("SELECT trading_day, account, COUNT(*) AS positions, "
                         "ROUND(COALESCE(SUM(pnl),0),2) AS pnl, SUM(status='open') AS open "
                         "FROM positions GROUP BY trading_day, account ORDER BY trading_day DESC, account DESC "
                         "LIMIT ?", (limit,))
