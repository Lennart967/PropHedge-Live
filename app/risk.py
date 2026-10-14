import math


def consistency_limit(r):
    return r["profit_target"] * r["consistency_pct"] / 100


def atr(candles, period):
    """Average True Range über die letzten `period` abgeschlossenen Kerzen (die laufende Kerze zählt nicht)."""
    c = candles[:-1]
    if len(c) < period + 1:
        return None
    trs = [max(b["high"] - b["low"], abs(b["high"] - a["close"]), abs(b["low"] - a["close"]))
           for a, b in zip(c[-period - 1:-1], c[-period:])]
    return sum(trs) / period


def vpu(spec):
    return spec["tick_value"] / spec["tick_size"]


def decimals(step):
    s = f"{step:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def floor_step(x, step):
    if x <= 0:
        return 0.0
    return round(math.floor(x / step + 1e-9) * step, decimals(step))


def ceil_step(x, step):
    if x <= 0:
        return 0.0
    return round(math.ceil(x / step - 1e-9) * step, decimals(step))


def target_lots(r, spec, cost, left):
    """Prop-Lots, deren TP (netto, nach Kommission und ggf. vor dem TP abgeschnitten) den Rest bis zum Profit-Ziel
    plus Puffer bringt. Aufgerundet, damit der TP sicher reicht. None = kein Ziel-Deckel."""
    if not r.get("target_cap_enabled") or left <= 0:
        return None
    per_lot = vpu(spec) * (r["sl_distance"] - cost["prop_tp_cut"]) - r["commission_per_lot"]
    if per_lot <= 0:
        return None
    need = left * (1 + r.get("target_buffer_pct", 0.0) / 100) / per_lot
    return max(ceil_step(need, spec["vol_step"]), spec["vol_min"])


def round_step(x, step):
    if x <= 0:
        return 0.0
    return round(round(x / step) * step, decimals(step))


def bracket(side, price, dist, digits):
    if side == "buy":
        return round(price - dist, digits), round(price + dist, digits)
    return round(price + dist, digits), round(price - dist, digits)


def exit_fee(entry_fee, rt_per_lot, volume):
    """Erwartete Exit-Kommission (negativ). Bekannte Entry-Kommission wird von der Round-Trip-Kommission
    abgezogen; so stimmt es auch bei Brokern, die alles beim Öffnen oder alles beim Schließen berechnen."""
    if entry_fee is None:
        return -rt_per_lot / 2 * volume
    return min(0.0, -rt_per_lot * volume - entry_fee)


def open_exposure(positions, specs, buffer_pct=0.0, rt_per_lot=0.0, entry_fees=None):
    risk = reward = 0.0
    unprotected = []
    for p in positions:
        if not p["sl"] or not p["tp"]:
            unprotected.append(p["ticket"])
            continue
        d = 1 if p["side"] == "buy" else -1
        v = vpu(specs[p["symbol"]]) * p["volume"]
        # Entry-Kommission ist schon realisiert, offen ist nur noch der Exit
        fee = exit_fee((entry_fees or {}).get(p["ticket"]), rt_per_lot, p["volume"])
        risk += (p["price_open"] - p["sl"]) * d * v * (1 + buffer_pct / 100) - fee
        reward += (p["tp"] - p["price_open"]) * d * v + fee
    return {"risk": risk, "reward": reward, "unprotected": unprotected}


def rooms(r, realized, exp, balance, max_loss_level=None):
    """Verlustseite gegen den Daily Drawdown, Gewinnseite gegen die Consistency Rule.
    max_loss_level: Kontostand, ab dem der Max Loss verletzt ist (statisch: Startkapital − Max Loss,
    EOD trailing: höchster Tagesend-Stand × (1 − Max-Loss-%)).
    safety_pct: Sicherheitspuffer in % des jeweiligen Limits. Gerechnet wird gegen die verkleinerten Limits,
    damit Slippage, Kommission und Rundung nie bis an die echte Grenze reichen."""
    keep = 1 - r.get("safety_pct", 0.0) / 100
    dd, cons = r["daily_drawdown"] * keep, consistency_limit(r) * keep
    pnl_total = balance - r["initial_balance"]
    if max_loss_level is None:
        max_loss_level = r["initial_balance"] - r["max_loss"]
    max_loss_level += r["max_loss"] * (1 - keep)
    loss_room = dd + realized - exp["risk"]
    profit_room = cons - realized - exp["reward"]
    return {"dd_limit": dd, "consistency_limit": cons, "realized": realized,
            "dd_limit_real": r["daily_drawdown"], "consistency_limit_real": consistency_limit(r),
            "open_risk": exp["risk"], "open_reward": exp["reward"],
            "loss_room": loss_room, "profit_room": profit_room,
            "dd_room": loss_room, "consistency_room": profit_room,
            "max_loss_room": balance - max_loss_level - exp["risk"], "max_loss_level": max_loss_level,
            "pnl_total": pnl_total, "target_progress": pnl_total / r["profit_target"],
            # Rest bis zum Profit-Ziel, offene TP-Chancen schon abgezogen
            "target_left": r["profit_target"] - pnl_total - exp["reward"]}


