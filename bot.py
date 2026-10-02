"""
AI Daytrading Bot - single file (Bot1 + Bot2 monitor + external memory)
Run:      python bot.py            -> trading cycle
Monitor:  python bot.py monitor    -> health report only
"""
import os, sys, re, json, time, logging, tempfile, fcntl
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (LimitOrderRequest, TrailingStopOrderRequest,
                                     GetOrdersRequest)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed


# ============================ CONFIG ============================
class C:
    PAPER_TRADING = True            # <<< set False for real (cash) account

    BASE_UNIVERSE = ["TQQQ", "SOXL", "LABU", "BITO", "UPRO", "MARA", "RIOT", "TNA",
                     "FNGU", "SPXL", "TSLL", "NVDL", "BITX", "CLSK", "PLTR", "SOFI",
                     "HOOD", "COIN", "AMD", "AFRM", "UPST", "TECL", "FAS", "NUGT"]
    WATCHLIST_SIZE = 12

    ALLOCATION_PCT = 0.98           # ~full capital
    TRAILING_STOP_PCT = 1.0         # 1% trailing stop (server-side at Alpaca)
    EMERGENCY_STOP_RATIO = 0.99     # -1% hard backup stop
    TIME_EXIT_MINUTES = 30
    TIME_EXIT_MIN_PL = -0.003
    TIME_EXIT_MAX_PL = 0.005

    MAX_SPREAD = 0.04               # dollars
    MIN_AVG_MINUTE_VOLUME = 5_000   # IEX feed only sees a slice of volume; raise if you get SIP
    MIN_ATR_PCT = 0.05
    IDEAL_ATR_PCT = 0.07
    SMA_FAST, SMA_SLOW = 9, 20
    VOLUME_AVG_PERIOD = 15
    VOLUME_SURGE_RATIO = 1.2
    PRICE_EXPANSION_BARS = 10
    PRICE_EXPANSION_MIN = 0.008

    NO_TRADE_FIRST_MINUTES = 5      # skip first 5 min after open
    NO_NEW_ENTRY_AFTER = (15, 30)   # no new entries after 15:30 ET
    FLATTEN_AT = (15, 55)           # close everything 15:55 ET (day trading)

    MAX_DAILY_LOSS_PCT = 0.02
    MAX_TRADES_PER_DAY = 10
    MAX_CONSECUTIVE_ERRORS = 3
    PAUSE_MINUTES_AFTER_ERRORS = 30

    GEMINI_MODEL = "gemini-3.8-flash"
    GEMINI_TIMEOUT_MS = 20_000
    GEMINI_RETRIES = 2
    GEMINI_FALLBACK_AFTER = 3
    SEND_UPDATE_EVERY_N_RUNS = 6
    MAX_GREENLIGHT_CHECKS_PER_RUN = 2

    TZ = ZoneInfo("America/New_York")
    MEMORY_PATH = "state/memory.json"


ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "").strip()
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")


# ============================ UTILS ============================
def et_now():
    return datetime.now(C.TZ)

def utc_now():
    return datetime.now(timezone.utc)

def hm(dt):
    return dt.hour * 60 + dt.minute

def market_open_now(now):
    if now.weekday() >= 5:
        return False
    return 9 * 60 + 30 <= hm(now) < 16 * 60

def sv(x):
    return str(getattr(x, "value", x)).lower()

def money(v):
    return f"${v:,.2f}"


# ============================ MEMORY ============================
def default_memory():
    return {
        "version": 1, "last_updated": None,
        "account": {"equity_at_open": None},
        "session": {"date": None, "run_count_today": 0, "gemini_failures_today": 0,
                    "gemini_fallback_active": False, "consecutive_errors": 0,
                    "trades_today": 0, "trading_paused_until": None,
                    "last_run": None, "watchlist_built_date": None},
        "position": None,
        "daily_stats": {"realized_pl": 0.0, "history": []},
        "watchlist": {"symbols": [], "blacklisted": [], "performance": {}},
        "gemini_memory": {"last_queries": [], "market_context_summary": "",
                          "strategic_recommendation": "", "sentiment_history": []},
        "errors": [], "trade_log": [],
    }

def _merge(base, new):
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base

