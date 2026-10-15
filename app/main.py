import asyncio
import functools
import json
import multiprocessing as mp
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .broker import create_brokers
from .config import load_config
from .db import DB, ts_iso
from .engine import HedgeEngine, RuleError
from . import simulator

STATIC = Path(__file__).resolve().parent.parent / "static"


class Rules(BaseModel):
    daily_drawdown: float = Field(gt=0)
    consistency_pct: float = Field(gt=0, le=100)
    max_loss: float = Field(gt=0)
    profit_target: float = Field(gt=0)
    initial_balance: float = Field(gt=0)
    hedge_ratio: float = Field(ge=0, le=10)
    sl_distance: float = Field(gt=0)
    atr_enabled: bool = False
    atr_timeframe: str = Field(default="H1", pattern="^(M5|M15|M30|H1|H4|D1)$")
    atr_period: int = Field(default=14, ge=2, le=200)
    atr_mult: float = Field(default=2.0, gt=0, le=20)
    max_lots: float = Field(default=0.0, ge=0)
    target_cap_enabled: bool = True
    align_lots: bool = True
    target_buffer_pct: float = Field(default=15.0, ge=0, le=100)
    risk_pct: float = Field(gt=0, le=100)
    buffer_pct: float = Field(ge=0, le=100)
    safety_pct: float = Field(default=5.0, ge=0, lt=100)
    reset_hour: int = Field(ge=0, le=23)
    auto_close_orphan: bool = True
    eod_enabled: bool = True
    eod_time: str = Field(default="17:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    eod_close_min: int = Field(default=5, ge=0, le=240)
    eod_block_min: int = Field(default=15, ge=0, le=240)
    max_price_dev_pct: float = Field(default=1.0, gt=0, le=50)
    commission_per_lot: float = Field(default=0.0, ge=0)
    commission_live_per_lot: float = Field(default=0.0, ge=0)
    cost_warn_pct: float = Field(default=10.0, ge=0, le=100)
    cost_max_pct: float = Field(default=20.0, gt=0, le=100)
    hedge_mode: str = Field(default="fee", pattern="^(fee|sl|mse)$")
    ratio_correction: bool = True
    ratio_scope: str = Field(default="challenge", pattern="^(challenge|day)$")
    ratio_correction_max_pct: float = Field(default=50.0, ge=0, le=200)
    mirror_brackets: bool = True
    margin_max_use_pct: float = Field(default=80.0, gt=0, le=100)
    max_loss_mode: str = Field(default="static", pattern="^(static|eod_trailing)$")
    trail_lock: bool = False
    weekend_close_enabled: bool = True
    weekend_time: str = Field(default="17:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    weekend_close_min: int = Field(default=15, ge=0, le=600)
    weekend_block_min: int = Field(default=60, ge=0, le=1440)
    max_risk_enabled: bool = True
    max_risk_pct: float = Field(default=1.5, gt=0, le=100)
    direction_mode: str = Field(default="alternate", pattern="^(alternate|random)$")


class SymbolChange(BaseModel):
    prop: str = Field(min_length=1, max_length=64)
    live: str = Field(min_length=1, max_length=64)
    sl_distance: float | None = Field(default=None, gt=0)


class SimRequest(BaseModel):
    params: dict = {}
    mode: str = Field(default="optimize", pattern="^(optimize|custom|funded)$")
    hE: float | None = Field(default=None, ge=0, le=2)
    hF: float | None = Field(default=None, ge=0, le=2)
    hE2: float | None = Field(default=None, ge=0, le=2)
    step: float = Field(default=0.05, ge=0.005, le=0.25)
    hmax: float = Field(default=1.0, gt=0, le=2)
    metric: str = Field(default="sortino", pattern="^(sortino|ev|roi|worst)$")
    after_eval: bool = False
    pe_actual: float | None = None


class SimConfig(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    params: dict
    state: dict = {}
    result: dict = {}


class AccountValues(BaseModel):
    balance: float | None = Field(default=None, ge=0)
    realized_today: float | None = None


class AccountsUpdate(BaseModel):
    prop: AccountValues | None = None
    live: AccountValues | None = None


async def broadcast_loop(app):
    engine = app.state.engine
    while True:
        try:
            st = await engine.state()
        except Exception as e:
            st = {"error": str(e)}
        engine.last_state = st
        msg = json.dumps(st)
        for ws in list(app.state.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                app.state.clients.discard(ws)
        await asyncio.sleep(0.5)


async def sync_loop(engine):
    while True:
        try:
            await engine.sync()
            await engine.eod_check()
        except Exception as e:
            engine.db.log("sync_error", {"error": str(e)})
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app):
    cfg = load_config()
    db = DB(cfg.get("db_path", "prophedge.db"))
    prop, live = await asyncio.to_thread(create_brokers, cfg)
    app.state.cfg = cfg
    app.state.engine = HedgeEngine(cfg, prop, live, db)
    app.state.clients = set()
    # eigener Prozess, damit lange Rechnungen den Handel nicht blockieren
    app.state.sim_pool = ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"))
    app.state.sim_lock = asyncio.Lock()
    app.state.sim_seq = 0
    tasks = [asyncio.create_task(broadcast_loop(app)), asyncio.create_task(sync_loop(app.state.engine))]
    yield
    for t in tasks:
        t.cancel()
    app.state.sim_pool.shutdown(wait=False, cancel_futures=True)
    prop.close()
    live.close()


app = FastAPI(title="PropHedge Terminal", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/rules")
async def get_rules():
    return app.state.engine.db.get_rules()


@app.put("/api/rules")
async def put_rules(rules: Rules):
    app.state.engine.db.set_rules(rules.model_dump())
    app.state.engine.remember_asset()
    return app.state.engine.db.get_rules()


@app.post("/api/sim")
async def sim(req: SimRequest):
    if req.mode == "custom" and (req.hE is None or req.hF is None):
        raise HTTPException(400, "h_E und h_F angeben")
    if req.mode == "funded" and req.hE is None:
        raise HTTPException(400, "h_E angeben")
    app.state.sim_seq += 1
    mine = app.state.sim_seq
    async with app.state.sim_lock:
        if mine != app.state.sim_seq:  # neuere Anfrage wartet schon
            raise HTTPException(409, "Überholt von einer neueren Berechnung")
        job = functools.partial(simulator.run, req.params, req.mode, req.hE, req.hF, req.step, req.hmax,
                                req.metric, req.after_eval, req.pe_actual, req.hE2)
        try:
            return await asyncio.get_running_loop().run_in_executor(app.state.sim_pool, job)
        except (ValueError, ZeroDivisionError) as e:
            raise HTTPException(400, str(e))


@app.get("/api/sim/configs")
async def sim_configs():
    return app.state.engine.db.get_setting("sim_configs", [])


@app.post("/api/sim/configs")
async def sim_config_save(cfg: SimConfig):
    db = app.state.engine.db
    lst = db.get_setting("sim_configs", [])
    item = {"id": uuid.uuid4().hex[:12], "created": int(time.time()), **cfg.model_dump()}
    lst.append(item)
    db.set_setting("sim_configs", lst[-100:])
    return item


@app.delete("/api/sim/configs/{cid}")
async def sim_config_delete(cid: str):
    db = app.state.engine.db
    lst = [c for c in db.get_setting("sim_configs", []) if c.get("id") != cid]
    db.set_setting("sim_configs", lst)
    return lst


@app.post("/api/ratio/reset")
async def ratio_reset():
    eng = app.state.engine
    st = eng.last_state if eng.last_state and "server_time" in eng.last_state else await eng.state()
    since = ts_iso(st["server_time"])
    eng.db.set_setting("ratio_since", since)
    # Startpunkt für EOD-Trailing
    eng.db.set_setting("challenge_start_ts", int(st["server_time"]))
    eng.db.set_setting("challenge_trade_id", eng.db.next_trade_id())  # für die Richtungsfolge
    eng.reset_account_calibration()
    eng.db.log("ratio_reset", {"since": since})
    return {"since": since}


@app.get("/api/state")
async def state():
    try:
        return await app.state.engine.state()
    except Exception as e:
        raise HTTPException(503, str(e))


@app.get("/api/candles")
async def candles(tf: str = "M1", count: int = 500, account: str = "prop"):
    if tf not in ("M1", "M5", "M15", "M30", "H1", "H4", "D1") or account not in ("prop", "live"):
        raise HTTPException(400, "Ungültiger Timeframe oder Konto")
    eng = app.state.engine
    sym = eng.sym_p if account == "prop" else eng.sym_l
    try:
        return await eng.brokers[account].acall("rates", symbol=sym, timeframe=tf, count=max(10, min(count, 5000)))
    except Exception as e:
        raise HTTPException(503, str(e))


@app.post("/api/enter")
async def enter():
    try:
        return await app.state.engine.enter()
    except RuleError as e:
        raise HTTPException(409, e.reasons)
    except Exception as e:
        raise HTTPException(503, [str(e)])


@app.get("/api/symbols")
async def symbols(account: str = "prop", q: str = ""):
    if account not in ("prop", "live"):
        raise HTTPException(400, "Ungültiges Konto")
    try:
        return await app.state.engine.search_symbols(account, q)
    except Exception as e:
        raise HTTPException(503, str(e))


@app.get("/api/symbols/match")
async def symbols_match(prop: str):
    try:
        return await app.state.engine.match_symbol(prop)
    except Exception as e:
        raise HTTPException(503, str(e))


@app.get("/api/symbols/check")
async def symbols_check(prop: str, live: str):
    return await app.state.engine.check_symbols(prop, live)


@app.put("/api/symbols")
async def put_symbols(req: SymbolChange):
    try:
        return await app.state.engine.set_symbols(req.prop.strip(), req.live.strip(), req.sl_distance)
    except RuleError as e:
        raise HTTPException(409, e.reasons)


@app.delete("/api/assets")
async def delete_asset(prop: str, live: str):
    try:
        app.state.engine.remove_asset(prop, live)
    except RuleError as e:
        raise HTTPException(409, e.reasons)
    return app.state.engine.assets()


@app.put("/api/accounts")
async def put_accounts(req: AccountsUpdate):
    values = {k: v.model_dump(exclude_none=True) for k, v in (("prop", req.prop), ("live", req.live)) if v}
    try:
        return await app.state.engine.set_accounts(values)
    except RuleError as e:
        raise HTTPException(409, e.reasons)


@app.post("/api/trades/{trade_id}/close")
async def close_trade(trade_id: int):
    return await app.state.engine.close_trade(trade_id)


@app.post("/api/panic")
async def panic():
    return await app.state.engine.panic()


@app.get("/api/history")
async def history(day: str | None = None, account: str | None = None):
    eng = app.state.engine
    if day is None:
        st = eng.last_state or await eng.state()
        day = st.get("day")
    return {"day": day, "positions": eng.db.positions_by_day(day, account), "summary": eng.db.day_summary()}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    app.state.clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        app.state.clients.discard(websocket)