def position_size(r, spec, price, rm, free_margin, margin_per_lot, extra_caps=None):
    """Prop-Lots. Margin pro Lot kommt vom Broker (order_calc_margin), damit gilt der echte Hebel des Symbols."""
    unit = vpu(spec)
    fee = r["commission_per_lot"]
    gross = r["sl_distance"] * unit
    per_lot_risk = gross * (1 + r["buffer_pct"] / 100) + fee
    per_lot_reward = max(gross - fee, 1e-9)
    f = r["risk_pct"] / 100
    use = r["margin_max_use_pct"] / 100
    caps = {
        "Daily Drawdown": max(rm["loss_room"], 0) * f / per_lot_risk,
        "Consistency Rule": max(rm["profit_room"], 0) * f / per_lot_reward,
        "Max Loss": max(rm["max_loss_room"], 0) * f / per_lot_risk,
        "Margin Prop-Konto": max(free_margin, 0) * use / margin_per_lot if margin_per_lot > 0 else 0.0,
        "Max-Volumen des Symbols": spec["vol_max"],
        # Max. Risiko pro Trade (z. B. 1,5 % vom Startkapital): Kursverlust am SL + Kommission
        **({"Max. Risiko pro Trade": r["max_risk_pct"] / 100 * r["initial_balance"] / (gross + fee)}
           if r["max_risk_enabled"] and gross + fee > 0 else {}),
        **({"Max. Lots Prop": r["max_lots"]} if r.get("max_lots", 0) > 0 else {}),
        **(extra_caps or {}),
    }
    by = min(caps, key=caps.get)
    lots = floor_step(caps[by], spec["vol_step"])
    if lots < spec["vol_min"] - 1e-12:
        lots = 0.0
    return {"lots": lots, "limited_by": by, "risk_usd": lots * per_lot_risk, "reward_usd": lots * per_lot_reward,
            "sl_usd": lots * gross, "commission_usd": lots * fee, "margin": lots * margin_per_lot,
            "notional": lots * spec["contract_size"] * price}


def costs(r, spec_p, spec_l, spread_p, spread_l):
    """Kosten eines Hedges in Preiseinheiten: beide Spreads + beide Kommissionen (je Lot in Preis umgerechnet).

    S = (Spread Prop + Spread Live) / 2. Gespiegelt liegen Live-TP/-SL exakt auf Prop-SL/-TP.
    Prop-SL: Prop löst zuerst aus (Ask bzw. Bid erreicht das Level vor der Gegenseite), Live wird zum Markt
    geschlossen und gewinnt d − 2S. Prop-TP: der Live-SL löst um S früher aus, Live verliert d + S, Prop wird
    vor seinem TP geschlossen und bekommt d − S.
    Ungespiegelt (±d um den eigenen Fill): Prop-SL wie oben; bei Prop-TP trifft zuerst der Live-SL (Verlust d),
    Prop bekommt nur d − 2S."""
    s = (spread_p + spread_l) / 2
    total = 2 * s + r["commission_per_lot"] / vpu(spec_p) + r["commission_live_per_lot"] / vpu(spec_l)
    d = r["sl_distance"]
    mirror = r["mirror_brackets"]
    return {"spread_p": spread_p, "spread_l": spread_l, "half": s, "total": total,
            "pct": total / d * 100 if d > 0 else None,
            "live_win": d - 2 * s, "live_loss": d + s if mirror else d,
            "prop_tp_cut": s if mirror else 2 * s}