def load_memory():
    mem = default_memory()
    try:
        with open(C.MEMORY_PATH) as f:
            _merge(mem, json.load(f))
    except FileNotFoundError:
        log.info("No memory file yet - starting fresh.")
    except Exception as e:
        log.error(f"Memory unreadable, starting fresh: {e}")
    return mem

def save_memory(mem):
    mem["last_updated"] = utc_now().isoformat()
    mem["errors"] = mem["errors"][-50:]
    mem["trade_log"] = mem["trade_log"][-200:]
    d = os.path.dirname(C.MEMORY_PATH) or "."
    os.makedirs(d, exist_ok=True)
    lock = open(C.MEMORY_PATH + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(mem, f, indent=2)
        os.replace(tmp, C.MEMORY_PATH)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

def reset_if_new_day(mem, today):
    s = mem["session"]
    if s["date"] == today:
        return
    if s["date"]:
        h = mem["daily_stats"]["history"]
        h.append({"date": s["date"], "realized_pl": mem["daily_stats"]["realized_pl"],
                  "trades": s["trades_today"]})
        mem["daily_stats"]["history"] = h[-30:]
    s.update(date=today, run_count_today=0, gemini_failures_today=0,
             gemini_fallback_active=False, trades_today=0, consecutive_errors=0)
    mem["daily_stats"]["realized_pl"] = 0.0
    mem["account"]["equity_at_open"] = None
    log.info(f"New trading day: {today}")

def log_trade(mem, symbol, action, price, qty, note=""):
    mem["trade_log"].append({"time": utc_now().isoformat(), "symbol": symbol,
                             "action": action, "price": price, "qty": qty, "note": note})


# ============================ MARKET DATA ============================
class MarketData:
    def __init__(self):
        self.client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)

    def minute_bars(self, sym, hours=3):
        try:
            req = StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Minute,
                                   start=utc_now() - timedelta(hours=hours),
                                   feed=DataFeed.IEX)
            return self.client.get_stock_bars(req).data.get(sym, [])
        except Exception as e:
            log.warning(f"minute bars {sym}: {e}")
            return []

    def daily_stats(self, sym, period=14):
        """returns (atr_pct, avg_daily_volume) or (0,0)"""
        try:
            req = StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Day,
                                   start=utc_now() - timedelta(days=45), feed=DataFeed.IEX)
            bars = self.client.get_stock_bars(req).data.get(sym, [])
            if len(bars) < period + 1:
                return 0.0, 0.0
            trs = []
            for i in range(1, len(bars)):
                h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
                trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            atr = sum(trs[-period:]) / period
            avgv = sum(b.volume for b in bars[-period:]) / period
            return atr / bars[-1].close, avgv
        except Exception as e:
            log.warning(f"daily stats {sym}: {e}")
            return 0.0, 0.0

    def quote(self, sym):
        """returns (bid, ask) or (0,0)"""
        try:
            snap = self.client.get_stock_snapshot(
                StockSnapshotRequest(symbol_or_symbols=[sym], feed=DataFeed.IEX))
            q = snap[sym].latest_quote
            return float(q.bid_price), float(q.ask_price)
        except Exception as e:
            log.warning(f"quote {sym}: {e}")
            return 0.0, 0.0


# ============================ GEMINI ============================
_ai = None
def ai_client():
    global _ai
    if _ai is None:
        from google import genai
        from google.genai import types
        _ai = genai.Client(api_key=GEMINI_KEY,
                           http_options=types.HttpOptions(timeout=C.GEMINI_TIMEOUT_MS))
    return _ai

def gemini(mem, prompt, label):
    """Returns text or None. Handles retries, backoff, failure counting, fallback."""
    s, g = mem["session"], mem["gemini_memory"]
    if not GEMINI_KEY:
        return None
    for attempt in range(C.GEMINI_RETRIES):
        try:
            r = ai_client().models.generate_content(model=C.GEMINI_MODEL, contents=prompt)
            text = (r.text or "").strip()
            if text:
                g["last_queries"].append({"time": utc_now().isoformat(), "label": label,
                                          "reply": text[:300]})
                g["last_queries"] = g["last_queries"][-10:]
                return text
        except Exception as e:
            log.warning(f"Gemini attempt {attempt + 1} failed: {e}")
        if attempt < C.GEMINI_RETRIES - 1:
            time.sleep(2 ** attempt)
    s["gemini_failures_today"] += 1
    log.warning(f"Gemini failures today: {s['gemini_failures_today']}")
    if s["gemini_failures_today"] >= C.GEMINI_FALLBACK_AFTER:
        s["gemini_fallback_active"] = True
        log.warning("Gemini FALLBACK ACTIVE - trading on technical signals only until tomorrow.")
    return None

