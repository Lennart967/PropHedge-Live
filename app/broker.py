import asyncio
import itertools
import json
import math
import multiprocessing as mp
import random
import sqlite3
import threading
import time


class MT5Handlers:
    def __init__(self, mt5, magic, deviation):
        self.mt5 = mt5
        self.magic = magic
        self.deviation = deviation

    def account(self):
        a = self.mt5.account_info()
        if a is None:
            raise RuntimeError(f"account_info: {self.mt5.last_error()}")
        return {"login": a.login, "server": a.server, "currency": a.currency, "balance": a.balance,
                "equity": a.equity, "margin": a.margin, "margin_free": a.margin_free, "margin_level": a.margin_level,
                "margin_so_mode": a.margin_so_mode, "margin_so_call": a.margin_so_call,
                "margin_so_so": a.margin_so_so, "leverage": a.leverage}

    def calc_margin(self, symbol, side, volume, price):
        t = self.mt5.ORDER_TYPE_BUY if side == "buy" else self.mt5.ORDER_TYPE_SELL
        m = self.mt5.order_calc_margin(t, symbol, float(volume), float(price))
        if m is None:
            raise RuntimeError(f"order_calc_margin {symbol}: {self.mt5.last_error()}")
        return float(m)

    def symbol(self, symbol):
        if not self.mt5.symbol_select(symbol, True):
            raise RuntimeError(f"Symbol {symbol} nicht verfügbar")
        s = self.mt5.symbol_info(symbol)
        return {"name": s.name, "digits": s.digits, "point": s.point, "tick_size": s.trade_tick_size,
                "tick_value": s.trade_tick_value, "contract_size": s.trade_contract_size,
                "vol_min": s.volume_min, "vol_max": s.volume_max, "vol_step": s.volume_step,
                "stops_level": s.trade_stops_level, "filling_mode": s.filling_mode,
                "trade_mode": s.trade_mode, "description": s.description,
                "currency_base": s.currency_base, "currency_profit": s.currency_profit}

    def symbols(self):
        return [{"name": s.name, "description": s.description, "trade_mode": s.trade_mode}
                for s in (self.mt5.symbols_get() or [])]

    def tick(self, symbol):
        t = self.mt5.symbol_info_tick(symbol)
        if t is None:
            raise RuntimeError(f"Kein Tick für {symbol}: {self.mt5.last_error()}")
        return {"bid": t.bid, "ask": t.ask, "time": int(t.time)}

    def rates(self, symbol, timeframe, count):
        tf = getattr(self.mt5, "TIMEFRAME_" + timeframe)
        r = self.mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if r is None:
            raise RuntimeError(f"Keine Kerzen: {self.mt5.last_error()}")
        return [{"time": int(x["time"]), "open": float(x["open"]), "high": float(x["high"]),
                 "low": float(x["low"]), "close": float(x["close"])} for x in r]

    def _pos(self, p):
        return {"ticket": p.ticket, "symbol": p.symbol,
                "side": "buy" if p.type == self.mt5.POSITION_TYPE_BUY else "sell",
                "volume": p.volume, "price_open": p.price_open, "price_current": p.price_current,
                "sl": p.sl, "tp": p.tp, "profit": p.profit + p.swap, "swap": p.swap, "magic": p.magic,
                "time": int(p.time)}

    def positions(self):
        return [self._pos(p) for p in (self.mt5.positions_get() or [])]

    def _filling(self, symbol):
        fm = self.mt5.symbol_info(symbol).filling_mode
        if fm & 1:
            return self.mt5.ORDER_FILLING_FOK
        if fm & 2:
            return self.mt5.ORDER_FILLING_IOC
        return self.mt5.ORDER_FILLING_RETURN

    def _send(self, req):
        r = self.mt5.order_send(req)
        if r is None:
            return {"ok": False, "retcode": -1, "comment": str(self.mt5.last_error())}
        ok = r.retcode in (self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_PLACED,
                           self.mt5.TRADE_RETCODE_DONE_PARTIAL)
        return {"ok": ok, "retcode": r.retcode, "comment": r.comment, "order": r.order,
                "price": r.price, "volume": r.volume}

    def _find_position(self, order):
        for _ in range(40):
            ps = self.mt5.positions_get(ticket=order)
            if ps:
                return ps[0].ticket
            for p in self.mt5.positions_get() or []:
                if p.identifier == order:
                    return p.ticket
            time.sleep(0.05)
        return order

    def market_order(self, symbol, side, volume, sl, tp, comment):
        t = self.mt5.symbol_info_tick(symbol)
        buy = side == "buy"
        req = {"action": self.mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume),
               "type": self.mt5.ORDER_TYPE_BUY if buy else self.mt5.ORDER_TYPE_SELL,
               "price": t.ask if buy else t.bid, "sl": float(sl), "tp": float(tp),
               "deviation": self.deviation, "magic": self.magic, "comment": comment[:31],
               "type_time": self.mt5.ORDER_TIME_GTC, "type_filling": self._filling(symbol)}
        res = self._send(req)
        if res["ok"]:
            res["ticket"] = self._find_position(res["order"])
        return res

    def modify_sltp(self, ticket, sl, tp):
        ps = self.mt5.positions_get(ticket=ticket)
        if not ps:
            return {"ok": False, "comment": "Position nicht gefunden"}
        return self._send({"action": self.mt5.TRADE_ACTION_SLTP, "position": ticket, "symbol": ps[0].symbol,
                           "sl": float(sl), "tp": float(tp), "magic": ps[0].magic})

    def close_position(self, ticket):
        ps = self.mt5.positions_get(ticket=ticket)
        if not ps:
            return {"ok": True, "comment": "bereits geschlossen"}
        p = ps[0]
        t = self.mt5.symbol_info_tick(p.symbol)
        buy_pos = p.type == self.mt5.POSITION_TYPE_BUY
        req = {"action": self.mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume, "position": ticket,
               "type": self.mt5.ORDER_TYPE_SELL if buy_pos else self.mt5.ORDER_TYPE_BUY,
               "price": t.bid if buy_pos else t.ask, "deviation": self.deviation, "magic": p.magic,
               "comment": "PropHedge close", "type_time": self.mt5.ORDER_TIME_GTC,
               "type_filling": self._filling(p.symbol)}
        res = self._send(req)
        res["ticket"] = ticket
        return res

    def close_all(self):
        out = [self.close_position(p.ticket) for p in (self.mt5.positions_get() or [])]
        for o in self.mt5.orders_get() or []:
            r = self._send({"action": self.mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
            r["ticket"] = o.ticket
            out.append(r)
        return out

    def _reason(self, code):
        names = {getattr(self.mt5, "DEAL_REASON_SL", 4): "SL", getattr(self.mt5, "DEAL_REASON_TP", 5): "TP",
                 getattr(self.mt5, "DEAL_REASON_SO", 6): "Stop Out"}
        return names.get(code, "manuell")

    def position_result(self, ticket):
        deals = self.mt5.history_deals_get(position=ticket) or []
        outs = [d for d in deals if d.entry != self.mt5.DEAL_ENTRY_IN]
        if not outs or self.mt5.positions_get(ticket=ticket):
            return {"closed": False}
        pnl = sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in deals)
        last = outs[-1]
        return {"closed": True, "pnl": float(pnl), "close_price": last.price, "close_time": int(last.time),
                "reason": self._reason(last.reason),
                "commission": float(sum(d.commission + getattr(d, "fee", 0.0) for d in deals)),
                "volume": float(sum(d.volume for d in deals if d.entry == self.mt5.DEAL_ENTRY_IN))}

    def entry_commission(self, ticket):
        for _ in range(20):
            ins = [d for d in (self.mt5.history_deals_get(position=ticket) or []) if d.entry == self.mt5.DEAL_ENTRY_IN]
            if ins:
                return {"found": True, "volume": float(sum(d.volume for d in ins)),
                        "commission": float(sum(d.commission + getattr(d, "fee", 0.0) for d in ins))}
            time.sleep(0.1)
        return {"found": False}

    def deals_since(self, ts_from):
        """Alle Buchungen seit ts_from."""
        deals = self.mt5.history_deals_get(int(ts_from), int(time.time()) + 3 * 86400) or []
        return [{"time": int(d.time), "amount": float(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0))}
                for d in deals]

    def realized_since(self, ts_from):
        deals = self.mt5.history_deals_get(int(ts_from), int(ts_from) + 3 * 86400) or []
        tot = 0.0
        for d in deals:
            if d.type in (self.mt5.DEAL_TYPE_BUY, self.mt5.DEAL_TYPE_SELL):
                tot += d.profit + d.commission + d.swap + getattr(d, "fee", 0.0)
        return float(tot)


