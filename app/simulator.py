"""PropHedge-Simulator: geschlossene Lösung aus dem Paper "PropHedge – The Payoff Structure of Hedged Prop Firm
Evaluation Accounts with Static Drawdown" (Rüther 2026), Gleichungen (1)–(16).

Gegenüber hedge_simulator.ipynb korrigiert:
  * Profit-Split φ fehlte im Notebook (Payout war immer 100 %).
  * int()-Abschneiden im Pass-Pfad entfernt.
  * Live-Gebühren in der Funded-Phase nur, wenn dort auch gehedgt wird (wie in der Eval-Phase).
  * "ROI" bezog sich auf h_F × A_F (Nominalwert) statt auf das gebundene Kapital; jetzt Kapitalbedarf Π_min (14).
Optional (Standard an): Gebühren je Pfad mit der bedingten Trade-Anzahl statt der mittleren. Der Erwartungswert
bleibt identisch, aber die einzelnen Pfade und damit die Sortino-Ratio werden exakt.

EOD-Trailing-Drawdown (je Phase wählbar): Nach jedem Handelstag (alle k Trades) steigt die Max-Loss-Grenze auf
Tagesend-Stand × (1 − D), sinkt aber nie. Optional stoppt sie beim Startkapital. Dafür gibt es keine geschlossene
Lösung mehr (die Grenze hängt vom ganzen Pfad ab, ein Fail endet nicht mehr bei einem festen Verlust), daher
Monte-Carlo mit festem Seed. Statische Phasen werden im Monte-Carlo identisch simuliert, sobald eine Phase trailt.

2-Step-Challenges: Eval-Steps hintereinander (jeder Step startet wieder bei der Kontogröße A und hat eine eigene
Hedge Ratio), danach die Funded-Phase. 1-Step ist der Sonderfall mit einem Step (= Paper).
"""
import math
from functools import lru_cache

import numpy as np

DEFAULTS = {
    "A": 50000.0,         # Kontogröße
    "C": 319.0,           # Preis der Evaluation
    "r": 0.02,            # Risiko pro Trade (Anteil der Kontogröße)
    "T_E": 0.10, "D_E": 0.10,   # Eval: Profit-Ziel / Max Drawdown
    "T_F": 0.08, "D_F": 0.04,   # Funded: Payout-Ziel / Max Drawdown
    "f_E": 5.0,           # Gebühr pro Trade Prop-Konto, Eval
    "f_EF": 0.0,          # Gebühr pro Trade Prop-Konto, Funded
    "f_L": 0.0,           # Gebühr pro Trade Live-Konto bei Hedge Ratio 1 (skaliert mit h: Spread × Live-Lots)
    "phi": 1.0,           # Profit-Split
    "q": 1.0,             # Anteil tatsächlich ausgezahlter Payouts
    "C_F": 0.0,           # Aktivierungsgebühr Funded
    "funded_scaled": True,  # A_F = A·(1+T_E); sonst A_F = A
    "exact_fees": True,
    "dd_mode_E": "static",  # static | trailing (EOD)
    "dd_mode_F": "static",
    "trail_lock": False,    # Trailing stoppt beim Startkapital
    "trades_per_day": 2,
    "mc_paths": 20000,
    "max_r_enabled": True,  # Risiko pro Trade deckeln (FTMO achtet teils auf max. 1,5 %)
    "max_r": 0.015,
    "two_step": False,      # 2-Step-Challenge: Step 1 (T_E, D_E) und Step 2 (T_E2, D_E2), je eigene Hedge Ratio
    "T_E2": 0.05, "D_E2": 0.10,
    "dd_mode_E2": "static",
    "refund_fee": False,    # Challenge-Gebühr wird mit dem ersten Payout erstattet (z. B. FTMO)
}
SEED = 20261013