def memory_context(mem):
    g = mem["gemini_memory"]
    p = mem["position"]
    pos = (f"{p['symbol']} qty {p['qty']} entry {p['entry_price']}" if p else "none")
    return (f"Current time (ET): {et_now():%Y-%m-%d %H:%M}\n"
            f"Current position: {pos}\n"
            f"Market context summary: {g['market_context_summary'] or 'n/a'}\n"
            f"Last strategic recommendation: {g['strategic_recommendation'] or 'n/a'}\n"
            f"Recent sentiment history: {json.dumps(g['sentiment_history'][-5:])}")

def get_greenlight(mem, sig):
    """True = allowed to enter."""
    s, g = mem["session"], mem["gemini_memory"]
    if s["gemini_fallback_active"]:
        log.info(f"[{sig['symbol']}] Gemini fallback active -> technical-only approval")
        return True
    prompt = (f"You are a strict risk-manager AI for a LONG-only day-trading bot.\n"
              f"{memory_context(mem)}\n"
              f"Candidate: {sig['symbol']} price {sig['price']:.2f}, SMA9 {sig['sma9']:.2f}, "
              f"volume ratio {sig['vol_ratio']:.2f}, 10-bar expansion {sig['expansion']*100:.2f}%, "
              f"ATR {sig['atr_pct']*100:.1f}%, spread ${sig['spread']:.3f}.\n"
              f"Is there any breaking catastrophic news, macro event, or sector-specific risk "
              f"making a LONG entry on {sig['symbol']} highly dangerous RIGHT NOW? "
              f"Respond STRICTLY with 'GREENLIGHT' or 'REJECT' followed by one sentence.")
    text = gemini(mem, prompt, f"greenlight {sig['symbol']}")
    if text is None:
        if s["gemini_fallback_active"]:
            return True
        return False
    ok = bool(re.match(r"\W*GREENLIGHT", text.upper()))
    g["sentiment_history"].append({"time": utc_now().isoformat(), "symbol": sig["symbol"],
                                   "verdict": "GREENLIGHT" if ok else "REJECT",
                                   "note": text[:150]})
    g["sentiment_history"] = g["sentiment_history"][-10:]
    log.info(f"[{sig['symbol']}] Gemini verdict: {text[:200]}")
    return ok

def periodic_update(mem):
    p = mem["position"]
    summary = (f"Position: {p['symbol']} entry {p['entry_price']} unrealized "
               f"{p.get('unrealized_pl_pct', 0):.2f}%" if p else "Position: none")
    prompt = (f"{memory_context(mem)}\n\nBot status report.\n{summary}\n"
              f"Realized P/L today: {mem['daily_stats']['realized_pl']:.2f}\n"
              f"Trades today: {mem['session']['trades_today']}\n"
              f"Watchlist (ranked): {mem['watchlist']['symbols']}\n"
              f"In 3 short sentences: (1) market context summary, (2) one strategic "
              f"recommendation for this day-trading strategy. Label them CONTEXT: and ADVICE:")
    text = gemini(mem, prompt, "periodic_update")
    if text:
        m = re.search(r"CONTEXT:(.*?)(ADVICE:|$)", text, re.S | re.I)
        a = re.search(r"ADVICE:(.*)", text, re.S | re.I)
        if m: mem["gemini_memory"]["market_context_summary"] = m.group(1).strip()[:500]
        if a: mem["gemini_memory"]["strategic_recommendation"] = a.group(1).strip()[:500]
        log.info("Gemini periodic update stored in memory.")