def hedge_outcomes(lots, spec_p, spec_l, r, cost, fx=1.0):
    """Netto-Ergebnisse pro Hedge in Prop-Währung: Prop-Verlust bei SL, Prop-Gewinn bei TP, Live-Gewinn bzw.
    -Verlust pro Live-Lot (inkl. Spread und Kommissionen)."""
    up, ul = vpu(spec_p), vpu(spec_l)
    d = r["sl_distance"]
    return {"prop_loss": lots * (up * d + r["commission_per_lot"]),
            "prop_gain": lots * (up * (d - cost["prop_tp_cut"]) - r["commission_per_lot"]),
            "live_gain": (ul * cost["live_win"] - r["commission_live_per_lot"]) * fx,
            "live_loss": (ul * cost["live_loss"] + r["commission_live_per_lot"]) * fx}


def mse_lots(o, ratio, prop_sum=0.0, live_sum=0.0, p_sl=0.5):
    """Live-Lots, die den erwarteten quadratischen Fehler der kumulierten Hedge Ratio nach diesem Hedge minimieren.

    Fehler je Szenario: Ratio danach − Ziel = −(Live + Ratio × Prop) / Prop, beides kumuliert. Mit
    e = Live + Ratio × Prop (Abweichung in Geld) und Gewicht w = 1 / Prop² ist das ein gewichtetes
    Kleinste-Quadrate-Problem in L mit geschlossener Lösung:
        SL: e_SL = D + L·g − Ratio·PL      TP: e_TP = D − L·c + Ratio·PG
        L* = [p·w_SL·g·(Ratio·PL − D) + (1−p)·w_TP·c·(D + Ratio·PG)] / [p·w_SL·g² + (1−p)·w_TP·c²]
    Prop nach dem Szenario wird nach unten auf die Größe dieses Hedges begrenzt, sonst explodiert das Gewicht,
    wenn der kumulierte Prop-P&L zufällig nahe 0 landet."""
    g, c, pl, pg = o["live_gain"], o["live_loss"], o["prop_loss"], o["prop_gain"]
    if g <= 0 or c <= 0 or pl <= 0:
        return 0.0
    dev = live_sum + ratio * prop_sum
    floor = max(pl, pg, 1e-9)
    w_sl = 1 / max(abs(prop_sum - pl), floor) ** 2
    w_tp = 1 / max(abs(prop_sum + pg), floor) ** 2
    num = p_sl * w_sl * g * (ratio * pl - dev) + (1 - p_sl) * w_tp * c * (dev + ratio * pg)
    return max(num / (p_sl * w_sl * g * g + (1 - p_sl) * w_tp * c * c), 0.0)


def trade_fees(lots, hedge_lots, spec_p, spec_l, r, cost, fx=1.0):
    """Erwartete Kosten eines Hedges in Prop-Währung, aufgeteilt wie im Simulator (f_E Prop, f_L Live).
    Live trägt beide Spreads (gewinnt d − Kosten, verliert d + Kosten); ohne Spiegeln je Ausgang anders, daher
    der Mittelwert. Prop: Kommission plus der Teil des TP, der ungespiegelt vor dem Ziel abgeschnitten wird."""
    d = r["sl_distance"]
    spread_l = ((d - cost["live_win"]) + (cost["live_loss"] - d)) / 2
    f_l = hedge_lots * (vpu(spec_l) * spread_l + r["commission_live_per_lot"]) * fx
    f_p = lots * (r["commission_per_lot"] + vpu(spec_p) * cost["prop_tp_cut"] / 2)
    return {"prop": f_p, "live": f_l}