def walk(T, D, r):
    """Gambler's Ruin: Pass-Wahrscheinlichkeit und erwartete Trade-Anzahl (gesamt, bedingt auf Pass/Fail)."""
    a, b = T / r, D / r  # Schritte bis Ziel / bis Drawdown
    return {"p": D / (D + T), "n": a * b,
            "n_pass": a * (a + 2 * b) / 3, "n_fail": b * (2 * a + b) / 3}


@lru_cache(maxsize=16)
def simulate_phase(T, D, r, k, trailing, lock, n, seed):
    """Monte-Carlo einer Phase: symmetrischer Random Walk mit ±r, Ziel +T, Max-Loss-Grenze 1 − D (statisch)
    bzw. nach jedem Tag max(Grenze, Stand × (1 − D)) (EOD trailing). Rückgabe je Pfad: bestanden, P&L der Phase
    beim Ende (Anteil des Startkapitals; Fail = Grenze − 1) und Anzahl Trades."""
    rng = np.random.default_rng(seed)
    steps = np.zeros(n, dtype=np.int64)  # Stand = 1 + steps · r, ganzzahlig gegen Rundungsdrift
    thr = np.full(n, 1.0 - D)
    trades = np.zeros(n, dtype=np.int64)
    passed = np.zeros(n, dtype=bool)
    delta = np.zeros(n)
    active = np.arange(n)
    t = 0
    while active.size and t < 200000:
        # Nächstes Ereignis: TP (+r) bzw. Ziel, falls näher, oder SL (−r) bzw. Max-Loss-Grenze, falls näher.
        # Faire Wahrscheinlichkeit für "oben zuerst": (Stand − unten) / (oben − unten). Liegt die (trailende)
        # Grenze zwischen zwei Schritten, wird sie so mit der richtigen Wahrscheinlichkeit getroffen.
        bal = 1.0 + steps[active] * r
        lo_t = thr[active]
        up_lvl = np.minimum(bal + r, 1.0 + T)
        dn_lvl = np.maximum(bal - r, lo_t)
        go_up = rng.random(active.size) < (bal - dn_lvl) / (up_lvl - dn_lvl)
        trades[active] += 1
        up = go_up & (bal + r >= 1.0 + T - 1e-9)
        dn = ~go_up & (bal - r <= lo_t + 1e-9)
        steps[active] += np.where(go_up, 1, -1)
        passed[active[up]] = True
        delta[active[up]] = T
        delta[active[dn]] = lo_t[dn] - 1.0
        active = active[~(up | dn)]
        t += 1
        if trailing and t % k == 0 and active.size:
            new = (1.0 + steps[active] * r) * (1.0 - D)
            if lock:
                new = np.minimum(new, 1.0)
            thr[active] = np.maximum(thr[active], new)
    return {"passed": passed, "delta": delta, "trades": trades}


def eval_steps(P):
    """Eval-Steps als (Name, Ziel, Drawdown, Drawdown-Art)."""
    if not P["two_step"]:
        return [("Eval", P["T_E"], P["D_E"], P["dd_mode_E"])]
    return [("Step 1", P["T_E"], P["D_E"], P["dd_mode_E"]), ("Step 2", P["T_E2"], P["D_E2"], P["dd_mode_E2"])]


def funded_capital(P):
    """Startkapital Funded: A · (1 + Ziel des letzten Steps) oder Reset auf A."""
    return P["A"] * (1 + eval_steps(P)[-1][1]) if P["funded_scaled"] else P["A"]


def _mc_needed(P):
    return P["dd_mode_F"] == "trailing" or any(m == "trailing" for *_, m in eval_steps(P))