# ============================ WATCHLIST (pre-market) ============================
def build_watchlist(mem, md):
    log.info("Building today's watchlist...")
    universe = [s for s in C.BASE_UNIVERSE if s not in mem["watchlist"]["blacklisted"]]

    # AI may ADD tickers (STORY step 3)
    text = gemini(mem, f"{memory_context(mem)}\nSuggest up to 3 additional highly liquid, volatile "
                       f"US stocks/ETFs (no penny stocks) with bullish momentum or catalysts today. "
                       f"Reply ONLY with tickers separated by commas.", "watchlist_add")
    if text:
        for t in re.findall(r"\b[A-Z]{1,5}\b", text.upper()):
            if t not in universe and t not in mem["watchlist"]["blacklisted"] and len(universe) < 40:
                universe.append(t)
        log.info(f"AI suggested extras, universe size now {len(universe)}")

    ranked = []
    for sym in universe:
        atr, vol = md.daily_stats(sym)
        if atr <= 0 or vol <= 0:
            continue
        ranked.append((min(atr, C.IDEAL_ATR_PCT), vol, sym, atr))
    ranked.sort(reverse=True)               # best ATR (capped at ideal) then volume
    top = ranked[:C.WATCHLIST_SIZE]
    mem["watchlist"]["symbols"] = [r[2] for r in top]
    mem["watchlist"]["performance"] = {r[2]: {"atr_pct": round(r[3], 4), "avg_vol": int(r[1])}
                                       for r in top}
    mem["session"]["watchlist_built_date"] = mem["session"]["date"]
    log.info("Watchlist (ranked): " + ", ".join(f"{r[2]}({r[3]*100:.1f}%)" for r in top))


# ============================ SIGNALS ============================
def evaluate(mem, md, sym):
    """Returns signal dict if ALL entry requirements pass, else None."""
    bars = md.minute_bars(sym)
    if len(bars) < max(C.VOLUME_AVG_PERIOD, C.SMA_SLOW, C.PRICE_EXPANSION_BARS) + 1:
        log.info(f"[{sym}] skip: not enough bars ({len(bars)})")
        return None
    closes = [b.close for b in bars]
    vols = [b.volume for b in bars]
    price = closes[-1]
    sma9 = sum(closes[-C.SMA_FAST:]) / C.SMA_FAST
    sma20 = sum(closes[-C.SMA_SLOW:]) / C.SMA_SLOW
    avgv = sum(vols[-C.VOLUME_AVG_PERIOD:]) / C.VOLUME_AVG_PERIOD
    cur_v = vols[-1]
    last10 = closes[-C.PRICE_EXPANSION_BARS:]
    expansion = (max(last10) - min(last10)) / min(last10)
    bid, ask = md.quote(sym)
    spread = ask - bid
    atr_pct = mem["watchlist"]["performance"].get(sym, {}).get("atr_pct") or md.daily_stats(sym)[0]

    fails = []
    if not price > sma9: fails.append(f"price {price:.2f} <= SMA9 {sma9:.2f}")
    if not cur_v > avgv * C.VOLUME_SURGE_RATIO: fails.append(f"volume {cur_v:.0f} <= {C.VOLUME_SURGE_RATIO}x avg {avgv:.0f}")
    if not expansion >= C.PRICE_EXPANSION_MIN: fails.append(f"expansion {expansion*100:.2f}% too small")
    if not (ask > 0 and spread <= C.MAX_SPREAD): fails.append(f"spread ${spread:.3f} too wide / no quote")
    if not avgv >= C.MIN_AVG_MINUTE_VOLUME: fails.append(f"avg volume {avgv:.0f} too low")
    if fails:
        log.info(f"[{sym}] skip: " + "; ".join(fails))
        return None

    # Setup tier: 1 = trending up + volume, 2 = starting to go up + volume, 3 = volatile + volume
    if price > sma9 > sma20:
        tier = 1
    elif price > sma9 and closes[-1] > closes[-6] and sma9 >= sum(closes[-C.SMA_FAST - 1:-1]) / C.SMA_FAST:
        tier = 2
    else:
        tier = 3
    log.info(f"[{sym}] PASSED entry conditions | tier {tier} | ATR {atr_pct*100:.1f}% | spread ${spread:.3f}")
    return {"symbol": sym, "tier": tier, "price": price, "sma9": sma9, "vol_ratio": cur_v / avgv,
            "expansion": expansion, "spread": spread, "ask": ask, "atr_pct": atr_pct}