def _worker(conn, cfg, deviation):
    try:
        import MetaTrader5 as mt5
    except ImportError as e:
        conn.send((None, False, f"MetaTrader5-Paket fehlt: {e}"))
        return
    kw = {"timeout": 60000, "portable": bool(cfg.get("portable", False))}
    if cfg.get("password"):
        kw.update(login=int(cfg["login"]), password=cfg["password"], server=cfg["server"])
    # ohne Passwort ans eingeloggte Terminal hängen
    ok = mt5.initialize(cfg["terminal_path"], **kw)
    if not ok:
        conn.send((None, False, str(mt5.last_error())))
        return
    info = mt5.account_info()
    if info is None or info.login != int(cfg["login"]):
        conn.send((None, False, f"Terminal ist mit falschem Konto verbunden ({info.login if info else '?'})"))
        mt5.shutdown()
        return
    conn.send((None, True, None))
    h = MT5Handlers(mt5, int(cfg.get("magic", 0)), deviation)
    while True:
        try:
            msg = conn.recv()
        except EOFError:
            break
        if msg is None:
            break
        rid, method, kw = msg
        try:
            conn.send((rid, True, getattr(h, method)(**kw)))
        except Exception as e:
            conn.send((rid, False, f"{type(e).__name__}: {e}"))
    mt5.shutdown()