def hedge_volume(lots, spec_p, spec_l, r, cost, track=None, fx=1.0):
    """Live-Lots je nach hedge_mode. Rückgabe: (Lots, Lots ohne Ausgleich).

    fee: Live bewegt sich mit dem Kurs genau Ratio × so viel wie Prop (reines Volumenverhältnis). Spread und
         Kommissionen sind eine feste Gebühr pro Trade auf beiden Ausgängen, wie im Simulator (f_E, f_L); es wird
         nichts nachgeholt, daher kein Ausgleich und keine Verstärkung auf dem Pass-Pfad.
    sl:  Bei Prop-SL ist der Live-Gewinn netto genau Ratio × Prop-Verlust netto; alle Kosten landen auf dem
         Pass-Pfad. Der Ausgleich holt die bisherige Abweichung im SL-Fall nach (begrenzt auf ±max %).
    mse: Kleinster mittlerer quadratischer Fehler der (kumulierten) Ratio über Prop-SL und Prop-TP.
    fx = Wert von 1 Einheit Live-Kontowährung in Prop-Kontowährung."""
    ratio = r["hedge_ratio"]
    if lots <= 0 or ratio <= 0:
        return 0.0, 0.0
    mode = r.get("hedge_mode", "fee")
    m = r["ratio_correction_max_pct"] / 100
    clamp = lambda x, b: min(max(x, b * (1 - m)), b * (1 + m))
    o = hedge_outcomes(lots, spec_p, spec_l, r, cost, fx)
    if mode == "fee":
        base = target = ratio * lots * vpu(spec_p) / (vpu(spec_l) * fx)
    elif mode == "sl":
        if o["live_gain"] <= 0:
            return 0.0, 0.0
        base = ratio * o["prop_loss"] / o["live_gain"]
        target = base
        if r["ratio_correction"] and track:
            target = clamp(base - track["deviation"] / o["live_gain"], base)
    else:
        base = target = mse_lots(o, ratio)
        if r["ratio_correction"] and track:
            target = clamp(mse_lots(o, ratio, track["prop"], track["live"]), base)

    def to_lots(x):
        v = min(round_step(x, spec_l["vol_step"]), spec_l["vol_max"])
        return v if v >= spec_l["vol_min"] - 1e-12 else 0.0

    return to_lots(target), to_lots(base)


def margin_check(acc, required, worst_loss):
    """Margin-Level im schlechtesten Fall: neue Position offen, alle Stops kurz vor dem Auslösen.
    Schwelle ist das höhere von Margin-Call- und Stop-Out-Level des Brokers."""
    used = acc.get("margin", 0.0) + required
    eq = acc["equity"] - worst_loss
    so_mode = acc.get("margin_so_mode", 0)
    threshold = max(acc.get("margin_so_call", 0.0), acc.get("margin_so_so", 0.0))
    if so_mode == 1:  # Schwelle in Geld statt Prozent
        level, ok = None, eq > threshold
    else:
        level = eq / used * 100 if used > 0 else None
        ok = level is None or level > threshold
    return {"required": required, "free": acc["margin_free"], "used_after": used, "equity_worst": eq,
            "level_worst": level, "threshold": threshold, "threshold_money": so_mode == 1, "ok": ok,
            "usage_pct": required / acc["margin_free"] * 100 if acc["margin_free"] > 0 else None}


def violations(r, rm, size, exp, spec_p, hedge_lots, cost=None):
    v = []
    if cost and size["lots"] > 0 and cost["pct"] is not None and cost["pct"] > r["cost_max_pct"]:
        v.append(f"Kosten (Spread + Kommission) sind {cost['pct']:.0f} % des SL/TP-Abstands "
                 f"(max. {r['cost_max_pct']:g} %). Abstand vergrößern oder auf engeren Spread warten.")
    if exp["unprotected"]:
        v.append(f"Prop-Position(en) ohne SL/TP offen (Ticket {', '.join(map(str, exp['unprotected']))}). "
                 "Erst absichern oder schließen.")
    if r["commission_per_lot"] >= r["sl_distance"] * vpu(spec_p):
        v.append("Die Kommission ist größer als der TP-Gewinn. SL/TP-Abstand vergrößern.")
    if r["sl_distance"] < spec_p["stops_level"] * spec_p["point"]:
        v.append(f"SL/TP-Abstand liegt unter dem Mindestabstand des Brokers "
                 f"({spec_p['stops_level'] * spec_p['point']:g}).")
    if size["lots"] <= 0:
        v.append(f"Positionsgröße ist 0, begrenzt durch {size['limited_by']}.")
        return v
    if size["risk_usd"] > rm["loss_room"] + 1e-6:
        v.append("Der Stop Loss würde den Daily Drawdown brechen.")
    if size["reward_usd"] > rm["profit_room"] + 1e-6:
        v.append("Der Take Profit würde die Consistency Rule brechen.")
    if size["risk_usd"] > rm["max_loss_room"] + 1e-6:
        v.append("Der Stop Loss würde den Max Loss brechen.")
    if r["hedge_ratio"] > 0 and hedge_lots <= 0:
        v.append("Hedge-Volumen liegt unter dem Mindestvolumen des Live-Kontos.")
    return v