# ============================ ORDERS ============================
def open_entry(mem, trading, sig):
    sym = sig["symbol"]
    acct = trading.get_account()
    # min(cash, buying_power) so a margin paper account never uses leverage
    power = min(float(acct.buying_power), float(acct.cash))
    qty = int((power * C.ALLOCATION_PCT) // sig["ask"])
    if qty < 1:
        log.info(f"[{sym}] cannot afford 1 share (power {money(power)}, ask {sig['ask']})")
        return False
    log.info(f"ENTRY: BUY {qty} {sym} limit {sig['ask']:.2f}")
    order = trading.submit_order(LimitOrderRequest(
        symbol=sym, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
        limit_price=round(sig["ask"], 2)))
    o = order
    for _ in range(10):                      # wait up to ~30s for the fill
        time.sleep(3)
        o = trading.get_order_by_id(order.id)
        if sv(o.status) == "filled":
            break
    if sv(o.status) != "filled":
        try:
            trading.cancel_order_by_id(order.id)
        except Exception as e:
            log.warning(f"cancel entry: {e}")
        time.sleep(1)
        o = trading.get_order_by_id(order.id)
    filled = int(float(o.filled_qty or 0))
    if filled < 1:
        log.info(f"[{sym}] entry not filled - cancelled, will retry next cycle")
        return False
    fill_price = float(o.filled_avg_price or sig["ask"])
    trading.submit_order(TrailingStopOrderRequest(
        symbol=sym, qty=filled, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
        trail_percent=C.TRAILING_STOP_PCT))
    log.info(f"FILLED {filled} {sym} @ {fill_price:.2f} | {C.TRAILING_STOP_PCT}% trailing stop placed")
    mem["position"] = {"symbol": sym, "qty": filled, "entry_price": fill_price,
                       "entry_time": utc_now().isoformat(), "highest_price_since_entry": fill_price,
                       "has_trailing_stop": True, "unrealized_pl_pct": 0.0, "tier": sig["tier"]}
    mem["session"]["trades_today"] += 1
    log_trade(mem, sym, "BUY", fill_price, filled, f"tier {sig['tier']}")
    return True

def exit_position(mem, trading, reason, price):
    p = mem["position"]
    sym = p["symbol"]
    log.info(f"EXIT {sym}: {reason}")
    try:
        trading.cancel_orders()               # free the shares held by the trailing stop
        time.sleep(1)
    except Exception as e:
        log.warning(f"cancel orders: {e}")
    trading.close_position(sym)
    pl = (price - p["entry_price"]) * p["qty"]
    mem["daily_stats"]["realized_pl"] += pl
    log_trade(mem, sym, "SELL", price, p["qty"], reason)
    perf = mem["watchlist"]["performance"].setdefault(sym, {})
    perf["last_pl"] = round(pl, 2)
    mem["position"] = None

def reconcile(mem, trading):
    """Sync memory with Alpaca (position closed by trailing stop / adopted manually)."""
    positions = trading.get_all_positions()
    p = mem["position"]
    if p and not positions:
        # closed by the server-side trailing stop
        exit_price = p.get("highest_price_since_entry", p["entry_price"]) * (1 - C.TRAILING_STOP_PCT / 100)
        try:
            orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED,
                                                         symbols=[p["symbol"]], limit=10))
            for o in orders:
                if "sell" in sv(o.side) and o.filled_avg_price:
                    exit_price = float(o.filled_avg_price)
                    break
        except Exception as e:
            log.warning(f"reconcile orders: {e}")
        pl = (exit_price - p["entry_price"]) * p["qty"]
        mem["daily_stats"]["realized_pl"] += pl
        log_trade(mem, p["symbol"], "SELL", exit_price, p["qty"], "trailing stop / external close")
        log.info(f"Position {p['symbol']} closed by stop. Realized ~{money(pl)}")
        mem["position"] = None
    elif positions and not mem["position"]:
        pos = positions[0]
        log.info(f"Adopting untracked position {pos.symbol}")
        mem["position"] = {"symbol": pos.symbol, "qty": int(float(pos.qty)),
                           "entry_price": float(pos.avg_entry_price),
                           "entry_time": utc_now().isoformat(),
                           "highest_price_since_entry": float(pos.avg_entry_price),
                           "has_trailing_stop": False, "unrealized_pl_pct": 0.0, "tier": 0}
    return positions

def has_trailing_stop(trading, sym):
    orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[sym]))
    return any("trailing" in sv(o.order_type) for o in orders)

