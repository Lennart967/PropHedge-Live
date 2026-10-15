import asyncio
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import risk
from .db import ts_iso

NY = ZoneInfo("America/New_York")
TRADE_MODES = {0: "gesperrt", 1: "nur Long", 2: "nur Short", 3: "nur Schließen", 4: "voll handelbar"}
# Gleiche Underlyings mit brokerabhängigen Namen
ALIASES = {
    "NAS100": {"NAS100", "US100", "USTEC", "USTECH", "USTECH100", "NDX100", "NASDAQ100", "NASDAQ", "NQ100", "NAS"},
    "US30": {"US30", "DJ30", "DJI30", "DOW30", "WS30", "USA30", "DOWJONES"},
    "US500": {"US500", "SPX500", "SP500", "USA500", "SPX"},
    "GER40": {"GER40", "DE40", "DAX40", "GER30", "DE30", "DAX", "GERMANY40"},
    "UK100": {"UK100", "FTSE100", "FTSE"},
    "XAUUSD": {"XAUUSD", "GOLD"},
    "XAGUSD": {"XAGUSD", "SILVER"},
    "BTCUSD": {"BTCUSD", "BITCOIN"},
}
SUFFIXES = ("CASH", "SPOT", "PRO", "ECN", "RAW", "STD", "ROLL", "MINI", "MICRO")


def sym_key(name):
    base = re.sub(r"[^A-Z0-9]", "", re.split(r"[._\-#+!]", name.upper())[0])
    for suf in SUFFIXES:
        if base.endswith(suf) and len(base) > len(suf) + 2:
            base = base[:-len(suf)]
    # m/r/c/i am Ende ist meist nur der Kontotyp
    cands = (base, base[:-1]) if len(base) > 4 and base[-1] in "MRCI" else (base,)
    for cand in cands:
        for canon, names in ALIASES.items():
            if cand in names:
                return canon
    return cands[-1] if len(base) == 7 else base


class RuleError(Exception):
    def __init__(self, reasons):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _norm(res):
    if isinstance(res, Exception):
        return {"ok": False, "retcode": None, "comment": str(res)}
    return res


