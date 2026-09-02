import os
import time
from datetime import datetime, timezone, timedelta
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

# API Credentials fetched securely from environment variables
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "PKYKCQOK5SHSZO365FNZWBVE3K")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "2M26NEWpkHFq6Q3GB26uuDzvawhECVaUXPVNHxvnGFik")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6JP0O-Kr0OMUDLCOhGXEtUu-1L8U57KaM6meMBesO85ig")

if not ALPACA_API_KEY or not GEMINI_API_KEY:
    raise ValueError("Missing Alpaca or Gemini API credentials.")

# Strategy & Account Constants
BUCKET_ALLOCATION_PCT = 0.50   # 50% equity (compounding $250 buckets on a $500 balance)
MAX_SPREAD = 0.04              # Maximum allowed spread to prevent slippage
TRAILING_STOP_PCT = 1.0        # 1.0% Trailing Stop
EMERGENCY_STOP_RATIO = 0.99    # -1.0% Hard Emergency Drop
STALL_TIMEOUT_SEC = 600        # 10 Minutes stall threshold
STALL_MIN_GAIN_PCT = 0.004     # Must be +0.4% minimum gain after stall timeout

# Target Watchlist (Volatile Mid-Caps & Leveraged ETFs matching ~7% ATR target)
WATCHLIST = ["TQQQ", "SOXL", "LABU", "BITO", "UPRO", "MARA", "RIOT"]

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

def ask_gemini_greenlight(symbol):
    """
    Semi-Live AI Guardrail: Checks market sentiment before entry.
    Prevents buying into chaotic breaking news or macro events.
    """
    try:
        prompt = (
            f"You are a strict risk-manager AI. Assess the current market sentiment for the ticker {symbol}. "
            "Is there any breaking catastrophic news, pending Fed rate announcements, or chaotic macro events "
            "that make going LONG highly dangerous right now? "
            "Respond strictly with either 'GREENLIGHT' or 'REJECT', followed by a one-sentence reason."
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
    """Purely Technical Entry Engine for Volatile Momentum."""
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
        print(f"Error evaluating bar snapshot for {symbol}: {e}")
    return False

print("Starting AI-Enhanced Execution Sequence...")

clock = trading_client.get_clock()
if not clock.is_open:
    print("Market is closed. Exiting script.")
    exit(0)

# 1. Existing Position Management
positions = trading_client.get_all_positions()

if len(positions) > 0:
    pos = positions[0]
    symbol = pos.symbol
    qty = float(pos.qty)
    current_price = float(pos.current_price)
    avg_entry = float(pos.avg_entry_price)
    unrealized_pl_pct = (current_price - avg_entry) / avg_entry

    print(f"ACTIVE TRADE: {symbol} | Entry: ${avg_entry:.2f} | Current: ${current_price:.2f} | P/L: {unrealized_pl_pct*100:.2f}%")

    # Verify native Alpaca 1.0% trailing stop is attached
    order_params = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
    open_orders = trading_client.get_orders(filter=order_params)
    has_trailing_stop = any(o.order_type == "trailing_stop" for o in open_orders)

    if not has_trailing_stop:
        print(f"Deploying missing 1.0% trailing stop to {symbol}...")
        trail_order = TrailingStopOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL, trail_percent=TRAILING_STOP_PCT, time_in_force=TimeInForce.DAY
        )
        trading_client.submit_order(trail_order)

    # 1.0% Hard Emergency Stop Override
    if current_price <= (avg_entry * EMERGENCY_STOP_RATIO):
        print(f"EMERGENCY LIMIT BREACH: {symbol} hit -1.0%. Liquidating immediately...")
        trading_client.close_all_positions(cancel_orders=True)
else:
    # 2. Scanning & AI Greenlight Execution
    print("No open positions. Scanning watchlist...")
    
    for symbol in WATCHLIST:
        req = StockSnapshotRequest(symbol_or_symbols=[symbol])
        snapshot = data_client.get_stock_snapshot(req)
        quote = snapshot[symbol].latest_quote
        spread = quote.ask_price - quote.bid_price

        # Technical check -> Spread filter -> Gemini AI check
        if spread <= MAX_SPREAD and check_snapshot_entry_signal(symbol):
            if ask_gemini_greenlight(symbol):
                
                account = trading_client.get_account()
                trade_allocation = float(account.equity) * BUCKET_ALLOCATION_PCT
                qty_to_buy = int(trade_allocation // quote.ask_price)

                if qty_to_buy <= 0:
                    continue

                print(f"AI APPROVED: Executing LONG {qty_to_buy} shares of {symbol} at ${quote.ask_price:.2f}")

                # Limit Entry Execution
                entry_order = LimitOrderRequest(
                    symbol=symbol, qty=qty_to_buy, limit_price=quote.ask_price, side=OrderSide.BUY, time_in_force=TimeInForce.DAY
                )
                trading_client.submit_order(entry_order)
                time.sleep(3)

                # Server-Side 1.0% Trailing Stop
                trail_order = TrailingStopOrderRequest(
                    symbol=symbol, qty=qty_to_buy, side=OrderSide.SELL, trail_percent=TRAILING_STOP_PCT, time_in_force=TimeInForce.DAY
                )
                trading_client.submit_order(trail_order)
                break

print("Bot execution cycle completed.")
