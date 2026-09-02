import os
import time
from datetime import datetime, timezone, timedelta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, TrailingStopOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# API Credentials fetched securely from GitHub Repository Secrets
API_KEY = os.environ.get("ALPACA_API_KEY", "PKYKCQOK5SHSZO365FNZWBVE3K")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "2M26NEWpkHFq6Q3GB26uuDzvawhECVaUXPVNHxvnGFik")

if not API_KEY or not SECRET_KEY:
    raise ValueError("Missing ALPACA_API_KEY or ALPACA_SECRET_KEY environment variables.")

# Account Allocation & Strategy Constants
BUCKET_ALLOCATION_PCT = 0.50   # Allocates 50% of total equity per bucket ($250 on a $500 balance, auto-compounding)
MAX_SPREAD = 0.04              # Max bid-ask spread to control entry slippage
TRAILING_STOP_PCT = 1.0        # 1.0% Trailing Stop
EMERGENCY_STOP_RATIO = 0.99    # 1.0% Hard Emergency Drop Threshold
STALL_TIMEOUT_SEC = 600        # 10 Minutes stall window
STALL_MIN_GAIN_PCT = 0.004     # Must be up at least +0.4% or position is liquidated

# Watchlist: Volatile Mid-Caps & Leveraged ETFs targeting ~7% ATR conditions
WATCHLIST = ["TQQQ", "SOXL", "LABU", "BITO", "UPRO"]

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

def check_autonomous_entry_signal(symbol):
    """
    Purely Mathematical Entry Engine (No Gemini API):
    1. Volatility Expansion: Checks for a minimum 0.8% price expansion across recent 1-min bars.
    2. Trend Alignment: Price must be trading above the 9-period SMA.
    3. Volume Surge: Minute bar volume must exceed 1.2x the 15-period average volume.
    """
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
        print(f"Signal Evaluation Error on {symbol}: {e}")
    return False

print("Autonomous Trading Engine Initialized...")

clock = trading_client.get_clock()
if not clock.is_open:
    print("Market is currently closed. Exiting script execution.")
    exit(0)

start_run_time = time.time()
entry_time = None
active_symbol = None

# Continuous execution loop during GitHub Actions runner window
while time.time() - start_run_time < 1500:
    try:
        positions = trading_client.get_all_positions()

        # Monitor Active Open Position
        if len(positions) > 0:
            pos = positions[0]
            active_symbol = pos.symbol
            current_price = float(pos.current_price)
            avg_entry = float(pos.avg_entry_price)
            unrealized_pl_pct = (current_price - avg_entry) / avg_entry

            if entry_time is None:
                entry_time = time.time()

            # 1. Emergency Hard Exit (-1.0%)
            if current_price <= (avg_entry * EMERGENCY_STOP_RATIO):
                print(f"EMERGENCY CONTINGENCY: -1.0% drop detected on {active_symbol}. Liquidating immediately...")
                trading_client.close_all_positions(cancel_orders=True)
                entry_time = None
                time.sleep(10)
                continue

            # 2. Time-Based Stall Exit
            elapsed = time.time() - entry_time
            if elapsed > STALL_TIMEOUT_SEC and unrealized_pl_pct < STALL_MIN_GAIN_PCT:
                print(f"STALL EXIT: {active_symbol} flat for {int(elapsed/60)} mins. Liquidating position...")
                trading_client.close_all_positions(cancel_orders=True)
                entry_time = None
                time.sleep(10)
                continue

            print(f"Active Position: {active_symbol} | Price: ${current_price:.2f} | Entry: ${avg_entry:.2f} | P/L: {unrealized_pl_pct*100:.2f}%")
            time.sleep(15)
            continue

        # Scan Watchlist for Autonomous Entry Signals
        for symbol in WATCHLIST:
            req = StockSnapshotRequest(symbol_or_symbols=[symbol])
            snapshot = data_client.get_stock_snapshot(req)
            quote = snapshot[symbol].latest_quote
            spread = quote.ask_price - quote.bid_price

            if spread <= MAX_SPREAD and check_autonomous_entry_signal(symbol):
                account = trading_client.get_account()
                equity = float(account.equity)
                
                # Dynamic Sizing: 50% of account balance per allocation bucket ($250 on $500, auto-compounding)
                trade_allocation = equity * BUCKET_ALLOCATION_PCT
                qty_to_buy = int(trade_allocation // quote.ask_price)

                if qty_to_buy <= 0:
                    print(f"Insufficient funds to open trade bucket for {symbol}.")
                    continue

                print(f"AUTONOMOUS SIGNAL: Buying {qty_to_buy} shares of {symbol} at ${quote.ask_price:.2f}")

                # Limit Entry at Ask to control slippage
                entry_order = LimitOrderRequest(
                    symbol=symbol,
                    qty=qty_to_buy,
                    limit_price=quote.ask_price,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY
                )
                trading_client.submit_order(entry_order)
                time.sleep(3)

                # Submit 1.0% Trailing Stop Order
                trail_order = TrailingStopOrderRequest(
                    symbol=symbol,
                    qty=qty_to_buy,
                    side=OrderSide.SELL,
                    trail_percent=TRAILING_STOP_PCT,
                    time_in_force=TimeInForce.DAY
                )
                trading_client.submit_order(trail_order)
                print(f"1.0% Trailing Stop deployed for {symbol}.")
                entry_time = time.time()
                break

        time.sleep(10)

    except Exception as e:
        print(f"Execution Error: {e}")
        time.sleep(10)