class BrokerBase:
    async def acall(self, method, **kw):
        return await asyncio.to_thread(self.call, method, **kw)


class MT5Broker(BrokerBase):
    def __init__(self, name, cfg, deviation):
        self.name = name
        ctx = mp.get_context("spawn")
        self.conn, child = ctx.Pipe()
        self.proc = ctx.Process(target=_worker, args=(child, cfg, deviation), daemon=True)
        self.proc.start()
        _, ok, err = self.conn.recv()
        if not ok:
            raise RuntimeError(f"{name}: MT5-Verbindung fehlgeschlagen: {err}")
        self.lock = threading.Lock()
        self.ids = itertools.count()

    def call(self, method, **kw):
        with self.lock:
            rid = next(self.ids)
            self.conn.send((rid, method, kw))
            _, ok, res = self.conn.recv()
        if not ok:
            raise RuntimeError(f"{self.name}.{method}: {res}")
        return res

    def close(self):
        try:
            self.conn.send(None)
            self.proc.join(3)
        except Exception:
            pass


class MockMarket:
    def __init__(self, price, vol):
        self.lock = threading.Lock()
        self.price = price
        self.vol = vol
        now = int(time.time())
        start = now - now % 60 - 60 * 2000
        hist = []
        p = price
        for i in range(2000):
            o = p
            path = [o]
            for _ in range(12):
                p += random.gauss(0, vol * math.sqrt(5))
                path.append(p)
            hist.append({"time": start + i * 60, "open": o, "high": max(path), "low": min(path), "close": p})
        shift = price - p
        for b in hist:
            for k in ("open", "high", "low", "close"):
                b[k] += shift
        self.bars = hist
        self.last = time.time()

    def step(self):
        with self.lock:
            now = time.time()
            n = int((now - self.last) / 0.2)
            if n > 0:
                for _ in range(min(n, 3000)):
                    self.price += random.gauss(0, self.vol * math.sqrt(0.2))
                self.last += n * 0.2
                t = int(now) // 60 * 60
                b = self.bars[-1]
                if b["time"] == t:
                    b["high"] = max(b["high"], self.price)
                    b["low"] = min(b["low"], self.price)
                    b["close"] = self.price
                else:
                    self.bars.append({"time": t, "open": b["close"], "high": max(b["close"], self.price),
                                      "low": min(b["close"], self.price), "close": self.price})
            return self.price