def _summary(P, hs, hF, paths, ev, down, stages, pi_pe, pi_min, n_E, n_F, pF, p_pass, payout_total, after_eval, mc):
    q = P["q"]
    sortino = ev / down if down > 1e-9 else None
    capital = max(0.0, -min([pi_min, pi_pe] + [st["pi_pass"] for st in stages]))
    p_payout = pF if after_eval else p_pass * pF
    ev_q1 = ev + (1 - q) * p_payout * payout_total
    q_star = 1 - ev_q1 / (p_payout * payout_total) if p_payout * payout_total > 0 else None
    pg = {k: v for k, _, _, v in paths}
    return {"hE": hs[0], "hE2": hs[1] if len(hs) > 1 else None, "hs": list(hs), "hF": hF,
            "ev": ev, "downside": down, "sortino": sortino,
            "capital": capital, "roi": ev / capital if capital > 0 else None, "q_star": q_star,
            "pi_pe": pi_pe, "pi_fe": stages[0]["pi_fail"] if stages else None,
            "pi_po": pg.get("PO"), "pi_den": pg.get("DEN", pi_min), "pi_ff": pg.get("FF"), "pi_min": pi_min,
            "payout": payout_total, "A_F": funded_capital(P), "pE": p_pass, "pF": pF,
            "n_E": n_E, "n_F": n_F, "mc": mc, "stages": stages,
            "paths": [{"key": k, "label": l, "p": p_, "pnl": v} for k, l, p_, v in paths if p_ > 1e-12]}


def evaluate(P, hs, hF, after_eval=False, pe_actual=None):
    """Pfad-PnL, Erwartungswert und Sortino. hs = Hedge Ratio je Eval-Step, hF = Funded.
    after_eval: alle Eval-Steps bestanden, nur die Funded-Phase ist offen (bedingte Wahrscheinlichkeiten).
    pe_actual: tatsächlicher Stand nach der Eval (überschreibt das Modell)."""
    if isinstance(hs, (int, float)):
        hs = [hs]
    if _mc_needed(P):
        return evaluate_mc(P, hs, np.array([hF], dtype=float), after_eval, pe_actual)[0]
    A, C, r = P["A"], P["C"], P["r"]
    steps = eval_steps(P)
    two = len(steps) > 1
    running, alive, paths, stages, n_E = -C, 1.0, [], [], 0.0
    for i, (name, T, D, _) in enumerate(steps):
        w, h = walk(T, D, r), hs[i]
        fee = P["f_E"] + P["f_L"] * h
        fp, ff = (w["n_pass"] * fee, w["n_fail"] * fee) if P["exact_fees"] else (w["n"] * fee,) * 2
        pi_fail = running + h * D * A - ff   # Live gewinnt h · Drawdown
        pi_pass = running - h * T * A - fp   # Live verliert h · Ziel
        stages.append({"name": name, "p": w["p"], "pi_fail": pi_fail, "pi_pass": pi_pass, "n": w["n"]})
        if not after_eval:
            paths.append((f"FE{i + 1}" if two else "FE", f"{name} verloren", alive * (1 - w["p"]), pi_fail))
        n_E += alive * w["n"]
        alive *= w["p"]
        running = pi_pass
    p_pass = alive
    pi_pe = running if pe_actual is None else pe_actual
    wf = walk(P["T_F"], P["D_F"], r)
    AF = funded_capital(P)
    fee_f = P["f_EF"] + P["f_L"] * hF
    ff_po, ff_ff = (wf["n_pass"] * fee_f, wf["n_fail"] * fee_f) if P["exact_fees"] else (wf["n"] * fee_f,) * 2
    payout_total = P["phi"] * P["T_F"] * AF + (C if P["refund_fee"] else 0.0)
    pi_ff = pi_pe + hF * P["D_F"] * AF - ff_ff - P["C_F"]
    pi_min = pi_pe - hF * P["T_F"] * AF - ff_po - P["C_F"]  # (14) tiefster laufender Stand
    pi_po = pi_min + payout_total
    pF, q = wf["p"], P["q"]
    base = 1.0 if after_eval else p_pass
    paths += [("PO", "Payout", base * pF * q, pi_po), ("DEN", "Payout verweigert", base * pF * (1 - q), pi_min),
              ("FF", "Funded verloren", base * (1 - pF), pi_ff)]
    paths = [x for x in paths if x[2] > 1e-12]
    ev = sum(p_ * x for _, _, p_, x in paths)
    down = math.sqrt(sum(p_ * min(x - ev, 0.0) ** 2 for _, _, p_, x in paths))
    return _summary(P, hs, hF, paths, ev, down, stages, pi_pe, pi_min, n_E, wf["n"], pF, p_pass,
                    payout_total, after_eval, False)


