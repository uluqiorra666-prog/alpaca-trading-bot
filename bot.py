import os
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest,
    TrailingStopOrderRequest,
    GetOrdersRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from google import genai

# API Credentials
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

if not ALPACA_API_KEY or not GEMINI_API_KEY:
    raise ValueError("Missing Alpaca or Gemini API credentials.")

# Strategy Parameters
BUCKET_ALLOCATION_PCT = 0.50   # 50% account allocation per trade[cite: 1]
MAX_SPREAD = 0.04              # Maximum bid-ask spread[cite: 1]
TRAILING_STOP_PCT = 1.0        # 1.0% Trailing Stop[cite: 1]
EMERGENCY_STOP_RATIO = 0.99    # -1.0% Hard Emergency Drop[cite: 1]

WATCHLIST = ["TQQQ", "SOXL", "LABU", "BITO", "UPRO", "MARA", "RIOT"]

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)[cite: 1]
data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)[cite: 1]
ai_client = genai.Client(api_key=GEMINI_API_KEY)

def is_us_market_open():
    """Calculates US market open status offline using New York time."""
    eastern = ZoneInfo("America/New_York")
    now = datetime.now(eastern)

    # Check Weekend (5 = Saturday, 6 = Sunday)
    if now.weekday() >= 5:
        return False

    # Check Trading Hours (9:30 AM to 4:00 PM ET)
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)

    return market_open <= now <= market_close

# Local Market Hours Check
if not is_us_market_open():
    print("Market is closed (Outside 9:30 AM - 4:00 PM ET or Weekend). Exiting script.")
    exit(0)

print("US Market is OPEN. Proceeding with execution sequence...")

def ask_gemini_greenlight(symbol):
    try:
        prompt = (
            f"You are a strict risk-manager AI. Assess current market conditions for {symbol}. "
            "Is there any breaking catastrophic news or macro events making going LONG highly dangerous right now? "
            "Respond strictly with 'GREENLIGHT' or 'REJECT', followed by a one-sentence reason."
        )
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        verdict = response.text.strip().upper()
        print(f"[{symbol} AI SENTIMENT]: {verdict}")
        return "GREENLIGHT" in verdict
    except Exception as e:
        print(f"Gemini API error on {symbol}: {e}")
        return False

def check_snapshot_entry_signal(symbol):
    try:
        now = datetime.now(timezone.utc)
        bars_req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=now - timedelta(hours=2)
        )
        bars = data_client.get_stock_bars(bars_req)[symbol]
        if len(bars) < 15:
            return False

        closes = [b.close for b in bars]
        volumes = [b.volume for b in bars]

        current_price = closes[-1]
        sma_9 = sum(closes[-9:]) / 9
        avg_vol_15 = sum(volumes[-15:]) / 15
        current_vol = volumes[-1]

        price_expansion_pct = (max(closes[-10:]) - min(closes[-10:])) / min(closes[-10:])

        if current_price > sma_9 and current_vol > (avg_vol_15 * 1.2) and price_expansion_pct >= 0.008:
            return True
    except Exception as e:
        print(f"Error evaluating bars for {symbol}: {e}")
    return False

# 1. Existing Position Management
positions = trading_client.get_all_positions()[cite: 1]

if len(positions) > 0:
    pos = positions[0][cite: 1]
    symbol = pos.symbol[cite: 1]
    qty = float(pos.qty)
    current_price = float(pos.current_price)[cite: 1]
    avg_entry = float(pos.avg_entry_price)[cite: 1]
    unrealized_pl_pct = (current_price - avg_entry) / avg_entry

    print(f"ACTIVE TRADE: {symbol} | Entry: ${avg_entry:.2f} | Current: ${current_price:.2f} | P/L: {unrealized_pl_pct*100:.2f}%")

    order_params = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
    open_orders = trading_client.get_orders(filter=order_params)
    has_trailing_stop = any(o.order_type == "trailing_stop" for o in open_orders)

    if not has_trailing_stop:
        print(f"Deploying missing 1.0% trailing stop to {symbol}...")
        trail_order = TrailingStopOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL, trail_percent=TRAILING_STOP_PCT, time_in_force=TimeInForce.DAY[cite: 1]
        )
        trading_client.submit_order(trail_order)[cite: 1]

    # Hard Emergency Stop Override (-1.0%)
    if current_price <= (avg_entry * EMERGENCY_STOP_RATIO):[cite: 1]
        print(f"EMERGENCY LIMIT BREACH: {symbol} hit -1.0%. Liquidating immediately...")[cite: 1]
        trading_client.close_all_positions(cancel_orders=True)[cite: 1]
else:
    # 2. Scanning & Trade Execution
    print("No open positions. Scanning watchlist...")
    
    for symbol in WATCHLIST:
        req = StockSnapshotRequest(symbol_or_symbols=[symbol])[cite: 1]
        snapshot = data_client.get_stock_snapshot(req)[cite: 1]
        quote = snapshot[symbol].latest_quote[cite: 1]
        spread = quote.ask_price - quote.bid_price[cite: 1]

        if spread <= MAX_SPREAD and check_snapshot_entry_signal(symbol):[cite: 1]
            if ask_gemini_greenlight(symbol):[cite: 1]
                account = trading_client.get_account()[cite: 1]
                trade_allocation = float(account.equity) * BUCKET_ALLOCATION_PCT[cite: 1]
                qty_to_buy = int(trade_allocation // quote.ask_price)

                if qty_to_buy <= 0:
                    continue

                print(f"AI APPROVED: Executing LONG {qty_to_buy} shares of {symbol} at ${quote.ask_price:.2f}")

                entry_order = LimitOrderRequest(
                    symbol=symbol, qty=qty_to_buy, limit_price=quote.ask_price, side=OrderSide.BUY, time_in_force=TimeInForce.DAY[cite: 1]
                )
                trading_client.submit_order(entry_order)[cite: 1]
                time.sleep(3)[cite: 1]

                trail_order = TrailingStopOrderRequest(
                    symbol=symbol, qty=qty_to_buy, side=OrderSide.SELL, trail_percent=TRAILING_STOP_PCT, time_in_force=TimeInForce.DAY[cite: 1]
                )
                trading_client.submit_order(trail_order)[cite: 1]
                break

print("Bot execution cycle completed.")