class MockStore:
    """Mock-Zustand in der DB."""

    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
        self.conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.lock = threading.Lock()

    def load(self, key):
        with self.lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, key, value):
        with self.lock:
            self.conn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))


class MockBroker(BrokerBase):
    def __init__(self, name, market, cfg, spread, offset, balance, store=None):
        self.name = name
        self.market = market
        self.symbol_name = cfg["symbol"]
        self.magic = int(cfg.get("magic", 0))
        self.spread = spread
        self.offset = offset
        self.balance = balance
        self.positions_ = {}
        self.closed = {}
        self.deals = []
        self.next_ticket = 1000 if name == "prop" else 5000
        self.lock = threading.RLock()
        self.store = store
        self.key = f"mock_state_{name}"
        self.last_save = 0.0
        saved = store.load(self.key) if store else None
        if saved:
            self.balance = saved["balance"]
            self.deals = saved["deals"]
            self.positions_ = {int(k): v for k, v in saved["positions"].items()}
            self.closed = {int(k): v for k, v in saved["closed"].items()}
            self.next_ticket = saved["next_ticket"]

    def _save(self):
        if not self.store:
            return
        cutoff = time.time() - 7 * 86400
        self.deals = [d for d in self.deals if d["time"] >= cutoff]
        if len(self.closed) > 500:
            self.closed = dict(list(self.closed.items())[-500:])
        self.store.save(self.key, {"balance": self.balance, "deals": self.deals, "positions": self.positions_,
                                   "closed": self.closed, "next_ticket": self.next_ticket})
        self.store.save("mock_market_price", self.market.price)
        self.last_save = time.time()

    def call(self, method, **kw):
        with self.lock:
            self._update()
            res = getattr(self, "_" + method)(**kw)
            if time.time() - self.last_save > 5:
                self._save()
            return res

    def close(self):
        pass

    def _quote(self):
        mid = self.market.step() + self.offset
        return round(mid - self.spread / 2, 2), round(mid + self.spread / 2, 2)

    def _pnl(self, p, price):
        d = 1 if p["side"] == "buy" else -1
        return round((price - p["price_open"]) * d * p["volume"], 2)

    def _finish(self, ticket, price, reason):
        p = self.positions_.pop(ticket)
        pnl = self._pnl(p, price)
        self.balance += pnl
        now = int(time.time())
        self.deals.append({"time": now, "pnl": pnl})
        self.closed[ticket] = {"closed": True, "pnl": pnl, "close_price": price, "close_time": now, "reason": reason}
        self._save()

    def _update(self):
        bid, ask = self._quote()
        for t, p in list(self.positions_.items()):
            px = bid if p["side"] == "buy" else ask
            p["price_current"] = px
            d = 1 if p["side"] == "buy" else -1
            if p["sl"] and (px - p["sl"]) * d <= 0:
                self._finish(t, p["sl"], "SL")
            elif p["tp"] and (p["tp"] - px) * d <= 0:
                self._finish(t, p["tp"], "TP")
        self.bid, self.ask = bid, ask

    def _account(self):
        fl = sum(self._pnl(p, p["price_current"]) for p in self.positions_.values())
        used = sum(p["volume"] * p["price_open"] / 20 for p in self.positions_.values())
        eq = self.balance + fl
        return {"login": 1 if self.name == "prop" else 2, "server": f"Mock-{self.name}", "currency": "USD",
                "balance": round(self.balance, 2), "equity": round(eq, 2), "margin": round(used, 2),
                "margin_free": round(eq - used, 2), "margin_level": eq / used * 100 if used else 0.0,
                "margin_so_mode": 0, "margin_so_call": 100.0, "margin_so_so": 50.0, "leverage": 20}

    def _calc_margin(self, symbol, side, volume, price):
        return float(volume) * float(price) / 20

    def _symbol(self, symbol):
        return {"name": symbol, "digits": 2, "point": 0.01, "tick_size": 0.01, "tick_value": 0.01,
                "contract_size": 1.0, "vol_min": 0.01, "vol_max": 100.0, "vol_step": 0.01,
                "stops_level": 0, "filling_mode": 1, "trade_mode": 4, "description": f"Mock {symbol}",
                "currency_base": "USD", "currency_profit": "USD"}

    def _symbols(self):
        return [{"name": self.symbol_name, "description": f"Mock {self.symbol_name}", "trade_mode": 4}]

    def _tick(self, symbol):
        return {"bid": self.bid, "ask": self.ask, "time": int(time.time())}

    def _rates(self, symbol, timeframe, count):
        sec = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}[timeframe]
        out = []
        for b in self.market.bars:
            t = b["time"] // sec * sec
            if out and out[-1]["time"] == t:
                o = out[-1]
                o["high"] = max(o["high"], b["high"])
                o["low"] = min(o["low"], b["low"])
                o["close"] = b["close"]
            else:
                out.append(dict(b, time=t))
        return [{k: (round(v + self.offset, 2) if k != "time" else v) for k, v in b.items()} for b in out[-count:]]

    def _positions(self):
        return [{**p, "profit": self._pnl(p, p["price_current"]), "swap": 0.0} for p in self.positions_.values()]

    def _market_order(self, symbol, side, volume, sl, tp, comment):
        px = self.ask if side == "buy" else self.bid
        px = round(px + random.choice([0, 0, 0.01, -0.01]), 2)
        t = self.next_ticket
        self.next_ticket += 1
        self.positions_[t] = {"ticket": t, "symbol": symbol, "side": side, "volume": float(volume),
                              "price_open": px, "price_current": px, "sl": float(sl), "tp": float(tp),
                              "magic": self.magic, "time": int(time.time())}
        self._save()
        return {"ok": True, "retcode": 10009, "comment": "done", "order": t, "price": px,
                "volume": float(volume), "ticket": t}

    def _modify_sltp(self, ticket, sl, tp):
        if ticket not in self.positions_:
            return {"ok": False, "comment": "Position nicht gefunden"}
        self.positions_[ticket].update(sl=float(sl), tp=float(tp))
        self._save()
        return {"ok": True, "retcode": 10009, "comment": "done"}

    def _close_position(self, ticket):
        p = self.positions_.get(ticket)
        if not p:
            return {"ok": True, "comment": "bereits geschlossen", "ticket": ticket}
        self._finish(ticket, self.bid if p["side"] == "buy" else self.ask, "manuell")
        return {"ok": True, "retcode": 10009, "comment": "done", "ticket": ticket}

    def _close_all(self):
        return [self._close_position(t) for t in list(self.positions_)]

    def _position_result(self, ticket):
        return self.closed.get(ticket, {"closed": False})

    def _entry_commission(self, ticket):
        return {"found": False}

    def _deals_since(self, ts_from):
        return [{"time": d["time"], "amount": d["pnl"]} for d in self.deals if d["time"] >= ts_from]

    def _realized_since(self, ts_from):
        return round(sum(d["pnl"] for d in self.deals if d["time"] >= ts_from), 2)

    def _set_account(self, ts_from, balance=None, realized_today=None):
        if balance is not None:
            self.balance = float(balance)
        if realized_today is not None:
            delta = round(float(realized_today) - self._realized_since(ts_from), 2)
            if delta:
                self.deals.append({"time": int(time.time()), "pnl": delta, "adjust": True})
        self._save()
        return self._account()


def create_brokers(cfg):
    if cfg.get("mode") == "mock":
        m = cfg.get("mock", {})
        store = MockStore(cfg["db_path"])
        price = store.load("mock_market_price") or m.get("start_price", 20000.0)
        market = MockMarket(price, m.get("volatility", 2.0))
        prop = MockBroker("prop", market, cfg["prop"], 1.0, 0.0, m.get("prop_balance", 50000.0), store)
        live = MockBroker("live", market, cfg["live"], 1.6, 0.4, m.get("live_balance", 10000.0), store)
        return prop, live
    dev = int(cfg.get("deviation_points", 30))
    prop = MT5Broker("prop", cfg["prop"], dev)
    try:
        live = MT5Broker("live", cfg["live"], dev)
    except Exception:
        prop.close()
        raise
    return prop, live