def evaluate_mc(P, hs, hF_arr, after_eval=False, pe_actual=None):
    """Monte-Carlo-Auswertung (EOD trailing), vektorisiert über mehrere h_F auf einmal. Liefert je h_F ein
    Ergebnis wie evaluate()."""
    A, C, r, k, n = P["A"], P["C"], P["r"], max(1, int(P["trades_per_day"])), int(P["mc_paths"])
    steps = eval_steps(P)
    two = len(steps) > 1
    AF = funded_capital(P)
    q = P["q"]
    payout_total = P["phi"] * P["T_F"] * AF + (C if P["refund_fee"] else 0.0)

    # Eval-Steps nacheinander: jeder Pfad läuft durch, bis er einen Step nicht besteht
    running = np.full(n, -C)
    alive = np.ones(n, dtype=bool)
    fail_val = np.full(n, np.nan)
    fail_step = np.full(n, -1)
    stages, n_E = [], 0.0
    for i, (name, T, D, mode) in enumerate(steps):
        S = simulate_phase(T, D, r, k, mode == "trailing", P["trail_lock"], n, SEED + i)
        h = hs[i]
        val = running - h * S["delta"] * A - S["trades"] * (P["f_E"] + P["f_L"] * h)
        failed = alive & ~S["passed"]
        fail_val[failed], fail_step[failed] = val[failed], i
        n_E += float(S["trades"][alive].sum()) / n
        ok = alive & S["passed"]
        stages.append({"name": name, "p": float(S["passed"][alive].mean()) if alive.any() else 0.0,
                       "pi_fail": float(val[failed].mean()) if failed.any() else 0.0,
                       "pi_pass": float(val[ok].mean()) if ok.any() else 0.0,
                       "n": float(S["trades"][alive].mean()) if alive.any() else 0.0})
        running = np.where(alive, val, running)
        alive = ok
    p_pass = float(alive.mean())
    pi_pe = float(pe_actual) if pe_actual is not None else (
        float(running[alive].mean()) if alive.any() else -C - sum(h * st[1] * A for h, st in zip(hs, steps)))
    if after_eval:
        base, alive = np.full(n, pi_pe), np.ones(n, dtype=bool)
    else:
        base = running

    F = simulate_phase(P["T_F"], P["D_F"], r, k, P["dd_mode_F"] == "trailing", P["trail_lock"], n, SEED + 10)
    H = np.asarray(hF_arr, dtype=float)[None, :]                      # 1 × m
    fee_f = P["f_EF"] + P["f_L"] * H
    fund = -H * F["delta"][:, None] * AF - F["trades"][:, None] * fee_f - P["C_F"]   # n × m
    x = np.where(alive[:, None], base[:, None] + fund, fail_val[:, None])
    po = alive & F["passed"]
    paid = x + po[:, None] * payout_total
    # Payout-Pfade aufteilen: mit q ausgezahlt, mit 1 − q verweigert
    xs = np.concatenate([paid, x[po]], axis=0)
    ws = np.concatenate([np.where(po, q, 1.0), np.full(int(po.sum()), 1 - q)])[:, None]
    wsum = float(ws.sum())
    ev = (ws * xs).sum(axis=0) / wsum
    down = np.sqrt((ws * np.minimum(xs - ev[None, :], 0.0) ** 2).sum(axis=0) / wsum)

    def group(mask, vals, weight):
        w = (weight * mask)[:, None]
        tot = float(w.sum())
        return tot / wsum, ((w * vals).sum(axis=0) / tot if tot > 0 else np.zeros(H.shape[1]))

    ones = np.ones(n)
    groups = []
    if not after_eval:
        for i, (name, *_rest) in enumerate(steps):
            pr, v = group(fail_step == i, x, ones)
            groups.append((f"FE{i + 1}" if two else "FE", f"{name} verloren", pr, v))
    for key, label, mask, vals, wt in (("PO", "Payout", po, paid, ones * q),
                                       ("DEN", "Payout verweigert", po, x, ones * (1 - q)),
                                       ("FF", "Funded verloren", alive & ~F["passed"], x, ones)):
        pr, v = group(mask, vals, wt)
        groups.append((key, label, pr, v))
    nf_pass = float(F["trades"][F["passed"]].mean()) if F["passed"].any() else 0.0
    pF = float(F["passed"].mean())
    out = []
    for j, hF in enumerate(H[0]):
        fee_j = P["f_EF"] + P["f_L"] * hF
        pi_min = pi_pe - hF * P["T_F"] * AF - nf_pass * fee_j - P["C_F"]
        paths = [(kk, l, pr, float(v[j])) for kk, l, pr, v in groups]
        out.append(_summary(P, hs, float(hF), paths, float(ev[j]), float(down[j]), stages, pi_pe, pi_min, n_E,
                            float(F["trades"].mean()), pF, p_pass, payout_total, after_eval, True))
    return out