class HedgeEngine:
    def __init__(self, cfg, prop, live, db):
        self.cfg = cfg
        self.prop = prop
        self.live = live
        self.db = db
        saved = db.get_setting("symbols") or {}
        self.sym_p = saved.get("prop") or cfg["prop"]["symbol"]
        self.sym_l = saved.get("live") or cfg["live"]["symbol"]
        self.sym_cache = {}
        if not self.assets():
            self.remember_asset()
        self.brokers = {"prop": prop, "live": live}
        self.lock = asyncio.Lock()
        self.specs = {}
        self.orphan_tries = {}
        self.entry_fees = {}
        self.fx_symbols = {}
        self.margin_cache = {}
        self.trail_cache = None
        self.atr_cache = None
        self.last_state = None

    async def spec(self, broker, symbol):
        key = (broker.name, symbol)
        if key not in self.specs:
            self.specs[key] = await broker.acall("symbol", symbol=symbol)
        return self.specs[key]

    @staticmethod
    def eod_status(rules, now=None):
        """Phase vor NY-Close bzw. Wochenende: open, block, close oder weekend."""
        now = now or datetime.now(NY)
        at = lambda day, hhmm: datetime(day.year, day.month, day.day, *map(int, hhmm.split(":")), tzinfo=NY)
        cands = []
        if rules["weekend_close_enabled"]:
            last_fri = now.date() - timedelta(days=(now.weekday() - 4) % 7)
            close = at(last_fri, rules["weekend_time"])
            reopen = at(last_fri + timedelta(days=2), "17:00")
            if close <= now < reopen:
                return {"enabled": True, "kind": "weekend", "phase": "weekend",
                        "minutes_left": round((reopen - now).total_seconds() / 60, 1),
                        "close_at": reopen.isoformat(timespec="minutes"), "time": "So 17:00",
                        "close_min": rules["weekend_close_min"], "block_min": rules["weekend_block_min"]}
            if now >= close:
                close += timedelta(days=7)
            cands.append(("weekend", close, rules["weekend_time"], rules["weekend_close_min"],
                          rules["weekend_block_min"]))
        if rules["eod_enabled"]:
            close = at(now.date(), rules["eod_time"])
            if now >= close:
                close += timedelta(days=1)
            while close.weekday() >= 5:  # Sa/So gibt es keinen Session-Schluss
                close += timedelta(days=1)
            cands.append(("daily", close, rules["eod_time"], rules["eod_close_min"], rules["eod_block_min"]))
        if not cands:
            return {"enabled": False, "phase": "open"}
        rank = {"close": 2, "block": 1, "open": 0}
        out = []
        for kind, close, hhmm, cmin, bmin in cands:
            left = (close - now).total_seconds() / 60
            phase = "close" if left <= cmin else "block" if left <= bmin else "open"
            out.append({"enabled": True, "kind": kind, "phase": phase, "minutes_left": round(left, 1),
                        "close_at": close.isoformat(timespec="minutes"), "time": hhmm,
                        "close_min": cmin, "block_min": bmin})
        return max(out, key=lambda e: (rank[e["phase"]], -e["minutes_left"]))

    @staticmethod
    def trading_day(server_time, reset_hour):
        start = server_time - ((server_time - reset_hour * 3600) % 86400)
        return start, datetime.fromtimestamp(start + 86399, timezone.utc).date().isoformat()

    async def atr_distance(self, rules, spec):
        """SL/TP-Abstand = ATR × Faktor."""
        tf, n = rules["atr_timeframe"], int(rules["atr_period"])
        key = (self.sym_p, tf, n)
        if self.atr_cache and self.atr_cache[0] == key and time.time() - self.atr_cache[1] < 60:
            value = self.atr_cache[2]
        else:
            candles = await self.prop.acall("rates", symbol=self.sym_p, timeframe=tf, count=n + 2)
            value = risk.atr(candles, n)
            self.atr_cache = (key, time.time(), value)
        if not value:
            return None
        step = spec.get("tick_size") or spec["point"]
        dist = round(max(round(value * rules["atr_mult"] / step), 1) * step, spec["digits"])
        return {"atr": value, "distance": dist, "timeframe": tf, "period": n, "mult": rules["atr_mult"]}

    async def state(self):
        stored = self.db.get_rules()
        rules = dict(stored)
        acc_p, acc_l, tick_p, tick_l, pos_p, pos_l, spec_p, spec_l = await asyncio.gather(
            self.prop.acall("account"), self.live.acall("account"),
            self.prop.acall("tick", symbol=self.sym_p), self.live.acall("tick", symbol=self.sym_l),
            self.prop.acall("positions"), self.live.acall("positions"),
            self.spec(self.prop, self.sym_p), self.spec(self.live, self.sym_l))
        atr_info, atr_error = None, None
        try:
            atr_info = await self.atr_distance(rules, spec_p)
        except Exception as e:
            atr_error = str(e)
        if rules["atr_enabled"] and atr_info:
            rules["sl_distance"] = atr_info["distance"]  # nur für die Berechnung
        day_start, day = self.trading_day(tick_p["time"], int(rules["reset_hour"]))
        realized, realized_l = await asyncio.gather(self.prop.acall("realized_since", ts_from=day_start),
                                                    self.live.acall("realized_since", ts_from=day_start))
        # Live in Prop-Währung
        fx = await self.fx_rate(acc_l["currency"], acc_p["currency"])
        fxv = fx or 1.0
        pairs, unmanaged = self.pairs(pos_p, pos_l)
        await self._load_entry_fees(pairs)
        fees = {acc: {k[1]: v for k, v in self.entry_fees.items() if k[0] == acc and v is not None}
                for acc in ("prop", "live")}
        specs = {p["symbol"]: await self.spec(self.prop, p["symbol"]) for p in pos_p}
        specs_l = {p["symbol"]: await self.spec(self.live, p["symbol"]) for p in pos_l}
        exp = risk.open_exposure(pos_p, specs, rules["buffer_pct"], rules["commission_per_lot"], fees["prop"])
        exp_l = risk.open_exposure(pos_l, specs_l, 0.0, rules["commission_live_per_lot"], fees["live"])
        ml_level, ml_info = await self.max_loss_level(rules, acc_p, day_start)
        rm = risk.rooms(rules, realized, exp, acc_p["balance"], ml_level)
        rm["max_loss_mode"], rm["max_loss_info"] = rules["max_loss_mode"], ml_info
        mid = (tick_p["bid"] + tick_p["ask"]) / 2
        mpl_p, mpl_l = await asyncio.gather(self.margin_per_lot(self.prop, self.sym_p, tick_p),
                                            self.margin_per_lot(self.live, self.sym_l, tick_l))
        cost = risk.costs(rules, spec_p, spec_l, tick_p["ask"] - tick_p["bid"], tick_l["ask"] - tick_l["bid"])
        track = self.ratio_track(rules, day, fxv)

        # Deckel kurz vor dem Profit-Ziel
        tl = risk.target_lots(rules, spec_p, cost, rm["target_left"])
        target_cap = {"Profit-Ziel": tl} if tl is not None else {}

        def sized(extra=None):
            sz = risk.position_size(rules, spec_p, mid, rm, acc_p["margin_free"], mpl_p, {**target_cap, **(extra or {})})
            return (sz, *risk.hedge_volume(sz["lots"], spec_p, spec_l, rules, cost, track, fxv))

        use = rules["margin_max_use_pct"]

        def margins(sz, hl):
            # schlechtester Fall: alles am Stop
            worst_p = sz["sl_usd"] + sz["commission_usd"] + exp["risk"]
            worst_l = hl * (risk.vpu(spec_l) * cost["live_loss"] + rules["commission_live_per_lot"]) + exp_l["risk"]
            return {"prop": risk.margin_check(acc_p, sz["lots"] * mpl_p, worst_p),
                    "live": risk.margin_check(acc_l, hl * mpl_l, worst_l)}

        def failing(m):
            for acc in ("live", "prop"):
                x = m[acc]
                if x["required"] > 0 and (not x["ok"] or x["usage_pct"] is None or x["usage_pct"] > use + 1e-6):
                    return acc
            return None

        size, hedge_lots, hedge_base = sized()
        margin = margins(size, hedge_lots)
        bad = failing(margin) if size["lots"] > 0 else None
        if bad:
            # größte Position, die auf beiden Konten passt
            label = "Margin / Stop-Out " + ("Live-Konto" if bad == "live" else "Prop-Konto")
            lo, hi, best = 0.0, size["lots"], None
            while hi - lo > spec_p["vol_step"] / 2:
                cap = (lo + hi) / 2
                sz, hl, hb = sized({label: cap})
                mm = margins(sz, hl)
                if sz["lots"] > 0 and not failing(mm):
                    lo, best = cap, (sz, hl, hb, mm)
                else:
                    hi = cap
            if best:
                size, hedge_lots, hedge_base, margin = best
            else:
                size, hedge_lots, hedge_base = sized({label: 0.0})
                margin = margins(size, hedge_lots)
        if rules.get("align_lots", True) and rules.get("hedge_mode", "fee") == "fee" and size["lots"] > 0:
            # Lot-Abgleich
            al = risk.align_lots(size["lots"], spec_p, spec_l, rules["hedge_ratio"], fxv)
            if al and al[0] < size["lots"] - 1e-12:
                f = al[0] / size["lots"]
                size = {**size, **{k: size[k] * f for k in ("risk_usd", "reward_usd", "sl_usd", "commission_usd",
                                                               "margin", "notional")},
                        "lots": al[0], "limited_by": size["limited_by"] + " + Lot-Abgleich"}
                margin = margins(size, al[1])
            if al:
                hedge_lots = hedge_base = al[1]
        track["base_lots"] = hedge_base

        def variant(dist):
            """Größe und Kosten bei Abstand dist, ohne Margin-Suche."""
            r2 = {**rules, "sl_distance": dist}
            c2 = risk.costs(r2, spec_p, spec_l, tick_p["ask"] - tick_p["bid"], tick_l["ask"] - tick_l["bid"])
            sz = risk.position_size(r2, spec_p, mid, rm, acc_p["margin_free"], mpl_p)
            hl, _ = risk.hedge_volume(sz["lots"], spec_p, spec_l, r2, c2, track, fxv)
            f = risk.trade_fees(sz["lots"], hl, spec_p, spec_l, r2, c2, fxv)
            return {"distance": dist, "lots": sz["lots"], "hedge_lots": hl, "limited_by": sz["limited_by"],
                    "risk_usd": sz["risk_usd"], "fee_prop": f["prop"], "fee_live": f["live"],
                    "cost_you": f["live"] + rules["hedge_ratio"] * f["prop"], "cost_pct": c2["pct"]}

        compare = {"fixed": variant(stored["sl_distance"]),
                   "atr": variant(atr_info["distance"]) if atr_info else None}
        margin["prop"]["per_lot"], margin["live"]["per_lot"] = mpl_p, mpl_l
        viol = risk.violations(rules, rm, size, exp, spec_p, hedge_lots, cost)
        for acc, label in (("prop", "Prop"), ("live", "Live")):
            m = margin[acc]
            if m["required"] > 0 and m["usage_pct"] is not None and m["usage_pct"] > use + 1e-6:
                viol.append(f"{label}-Konto: Margin reicht nicht ({m['required']:.2f} benötigt, "
                            f"max. {use:g} % der freien Margin {m['free']:.2f}).")
            if m["required"] > 0 and not m["ok"]:
                viol.append(f"{label}-Konto würde im schlechtesten Fall unter das Margin-Call-/Stop-Out-Level "
                            f"fallen ({m['threshold']:g}{'' if m['threshold_money'] else ' %'}). Position kleiner wählen.")
        if pos_p or pos_l or self.db.open_trades():
            n_p, n_l = len(pos_p), len(pos_l)
            viol.insert(0, f"Es ist schon eine Position offen (Prop {n_p}, Live {n_l}). Nur ein Hedge gleichzeitig: "
                           "erst schließen, dann neu öffnen.")
        if rules["target_cap_enabled"] and rm["pnl_total"] >= rules["profit_target"]:
            viol.insert(0, f"Profit-Ziel erreicht ({rm['pnl_total']:.2f} von {rules['profit_target']:g}). "
                           "Keine weiteren Trades nötig.")
        if rules["atr_enabled"] and not atr_info:
            viol.insert(0, f"ATR nicht verfügbar ({atr_error or 'zu wenige Kerzen'}), kein SL/TP-Abstand.")
        if fx is None:
            viol.insert(0, f"Kein Umrechnungskurs {acc_l['currency']} → {acc_p['currency']} gefunden.")
        mid_l = (tick_l["bid"] + tick_l["ask"]) / 2
        dev = abs(mid - mid_l) / mid * 100 if mid > 0 and mid_l > 0 else None
        if dev is None:
            viol.insert(0, "Keine Kurse auf einem der Konten (Markt geschlossen?).")
        elif dev > rules["max_price_dev_pct"]:
            viol.insert(0, f"Preise Prop/Live weichen um {dev:.2f} % ab (erlaubt {rules['max_price_dev_pct']:g} %). "
                           "Prüfen, ob beide Symbole dasselbe Asset sind.")
        eod = self.eod_status(rules)
        if eod["phase"] == "weekend":
            viol.insert(0, f"Wochenende: Markt geschlossen bis Sonntag 17:00 New York "
                           f"(in {eod['minutes_left'] / 60:.1f} h).")
        elif eod["phase"] != "open" and eod["kind"] == "weekend":
            viol.insert(0, f"Neue Hedges gesperrt: Wochenend-Close Freitag {eod['time']} New York "
                           f"(in {eod['minutes_left']:.0f} min), keine Positionen übers Wochenende.")
        elif eod["phase"] != "open":
            viol.insert(0, f"Neue Hedges gesperrt: NY-Session schließt um {eod['time']} New York "
                           f"(in {eod['minutes_left']:.0f} min), Overnight-Fees vermeiden.")
        d = rules["sl_distance"]
        preview = {s: dict(zip(("sl", "tp"), risk.bracket(s, tick_p["ask"] if s == "buy" else tick_p["bid"],
                                                          d, spec_p["digits"])),
                           entry=tick_p["ask"] if s == "buy" else tick_p["bid"]) for s in ("buy", "sell")}
        ratios = self.hedge_ratios(pairs, track, rules, size["lots"], hedge_lots, spec_p, spec_l, cost, fxv)
        scen = self.scenarios(pairs, rules, size, hedge_lots, spec_p, spec_l, acc_p, acc_l, realized, realized_l,
                              cost, fxv)
        for row in scen["rows"]:
            # realisierte Ratio, falls das Szenario eintritt
            rp = ratios["realized_prop"] + row["prop_realized"]
            rl = ratios["realized_live"] + row["live_realized"] * fxv
            row["ratio"] = -rl / rp if abs(rp) >= 1 else None
        return {"day": day, "server_time": tick_p["time"], "rules": stored, "calc_rules": rules, "atr": atr_info, "compare": compare,
                "rooms": rm, "size": size,
                "ratios": ratios, "scenarios": scen, "costs": cost, "margin": margin, "fx": fx,
                "hedge_lots": hedge_lots, "violations": viol, "pairs": pairs, "unmanaged": unmanaged,
                "preview": preview, "mode": self.cfg.get("mode", "mt5"),
                "day_start": day_start, "eod": eod, "price_dev_pct": dev, "assets": self.assets(),
                "prop": {"symbol": self.sym_p, "tick": tick_p, "account": acc_p, "digits": spec_p["digits"],
                         "realized": realized, "currency": acc_p["currency"]},
                "live": {"symbol": self.sym_l, "tick": tick_l, "account": acc_l, "digits": spec_l["digits"],
                         "realized": realized_l, "currency": acc_l["currency"]}}

    async def max_loss_level(self, rules, acc_p, day_start):
        """Max-Loss-Grenze als Kontostand, statisch oder EOD trailing."""
        init = rules["initial_balance"]
        static = init - rules["max_loss"]
        if rules["max_loss_mode"] != "eod_trailing":
            return static, None
        pct = rules["max_loss"] / init
        start = int(self.db.get_setting("challenge_start_ts") or 0)
        key = (day_start, start, pct, rules["trail_lock"], init, rules["reset_hour"])
        if self.trail_cache and self.trail_cache[0] == key and time.time() - self.trail_cache[1] < 300:
            return self.trail_cache[2]
        deals = sorted(await self.prop.acall("deals_since", ts_from=start), key=lambda d: d["time"])
        first = max(start, deals[0]["time"]) if deals else day_start
        level, high, days = static, None, 0
        after = 0.0  # Buchungen ab der Tagesgrenze
        i = len(deals) - 1
        b = day_start
        while b > first and days < 1000:
            while i >= 0 and deals[i]["time"] >= b:
                after += deals[i]["amount"]
                i -= 1
            eod = acc_p["balance"] - after  # Tagesend-Balance
            high = eod if high is None else max(high, eod)
            new = eod * (1 - pct)
            if rules["trail_lock"]:
                new = min(new, init)
            level = max(level, new)
            days += 1
            b -= 86400
        out = (level, {"eod_high": high, "days": days, "static": static})
        self.trail_cache = (key, time.time(), out)
        return out

    async def fx_rate(self, frm, to):
        """Kurs von frm in to, z. B. EUR → USD."""
        if frm == to:
            return 1.0
        for br in (self.live, self.prop):
            for pair, inverse in ((frm + to, False), (to + frm, True)):
                name = await self._fx_symbol(br, pair)
                if not name:
                    continue
                try:
                    await self.spec(br, name)
                    t = await br.acall("tick", symbol=name)
                except Exception:
                    continue
                m = (t["bid"] + t["ask"]) / 2
                if m > 0:
                    return 1 / m if inverse else m
        return None

    async def _fx_symbol(self, br, pair):
        key = (br.name, pair)
        if key not in self.fx_symbols:
            names = [s["name"] for s in await self.symbol_list(br.name)
                     if re.sub(r"[^A-Z]", "", s["name"].upper()).startswith(pair) and s["trade_mode"] != 0]
            names.sort(key=len)
            self.fx_symbols[key] = names[0] if names else ""
        return self.fx_symbols[key]

    async def margin_per_lot(self, br, symbol, tick):
        """Margin für 1 Lot laut Broker, kurz gecacht."""
        key = (br.name, symbol)
        c = self.margin_cache.get(key)
        if c and time.time() - c[0] < 10:
            return c[1]
        m = max(await br.acall("calc_margin", symbol=symbol, side="buy", volume=1.0, price=tick["ask"]),
                await br.acall("calc_margin", symbol=symbol, side="sell", volume=1.0, price=tick["bid"]))
        self.margin_cache[key] = (time.time(), m)
        return m

    def _vpu(self, acc, symbol):
        s = self.specs.get((acc, symbol))
        return risk.vpu(s) if s else None

    def ratio_track(self, rules, day, fx=1.0):
        """Geschlossene Hedges im Ausgleichs-Zeitraum."""
        if rules["ratio_scope"] == "day":
            # Hedges vor dem Reset zählen nicht
            closed, since = self.db.closed_pnl_by_account(day, self.db.get_setting("ratio_since")), None
        else:
            since = self.db.get_setting("ratio_since")
            closed = self.db.closed_pnl_since(since)
        # Live in Prop-Währung (aktueller Kurs)
        p, l = closed.get("prop", 0.0), closed.get("live", 0.0) * fx
        return {"scope": rules["ratio_scope"], "since": since, "prop": p, "live": l,
                "deviation": l + rules["hedge_ratio"] * p, "ratio": -l / p if abs(p) >= 1 else None}

    def hedge_ratios(self, pairs, track, rules, lots, hedge_lots, spec_p, spec_l, cost, fx=1.0):
        """Ratio geplant, gerundet, offen und realisiert."""
        conv = {"prop": 1.0, "live": fx}
        out = {"planned": rules["hedge_ratio"], "next": None, "open_volume": None, "open_pnl": None,
               "realized": None, "realized_prop": 0.0, "realized_live": 0.0}
        if lots > 0:
            o = risk.hedge_outcomes(lots, spec_p, spec_l, rules, cost, fx)
            prop_loss, live_gain = o["prop_loss"], hedge_lots * o["live_gain"]
            prop_gain, live_loss = o["prop_gain"], hedge_lots * o["live_loss"]
            out["next"] = live_gain / prop_loss
            out["next_tp"] = live_loss / prop_gain if prop_gain > 0 else None
            # reine Kursbewegung
            out["next_gross"] = hedge_lots * risk.vpu(spec_l) * fx / (lots * risk.vpu(spec_p))
            out["fees"] = risk.trade_fees(lots, hedge_lots, spec_p, spec_l, rules, cost, fx)
            # kumuliert nach SL bzw. TP
            pc, pt = track["prop"] - prop_loss, track["prop"] + prop_gain
            out["next_cum"] = -(track["live"] + live_gain) / pc if abs(pc) >= 1 else None
            out["next_dev"] = track["live"] + live_gain + rules["hedge_ratio"] * pc
            out["next_dev_tp"] = track["live"] - live_loss + rules["hedge_ratio"] * pt
        exp = {"prop": 0.0, "live": 0.0}
        pnl = {"prop": 0.0, "live": 0.0}
        for t in pairs:
            te = {"prop": 0.0, "live": 0.0}
            for l in t["legs"]:
                u = self._vpu(l["account"], l["symbol"])
                if l["live_open"] and u:
                    te[l["account"]] += l["volume"] * u * conv[l["account"]]
                    pnl[l["account"]] += l["profit"] * conv[l["account"]]
            t["ratio_actual"] = te["live"] / te["prop"] if te["prop"] else None
            exp["prop"] += te["prop"]
            exp["live"] += te["live"]
        if exp["prop"]:
            out["open_volume"] = exp["live"] / exp["prop"]
            if abs(pnl["prop"]) >= 1:
                out["open_pnl"] = -pnl["live"] / pnl["prop"]
        # geschlossene Hedges + offener P&L
        run = {"prop": track["prop"], "live": track["live"]}
        for t in pairs:
            for l in t["legs"]:
                if l["live_open"]:
                    fee = self.entry_fees.get((l["account"], l["ticket"])) or 0.0
                    run[l["account"]] += (l["profit"] + fee) * conv[l["account"]]
        out["running"] = -run["live"] / run["prop"] if abs(run["prop"]) >= 1 else None
        out["running_prop"], out["running_live"] = run["prop"], run["live"]
        out["realized_prop"], out["realized_live"] = track["prop"], track["live"]
        out["realized"] = track["ratio"]
        out["track"] = track
        return out

    async def _load_entry_fees(self, pairs):
        """Entry-Kommission je offenem Leg, gecacht."""
        for t in pairs:
            for l in t["legs"]:
                k = (l["account"], l["ticket"])
                if not l["live_open"] or k in self.entry_fees:
                    continue
                try:
                    c = await self.brokers[l["account"]].acall("entry_commission", ticket=l["ticket"])
                except Exception:
                    c = {"found": False}
                self.entry_fees[k] = c["commission"] if c.get("found") else None

    def scenarios(self, pairs, rules, size, hedge_lots, spec_p, spec_l, acc_p, acc_l, realized, realized_l, cost,
                  fx=1.0):
        """Kontostände, falls die offenen Hedges per Prop-SL bzw. -TP schließen (ohne Hedge: Vorschau)."""
        per_lot = {"prop": rules["commission_per_lot"], "live": rules["commission_live_per_lot"]}
        z = lambda: {"prop": 0.0, "live": 0.0}
        delta = {"prop_sl": z(), "prop_tp": z()}
        real = {"prop_sl": z(), "prop_tp": z()}
        basis = None
        for t in pairs:
            for l in t["legs"]:
                u = self._vpu(l["account"], l["symbol"])
                if not l["live_open"] or not u or not l["sl"] or not l["tp"]:
                    continue
                basis = "open"
                acc = l["account"]
                sgn = 1 if l["side"] == "buy" else -1
                at_sl = (l["sl"] - l["entry"]) * sgn * l["volume"] * u
                at_tp = (l["tp"] - l["entry"]) * sgn * l["volume"] * u
                known = self.entry_fees.get((acc, l["ticket"]))
                entry_fee = known if known is not None else -per_lot[acc] / 2 * l["volume"]
                exit_fee = risk.exit_fee(known, per_lot[acc], l["volume"])
                swap = l.get("swap", 0.0)
                # Prop-SL: Live am TP, und umgekehrt
                hit = {"prop_sl": at_sl, "prop_tp": at_tp} if acc == "prop" else {"prop_sl": at_tp, "prop_tp": at_sl}
                for key, gross in hit.items():
                    delta[key][acc] += gross + exit_fee + swap
                    real[key][acc] += gross + entry_fee + exit_fee + swap
        if basis is None:
            if size["lots"] <= 0:
                return {"basis": None, "rows": []}
            basis = "planned"
            gp = size["sl_usd"]
            ul = hedge_lots * risk.vpu(spec_l)
            fp, fl = size["commission_usd"], hedge_lots * per_lot["live"]
            # noch keine Position
            delta["prop_sl"] = {"prop": -gp - fp, "live": ul * cost["live_win"] - fl}
            cut = size["lots"] * risk.vpu(spec_p) * cost["prop_tp_cut"]  # ungespiegelt: Prop schließt vor dem TP
            delta["prop_tp"] = {"prop": gp - fp - cut, "live": -ul * cost["live_loss"] - fl}
            real = delta
        rows = []
        for key, label in (("prop_sl", "Prop-SL getroffen"), ("prop_tp", "Prop-TP getroffen")):
            dp, dl = delta[key]["prop"], delta[key]["live"]
            rows.append({"key": key, "label": label, "prop_delta": dp, "live_delta": dl, "net": dp + dl * fx,
                         "prop_realized": real[key]["prop"], "live_realized": real[key]["live"],
                         "prop_balance": acc_p["balance"] + dp, "live_balance": acc_l["balance"] + dl,
                         "prop_day_after": realized + dp, "live_day_after": realized_l + dl})
        return {"basis": basis, "rows": rows}

    def pairs(self, pos_p, pos_l):
        cur = {("prop", p["ticket"]): p for p in pos_p}
        cur.update({("live", p["ticket"]): p for p in pos_l})
        managed = set()
        out = []
        for t in self.db.open_trades():
            legs = []
            for leg in self.db.trade_positions(t["id"]):
                key = (leg["account"], leg["ticket"])
                managed.add(key)
                c = cur.get(key)
                legs.append({**leg, "live_open": c is not None,
                             "profit": c["profit"] if c else (leg["pnl"] or 0.0),
                             "price_current": c["price_current"] if c else leg["close_price"],
                             "sl": c["sl"] if c else leg["sl"], "tp": c["tp"] if c else leg["tp"],
                             "swap": c.get("swap", 0.0) if c else 0.0})
            out.append({**t, "legs": legs, "net": round(sum(l["profit"] for l in legs), 2)})
        unmanaged = [{**p, "account": k[0]} for k, p in cur.items() if k not in managed]
        return out, unmanaged

    async def symbol_list(self, acc):
        c = self.sym_cache.get(acc)
        if not c or time.time() - c[0] > 600:
            c = (time.time(), await self.brokers[acc].acall("symbols"))
            self.sym_cache[acc] = c
        return c[1]

    async def search_symbols(self, acc, q, limit=30):
        q = q.upper().strip()
        hits = [s for s in await self.symbol_list(acc)
                if q in s["name"].upper() or q in (s["description"] or "").upper()]
        hits.sort(key=lambda s: (s["trade_mode"] != 4, not s["name"].upper().startswith(q), len(s["name"])))
        return hits[:limit]

    async def match_symbol(self, prop_symbol):
        """Passende Live-Symbole zu einem Prop-Symbol."""
        k = sym_key(prop_symbol)
        hits = [s for s in await self.symbol_list("live") if s["name"] == prop_symbol or sym_key(s["name"]) == k]
        hits.sort(key=lambda s: (s["name"] != prop_symbol, s["trade_mode"] != 4, len(s["name"])))
        return hits[:10]

    async def _probe(self, broker, symbol):
        spec = await broker.acall("symbol", symbol=symbol)
        self.specs[(broker.name, symbol)] = spec
        tick = None
        for _ in range(6):
            tick = await broker.acall("tick", symbol=symbol)
            if tick["bid"] > 0 and tick["ask"] > 0:
                break
            await asyncio.sleep(0.5)
        return spec, tick

    async def check_symbols(self, sym_p, sym_l):
        rules = self.db.get_rules()
        errors, warnings, out = [], [], {}
        for acc, label, br, sym in (("prop", "Prop", self.prop, sym_p), ("live", "Live", self.live, sym_l)):
            try:
                spec, tick = await self._probe(br, sym)
            except Exception as e:
                errors.append(f"{label}: Symbol {sym} nicht verfügbar ({e}).")
                continue
            out[acc] = {"symbol": sym, "description": spec["description"], "trade_mode": spec["trade_mode"],
                        "trade_mode_txt": TRADE_MODES.get(spec["trade_mode"], str(spec["trade_mode"])),
                        "digits": spec["digits"], "contract_size": spec["contract_size"],
                        "vol_min": spec["vol_min"], "vol_step": spec["vol_step"],
                        "currency_base": spec["currency_base"], "currency_profit": spec["currency_profit"],
                        "value_per_point_lot": risk.vpu(spec), "bid": tick["bid"], "ask": tick["ask"]}
            if spec["trade_mode"] != 4:
                errors.append(f"{label}: {sym} ist nicht voll handelbar ({out[acc]['trade_mode_txt']}).")
            if tick["bid"] <= 0 or tick["ask"] <= 0:
                errors.append(f"{label}: keine Kurse für {sym} (Markt geschlossen?).")
        if "prop" in out and "live" in out:
            p, l = out["prop"], out["live"]
            if p["currency_profit"] != l["currency_profit"]:
                errors.append(f"Gewinnwährung unterschiedlich ({p['currency_profit']} vs. {l['currency_profit']}).")
            mp, ml = (p["bid"] + p["ask"]) / 2, (l["bid"] + l["ask"]) / 2
            if mp > 0 and ml > 0:
                dev = abs(mp - ml) / mp * 100
                out["deviation_pct"] = dev
                lim = rules["max_price_dev_pct"]
                if dev > lim:
                    errors.append(f"Preise weichen um {dev:.2f} % ab (erlaubt {lim:g} %). "
                                  "Das ist vermutlich nicht dasselbe Asset.")
                elif dev > lim / 4:
                    warnings.append(f"Preise weichen um {dev:.2f} % ab (z. B. Kassa vs. Future). Prüfen.")
            if sym_key(sym_p) != sym_key(sym_l):
                warnings.append("Die Symbolnamen deuten auf unterschiedliche Assets hin. Beschreibungen vergleichen.")
        return {"ok": not errors, "errors": errors, "warnings": warnings, **out}

    def assets(self):
        """Geprüfte Asset-Paare mit eigenem SL/TP-Abstand."""
        return self.db.get_setting("assets") or []

    def remember_asset(self):
        r = self.db.get_rules()
        lst = [a for a in self.assets() if (a["prop"], a["live"]) != (self.sym_p, self.sym_l)]
        lst.append({"prop": self.sym_p, "live": self.sym_l, "sl_distance": r["sl_distance"],
                    "commission_per_lot": r["commission_per_lot"],
                    "commission_live_per_lot": r["commission_live_per_lot"]})
        lst.sort(key=lambda a: (a["prop"].upper(), a["live"].upper()))
        self.db.set_setting("assets", lst)

    def remove_asset(self, sym_p, sym_l):
        if (sym_p, sym_l) == (self.sym_p, self.sym_l):
            raise RuleError(["Das aktive Asset kann nicht entfernt werden."])
        self.db.set_setting("assets", [a for a in self.assets() if (a["prop"], a["live"]) != (sym_p, sym_l)])

    async def set_symbols(self, sym_p, sym_l, sl_distance=None):
        if sl_distance is None:
            known = next((a for a in self.assets() if (a["prop"], a["live"]) == (sym_p, sym_l)), None)
            sl_distance = known["sl_distance"] if known else self.db.get_rules()["sl_distance"]
        known = next((a for a in self.assets() if (a["prop"], a["live"]) == (sym_p, sym_l)), None)
        async with self.lock:
            if self.db.open_trades():
                raise RuleError(["Erst alle offenen Hedges schließen, dann das Asset wechseln."])
            chk = await self.check_symbols(sym_p, sym_l)
            if not chk["ok"]:
                raise RuleError(chk["errors"])
            self.sym_p, self.sym_l = sym_p, sym_l
            self.db.set_setting("symbols", {"prop": sym_p, "live": sym_l})
            rules = self.db.get_rules()
            rules["sl_distance"] = sl_distance
            for k in ("commission_per_lot", "commission_live_per_lot"):
                if known and k in known:
                    rules[k] = known[k]
            self.db.set_rules(rules)
            self.remember_asset()
            self.db.log("symbols_changed", {"prop": sym_p, "live": sym_l, "sl_distance": sl_distance})
            return chk

    async def set_accounts(self, values):
        if self.cfg.get("mode") != "mock":
            raise RuleError(["Kontowerte kommen im MT5-Modus direkt vom Broker und können nicht gesetzt werden."])
        async with self.lock:
            st = await self.state()
            out = {}
            for acc, v in values.items():
                if v:
                    out[acc] = await self.brokers[acc].acall("set_account", ts_from=st["day_start"], **v)
            self.db.log("accounts_set", values)
            return out

    def choose_side(self):
        """Erster Hedge zufällig, zweiter Gegenrichtung, danach 50/50."""
        rules = self.db.get_rules()
        if rules["direction_mode"] == "alternate":
            sides = self.db.trade_sides_from_id(self.db.get_setting("challenge_trade_id"))
            if len(sides) == 1:
                return "sell" if sides[0] == "buy" else "buy"
        return secrets.choice(("buy", "sell"))

    async def enter(self, side=None):
        side = side or self.choose_side()
        async with self.lock:
            st = await self.state()
            if st["violations"]:
                self.db.log("entry_rejected", {"side": side, "reasons": st["violations"]})
                raise RuleError(st["violations"])
            r = st["calc_rules"]
            d = r["sl_distance"]
            sp = await self.spec(self.prop, self.sym_p)
            sl_spec = await self.spec(self.live, self.sym_l)
            lots, hl = st["size"]["lots"], st["hedge_lots"]
            lside = "sell" if side == "buy" else "buy"
            tp_, tl = st["prop"]["tick"], st["live"]["tick"]
            pe = tp_["ask"] if side == "buy" else tp_["bid"]
            le = tl["ask"] if lside == "buy" else tl["bid"]
            psl, ptp = risk.bracket(side, pe, d, sp["digits"])
            lsl, ltp = risk.bracket(lside, le, d, sl_spec["digits"])
            key = f"PH{int(time.time() * 1000) % 10 ** 9}"
            jobs = [self.prop.acall("market_order", symbol=self.sym_p, side=side, volume=lots, sl=psl, tp=ptp,
                                    comment=key)]
            if hl > 0:
                jobs.append(self.live.acall("market_order", symbol=self.sym_l, side=lside, volume=hl, sl=lsl,
                                            tp=ltp, comment=key))
            res = [_norm(x) for x in await asyncio.gather(*jobs, return_exceptions=True)]
            rp = res[0]
            rl = res[1] if hl > 0 else None
            if not rp["ok"] or (rl is not None and not rl["ok"]):
                undo = []
                if rp["ok"]:
                    undo.append(self.prop.acall("close_position", ticket=rp["ticket"]))
                if rl and rl["ok"]:
                    undo.append(self.live.acall("close_position", ticket=rl["ticket"]))
                undone = [_norm(x) for x in await asyncio.gather(*undo, return_exceptions=True)]
                reasons = []
                if not rp["ok"]:
                    reasons.append(f"Prop-Order abgelehnt: {rp.get('comment')} ({rp.get('retcode')})")
                if rl is not None and not rl["ok"]:
                    reasons.append(f"Live-Order abgelehnt: {rl.get('comment')} ({rl.get('retcode')})")
                if undo:
                    ok = all(u["ok"] for u in undone)
                    reasons.append("Gefüllte Gegenseite wurde wieder geschlossen." if ok else
                                   "ACHTUNG: Gefüllte Gegenseite konnte nicht geschlossen werden – manuell prüfen!")
                self.db.log("entry_failed", {"side": side, "prop": rp, "live": rl, "undo": undone})
                raise RuleError(reasons)
            opened_at = ts_iso(st["server_time"])
            legs = [("prop", self.prop, self.sym_p, side, rp, sp)]
            if rl:
                legs.append(("live", self.live, self.sym_l, lside, rl, sl_spec))
            rows, warnings = [], []
            for acc, br, sym, sd, rr, spec in legs:
                cur = next((p for p in await br.acall("positions") if p["ticket"] == rr["ticket"]), None)
                fill = cur["price_open"] if cur else (rr.get("price") or (pe if acc == "prop" else le))
                vol = cur["volume"] if cur else rr.get("volume") or (lots if acc == "prop" else hl)
                nsl, ntp = risk.bracket(sd, fill, d, spec["digits"])
                if acc == "live" and r["mirror_brackets"]:
                    # Live-SL = Prop-TP, Live-TP = Prop-SL
                    p_row = rows[0]
                    msl = round(p_row["tp"], spec["digits"])
                    mtp = round(p_row["sl"], spec["digits"])
                    valid = msl < fill < mtp if sd == "buy" else mtp < fill < msl
                    if valid:
                        nsl, ntp = msl, mtp
                    else:
                        warnings.append("Live: Spiegeln der Prop-SL/TP nicht möglich, ±Abstand um eigenen Fill gesetzt.")
                if cur and (abs(cur["sl"] - nsl) > spec["point"] / 2 or abs(cur["tp"] - ntp) > spec["point"] / 2):
                    try:
                        m = await br.acall("modify_sltp", ticket=rr["ticket"], sl=nsl, tp=ntp)
                    except Exception as e:
                        m = {"ok": False, "comment": str(e)}
                    if not m.get("ok"):
                        nsl, ntp = cur["sl"], cur["tp"]
                        warnings.append(f"{acc}: SL/TP konnte nicht auf Fill-Preis angepasst werden.")
                rows.append({"account": acc, "ticket": rr["ticket"], "symbol": sym, "side": sd, "volume": vol,
                             "entry": fill, "sl": nsl, "tp": ntp,
                             "risk_usd": round(vol * abs(fill - nsl) * risk.vpu(spec), 2)})
            info = []
            for acc, br, rr, rule_key in (("Prop", self.prop, rp, "commission_per_lot"),
                                          ("Live", self.live, rl, "commission_live_per_lot")):
                if not rr:
                    continue
                try:
                    c = await br.acall("entry_commission", ticket=rr["ticket"])
                    if c.get("found") and c["volume"] > 0:
                        self.entry_fees[(acc.lower(), rr["ticket"])] = c["commission"]
                        # Schätzung (Entry × 2), exakt erst beim Schließen
                        rt = round(-c["commission"] / c["volume"] * 2, 4)
                        if r[rule_key] == 0 and rt > 0:
                            self._set_commission(rule_key, rt)
                            info.append(f"Kommission {acc} geschätzt: {rt:.2f} pro Lot (Hin + Rück), "
                                        "wird nach dem Schließen exakt übernommen.")
                except Exception as e:
                    self.db.log("commission_error", {"account": acc, "error": str(e)})
            trade_id = self.db.add_trade(st["day"], opened_at, side, d, r["hedge_ratio"])
            for row in rows:
                self.db.add_position(trade_id, row["account"], st["day"], row["ticket"], row["symbol"], row["side"],
                                     row["volume"], row["entry"], row["sl"], row["tp"], row["risk_usd"], opened_at)
            self.db.log("entry", {"trade_id": trade_id, "legs": rows, "warnings": warnings})
            return {"trade_id": trade_id, "legs": rows, "warnings": warnings, "info": info}

    def reset_account_calibration(self):
        """Gelernte Werte der alten Konten vergessen."""
        keys = ("commission_per_lot", "commission_live_per_lot")
        rules = self.db.get_rules()
        for k in keys:
            rules[k] = 0.0
        self.db.set_rules(rules)
        self.db.set_setting("assets", [{**a, **{k: 0.0 for k in keys}} for a in self.assets()])
        self.entry_fees.clear()
        self.margin_cache.clear()
        self.specs.clear()
        self.sym_cache.clear()
        self.fx_symbols.clear()
        self.trail_cache = None

    def _set_commission(self, rule_key, value):
        nr = self.db.get_rules()
        nr[rule_key] = value
        self.db.set_rules(nr)
        self.remember_asset()

    def _learn_commission(self, row, res):
        """Round-Trip-Kommission aus der geschlossenen Position lernen."""
        if "commission" not in res or not res.get("volume"):
            return
        acc = row["account"]
        if row["symbol"] != (self.sym_p if acc == "prop" else self.sym_l):
            return
        key = "commission_per_lot" if acc == "prop" else "commission_live_per_lot"
        rt = round(max(0.0, -res["commission"] / res["volume"]), 4)
        if abs(rt - self.db.get_rules()[key]) > 0.005:
            self._set_commission(key, rt)
            self.db.log("commission_learned", {"account": acc, "symbol": row["symbol"], "per_lot_rt": rt})

    async def close_trade(self, trade_id):
        legs = [l for l in self.db.trade_positions(trade_id) if l["status"] == "open"]
        res = await asyncio.gather(*[self.brokers[l["account"]].acall("close_position", ticket=l["ticket"])
                                     for l in legs], return_exceptions=True)
        out = [_norm(x) for x in res]
        self.db.log("close_trade", {"trade_id": trade_id, "result": out})
        await self.sync()
        return out

    async def panic(self):
        res = await asyncio.gather(self.prop.acall("close_all"), self.live.acall("close_all"),
                                   return_exceptions=True)
        out = {"prop": res[0] if not isinstance(res[0], Exception) else [{"ok": False, "comment": str(res[0])}],
               "live": res[1] if not isinstance(res[1], Exception) else [{"ok": False, "comment": str(res[1])}]}
        self.db.log("panic", out)
        await self.sync()
        return out

    async def eod_check(self):
        if self.eod_status(self.db.get_rules())["phase"] != "close":
            return
        now = time.time()
        for acc, br in self.brokers.items():
            for p in await br.acall("positions"):
                k = (acc, p["ticket"])
                if now - self.orphan_tries.get(k, 0) < 5:
                    continue
                self.orphan_tries[k] = now
                r = _norm((await asyncio.gather(br.acall("close_position", ticket=p["ticket"]),
                                                return_exceptions=True))[0])
                self.db.log("eod_close", {"account": acc, "ticket": p["ticket"], "result": r})
        await self.sync()

    async def sync(self):
        rows = self.db.open_positions()
        if not rows:
            return
        rules = self.db.get_rules()
        cur = {}
        for acc in {r["account"] for r in rows}:
            cur[acc] = {p["ticket"] for p in await self.brokers[acc].acall("positions")}
        for row in rows:
            if row["ticket"] in cur[row["account"]]:
                continue
            res = await self.brokers[row["account"]].acall("position_result", ticket=row["ticket"])
            if res.get("closed"):
                self.db.close_position(row["id"], res["pnl"], res["close_price"], res["reason"],
                                       ts_iso(res["close_time"]))
                self._learn_commission(row, res)
        now = time.time()
        for t in self.db.open_trades():
            legs = self.db.trade_positions(t["id"])
            open_legs = [l for l in legs if l["status"] == "open"]
            if not open_legs:
                self.db.close_trade(t["id"])
                continue
            if len(open_legs) < len(legs) and rules["auto_close_orphan"]:
                for l in open_legs:
                    k = (l["account"], l["ticket"])
                    if now - self.orphan_tries.get(k, 0) < 5:
                        continue
                    self.orphan_tries[k] = now
                    r = _norm((await asyncio.gather(
                        self.brokers[l["account"]].acall("close_position", ticket=l["ticket"]),
                        return_exceptions=True))[0])
                    self.db.log("orphan_close", {"trade_id": t["id"], "account": l["account"],
                                                 "ticket": l["ticket"], "result": r})