def manage_position(mem, trading, positions, now):
    p = mem["position"]
    pos = next((x for x in positions if x.symbol == p["symbol"]), positions[0])
    price = float(pos.current_price)
    entry = p["entry_price"]
    p["highest_price_since_entry"] = max(p.get("highest_price_since_entry", entry), price)
    pl_pct = (price - entry) / entry
    p["unrealized_pl_pct"] = round(pl_pct * 100, 3)
    held_min = (utc_now() - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
    log.info(f"POSITION {p['symbol']} | entry {entry:.2f} | now {price:.2f} | "
             f"P/L {pl_pct*100:.2f}% | held {held_min:.0f} min | high {p['highest_price_since_entry']:.2f}")

    if price <= entry * C.EMERGENCY_STOP_RATIO:
        return exit_position(mem, trading, "EMERGENCY STOP -1%", price)
    if hm(now) >= C.FLATTEN_AT[0] * 60 + C.FLATTEN_AT[1]:
        return exit_position(mem, trading, "END-OF-DAY FLATTEN", price)
    if held_min > C.TIME_EXIT_MINUTES and C.TIME_EXIT_MIN_PL <= pl_pct <= C.TIME_EXIT_MAX_PL:
        return exit_position(mem, trading, f"TIME EXIT ({held_min:.0f} min, no movement)", price)
    if not has_trailing_stop(trading, p["symbol"]):
        log.info("Trailing stop missing -> placing it now")
        trading.submit_order(TrailingStopOrderRequest(
            symbol=p["symbol"], qty=p["qty"], side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY, trail_percent=C.TRAILING_STOP_PCT))
    p["has_trailing_stop"] = True


# ============================ RISK ============================
def circuit_breakers(mem, now):
    """(can_open_new_trade, reason)"""
    s = mem["session"]
    if not market_open_now(now):
        return False, "market closed"
    if hm(now) < 9 * 60 + 30 + C.NO_TRADE_FIRST_MINUTES:
        return False, f"first {C.NO_TRADE_FIRST_MINUTES} min of the session"
    if hm(now) >= C.NO_NEW_ENTRY_AFTER[0] * 60 + C.NO_NEW_ENTRY_AFTER[1]:
        return False, "too late in the day for new entries"
    eq = mem["account"]["equity_at_open"]
    if eq and mem["daily_stats"]["realized_pl"] < -C.MAX_DAILY_LOSS_PCT * eq:
        return False, f"daily loss limit hit ({mem['daily_stats']['realized_pl']:.2f})"
    if s["trades_today"] >= C.MAX_TRADES_PER_DAY:
        return False, "max trades per day"
    if s["consecutive_errors"] >= C.MAX_CONSECUTIVE_ERRORS:
        return False, "too many consecutive errors"
    if s["trading_paused_until"] and utc_now() < datetime.fromisoformat(s["trading_paused_until"]):
        return False, f"paused until {s['trading_paused_until']}"
    return True, "ok"


# ============================ MONITOR (Bot2) ============================
def health_check(mem):
    issues, recs, score = [], [], 100
    s = mem["session"]
    p = mem["position"]
    if p:
        held = (utc_now() - datetime.fromisoformat(p["entry_time"])).total_seconds() / 3600
        if held > 2:
            issues.append(f"Position held {held:.1f}h (too long for a day trade)"); score -= 25
            recs.append("Check the trailing stop and force an exit.")
    if s["gemini_failures_today"] > 5:
        issues.append("Gemini failures > 5 today"); score -= 15; recs.append("Check Gemini key/quota.")
    eq = mem["account"]["equity_at_open"]
    if eq and mem["daily_stats"]["realized_pl"] < -0.05 * eq:
        issues.append("Severe drawdown (< -5%)"); score -= 40; recs.append("Stop trading and review strategy.")
    if s["last_run"]:
        age = (utc_now() - datetime.fromisoformat(s["last_run"])).total_seconds() / 60
        if market_open_now(et_now()) and age > 15:
            issues.append(f"Bot stuck: last run {age:.0f} min ago"); score -= 30
            recs.append("Check GitHub Actions for failed/skipped runs.")
    if len(mem["errors"]) > 10:
        issues.append(f"{len(mem['errors'])} errors logged"); score -= 15; recs.append("Read memory.json errors.")
    today = s["date"]
    buys = [t for t in mem["trade_log"] if t["action"] == "BUY" and t["time"][:10] == (today or "")]
    if len(buys) > 3 and len({t["symbol"] for t in buys[-4:]}) == 1:
        issues.append("Churn: same symbol entered > 3 times"); score -= 20
        recs.append("Consider blacklisting the symbol.")
    score = max(score, 0)
    status = "HEALTHY" if score >= 80 else "WARNING" if score >= 50 else "CRITICAL"
    return {"status": status, "score": score, "issues": issues, "recommendations": recs,
            "timestamp": utc_now().isoformat()}

def print_report(mem):
    h = health_check(mem)
    p = mem["position"]
    print("\n===== BOT2 HEALTH REPORT =====")
    print(f"Status: {h['status']} ({h['score']}/100)")
    print(f"Position: {p['symbol'] + ' ' + str(p['qty']) + ' @ ' + str(p['entry_price']) if p else 'none'}")
    print(f"Realized P/L today: {mem['daily_stats']['realized_pl']:.2f} | "
          f"Trades: {mem['session']['trades_today']} | Runs: {mem['session']['run_count_today']}")
    print(f"Gemini failures: {mem['session']['gemini_failures_today']} "
          f"(fallback: {mem['session']['gemini_fallback_active']})")
    for i in h["issues"]: print(f" - ISSUE: {i}")
    for r in h["recommendations"]: print(f" - FIX: {r}")
    print("==============================\n")


# ============================ MAIN ============================
def run():
    mem = load_memory()
    now = et_now()
    s = mem["session"]
    try:
        if not ALPACA_KEY or not ALPACA_SECRET:
            raise ValueError("Missing Alpaca credentials")
        reset_if_new_day(mem, now.strftime("%Y-%m-%d"))
        trading = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=C.PAPER_TRADING)
        md = MarketData()
        log.info(f"Run start | {now:%a %H:%M} ET | paper={C.PAPER_TRADING}")

        # Daily pre-market watchlist
        if now.weekday() < 5 and (s["watchlist_built_date"] != s["date"]
                                  or not mem["watchlist"]["symbols"]):
            build_watchlist(mem, md)

        if not market_open_now(now):
            log.info("Market closed - nothing else to do.")
        else:
            if mem["account"]["equity_at_open"] is None:
                mem["account"]["equity_at_open"] = float(trading.get_account().equity)
                log.info(f"Equity at open: {money(mem['account']['equity_at_open'])}")

            positions = reconcile(mem, trading)
            if mem["position"]:
                manage_position(mem, trading, positions, now)
            else:
                ok, why = circuit_breakers(mem, now)
                if not ok:
                    log.info(f"No new entries: {why}")
                else:
                    log.info("No position. Scanning ranked watchlist...")
                    cands = []
                    for sym in mem["watchlist"]["symbols"]:
                        if sym in mem["watchlist"]["blacklisted"]:
                            continue
                        sig = evaluate(mem, md, sym)
                        if sig: cands.append(sig)
                    cands.sort(key=lambda x: (x["tier"], -x["atr_pct"], x["spread"]))
                    if not cands:
                        log.info("No symbol met the entry conditions this cycle.")
                    checks = 0
                    for sig in cands:
                        if checks >= C.MAX_GREENLIGHT_CHECKS_PER_RUN:
                            break
                        checks += 1
                        if get_greenlight(mem, sig):
                            if open_entry(mem, trading, sig):
                                break
                        else:
                            log.info(f"[{sig['symbol']}] entry blocked by AI greenlight")

            if s["run_count_today"] % C.SEND_UPDATE_EVERY_N_RUNS == 0:
                periodic_update(mem)

        s["consecutive_errors"] = 0
    except Exception as e:
        log.exception(f"Run error: {e}")
        s["consecutive_errors"] += 1
        mem["errors"].append({"time": utc_now().isoformat(), "error": str(e)[:300]})
        if s["consecutive_errors"] >= C.MAX_CONSECUTIVE_ERRORS:
            s["trading_paused_until"] = (utc_now() + timedelta(minutes=C.PAUSE_MINUTES_AFTER_ERRORS)).isoformat()
            s["consecutive_errors"] = 0
            log.error(f"Too many errors - new entries paused until {s['trading_paused_until']}")
    finally:
        s["run_count_today"] += 1
        s["last_run"] = utc_now().isoformat()
        try:
            save_memory(mem)
        except Exception as e:
            log.error(f"Could not save memory: {e}")
        print_report(mem)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "monitor":
        print_report(load_memory())
    else:
        run()
    sys.exit(0)   # never crash the workflow