def evaluate_all_hf(P, hs, hF_list, after_eval=False, pe_actual=None):
    if _mc_needed(P):
        return evaluate_mc(P, hs, np.array(hF_list, dtype=float), after_eval, pe_actual)
    return [evaluate(P, hs, h, after_eval, pe_actual) for h in hF_list]


def breakeven_ranges(P, hs, hF, after_eval=False, pe_actual=None, hcap=5.0):
    """Je Phase der Bereich ihrer Hedge Ratio, in dem sowohl ein Verlust dieser Phase als auch der Payout netto
    ≥ 0 enden; die anderen Ratios bleiben fest. Untergrenze: Live gewinnt bei Fail h · Drawdown und deckt damit
    Gebühren und bisherige Live-Verluste. Obergrenze: Live verliert beim Bestehen h · Ziel, der Payout muss das
    noch decken."""
    steps = eval_steps(P)
    two = len(steps) > 1
    phases = [] if after_eval else [(i, name, f"FE{i + 1}" if two else "FE")
                                    for i, (name, *_r) in enumerate(steps)]
    phases.append(("F", "Funded", "FF"))
    out = []
    for idx, name, own in phases:
        def pnl(h):
            hs2, hF2 = list(hs), hF
            if idx == "F":
                hF2 = h
            else:
                hs2[idx] = h
            return {p_["key"]: p_["pnl"] for p_ in evaluate(P, hs2, hF2, after_eval, pe_actual)["paths"]}

        lo_f = lambda h: pnl(h).get(own, 0.0)                                          # steigt mit h
        hi_f = lambda h: pnl(h).get("PO", 0.0)                                        # fällt mit h

        def root(f, rising):
            a, b = 0.0, hcap
            for _ in range(40):
                m = (a + b) / 2
                if (f(m) >= 0) == rising:
                    b = m
                else:
                    a = m
            return b if rising else a

        lo = 0.0 if lo_f(0.0) >= 0 else (root(lo_f, True) if lo_f(hcap) >= 0 else None)
        hi = None if hi_f(0.0) < 0 else (None if hi_f(hcap) >= 0 else root(hi_f, False))
        hi_inf = hi_f(0.0) >= 0 and hi_f(hcap) >= 0
        ok = lo is not None and hi_f(0.0) >= 0 and (hi_inf or lo <= hi)
        out.append({"phase": name, "key": idx, "lo": lo, "hi": hi, "hi_inf": hi_inf, "ok": ok,
                    "current": hF if idx == "F" else hs[idx]})
    return out


def _grid(step, hmax):
    n = int(round(hmax / step))
    return [round(i * step, 6) for i in range(n + 1)]


def run(params, mode="optimize", hE=None, hF=None, step=0.05, hmax=1.0, metric="sortino",
        after_eval=False, pe_actual=None, hE2=None):
    """mode: optimize (alle Ratios frei), custom (alle vorgegeben), funded (Eval-Ratios fest, h_F optimieren)."""
    P = {**DEFAULTS, **{k: v for k, v in (params or {}).items() if k in DEFAULTS}}
    for k in ("T_E", "D_E", "T_F", "D_F", "r", "T_E2", "D_E2"):
        if P[k] <= 0:
            raise ValueError(f"{k} muss > 0 sein")
    if P["D_E"] >= 1 or P["D_F"] >= 1 or P["D_E2"] >= 1:
        raise ValueError("Max Drawdown muss unter 100 % liegen")
    P["trades_per_day"] = max(1, int(P["trades_per_day"]))
    P["mc_paths"] = int(min(max(P["mc_paths"], 1000), 200000))
    # Gedeckeltes Risiko: kleinere Schritte, mehr Trades (n ~ T·D / r²) und damit mehr Gebühren
    P["r_requested"] = P["r"]
    if P["max_r_enabled"] and P["max_r"] > 0:
        P["r"] = min(P["r"], P["max_r"])
    two = bool(P["two_step"])
    notes = []
    # Drei Ratios: Raster begrenzen, damit die Optimierung schnell bleibt
    min_step = (0.05 if _mc_needed(P) else 0.025) if two else 0.0
    if step < min_step:
        step = min_step
        notes.append(f"Raster bei 2-Step auf {min_step:g} begrenzt (drei Ratios).")
    hs_axis = _grid(step, hmax)

    def score(m):
        # Sortino ist nur bei positivem EV sinnvoll: bei negativem EV würde ihre Maximierung das größte
        # Risiko bevorzugen. Daher: positive EV vor negativen; dort Sortino (bzw. ROI/EV), sonst kleinste
        # Abwärtsstreuung. Alle Pfade gleich und positiv (keine Streuung) ist das beste Ergebnis.
        if m["ev"] > 0:
            if metric == "sortino":
                return (1, math.inf if m["sortino"] is None else m["sortino"])
            return (1, m[metric] if m[metric] is not None else -math.inf)
        return (0, -m["downside"], m["ev"])

    def ratios(h1, h2):
        return [h1, h2 if h2 is not None else h1] if two else [h1]

    out = {"params": P, "mode": mode, "axis": hs_axis, "metric": metric, "mc": _mc_needed(P),
           "two_step": two, "notes": notes, "step": step}
    if mode == "funded":
        curve = evaluate_all_hf(P, ratios(hE, hE2), hs_axis, after_eval, pe_actual)
        best = max(curve, key=score)
        out["curve"] = [{"hF": m["hF"], "ev": m["ev"], "sortino": m["sortino"], "capital": m["capital"],
                         "paths": {p_["key"]: p_["pnl"] for p_ in m["paths"]}} for m in curve]
    else:
        # Heatmap: 1-Step h_E × h_F; 2-Step h_1 × h_2 mit dem jeweils besten h_F
        grid, grid_hf, best = [], [], None
        for h1 in hs_axis:
            row, row_hf = [], []
            for h2 in (hs_axis if two else [None]):
                res = evaluate_all_hf(P, ratios(h1, h2), hs_axis)
                if two:
                    cell = max(res, key=score)
                    row.append(cell[metric])
                    row_hf.append(cell["hF"])
                    cands = [cell]
                else:
                    row = [m[metric] for m in res]
                    cands = res
                for m in cands:
                    if best is None or score(m) > score(best):
                        best = m
            grid.append(row)
            if two:
                grid_hf.append(row_hf)
        out["grid"] = grid
        if two:
            out["grid_hf"] = grid_hf
    out["best"] = best
    out["selected"] = evaluate(P, ratios(hE, hE2), hF, after_eval, pe_actual) if mode == "custom" else best
    sel = out["selected"]
    out["ranges"] = breakeven_ranges(P, sel["hs"], sel["hF"], after_eval, pe_actual)
    return out
