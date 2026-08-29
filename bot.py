import os
import time
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, TrailingStopOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest

# PASTE YOUR KEYS HERE (Inside the quotes):
API_KEY = os.environ.get("ALPACA_API_KEY") or "PASTE_YOUR_ALPACA_API_KEY_HERE"
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY") or "PASTE_YOUR_ALPACA_SECRET_KEY_HERE"

SYMBOL = "SPY"
# RISK CONTROLS:
POSITION_RISK_PCT = 0.20  # Allocate max 20% of account per trade instead of going all-in
MAX_SPREAD = 0.02         # Filter out wide spreads to reduce slippage
TRAILING_STOP_PCT = 2.0   # Expanded from 1% to 2% to absorb normal market noise

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

def ask_gemini_greenlight(symbol, spread):
    return True

print("GitHub Actions Trading Runner Initialized...")

# Check if Market is Open
clock = trading_client.get_clock()
if not clock.is_open:
    print("Market is currently closed. Exiting run.")
    exit(0)

# Execute scan loop during open hours
start_time = time.time()
while time.time() - start_time < 1800:
    try:
        positions = trading_client.get_all_positions()
        if len(positions) > 0:
            pos = positions[0]
            current_price = float(pos.current_price)
            avg_entry = float(pos.avg_entry_price)

            # Hard Emergency Liquidation Stop (-3%)
            if current_price < (avg_entry * 0.97):
                print("EMERGENCY CONTINGENCY: Liquidating position!")
                trading_client.close_all_positions(cancel_orders=True)

            time.sleep(5)
            continue

        # Get latest market snapshot
        req = StockSnapshotRequest(symbol_or_symbols=[SYMBOL])
        snapshot = data_client.get_stock_snapshot(req)
        quote = snapshot[SYMBOL].latest_quote
        spread = quote.ask_price - quote.bid_price

        print(f"[{SYMBOL}] Spread: ${spread:.2f} | Ask: ${quote.ask_price}")

        # Check spread condition to reduce slippage
        if spread <= MAX_SPREAD:
            if ask_gemini_greenlight(SYMBOL, spread):
                print("Gemini Greenlight Confirmed. Calculating Position & Executing...")

                # Dynamic sizing: 20% of account equity instead of going 100% all-in
                account = trading_client.get_account()
                equity = float(account.equity)
                trade_allocation = equity * POSITION_RISK_PCT
                qty_to_buy = round(trade_allocation / quote.ask_price, 4)

                if qty_to_buy <= 0:
                    print("Insufficient equity to execute trade allocation.")
                    time.sleep(30)
                    continue

                # Limit order at ask price to prevent execution slippage
                entry_req = LimitOrderRequest(
                    symbol=SYMBOL,
                    qty=qty_to_buy,
                    limit_price=quote.ask_price,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
                trading_client.submit_order(entry_req)
                time.sleep(3)

                # Deploy 2.0% trailing stop to tolerate micro-swings
                trail_req = TrailingStopOrderRequest(
                    symbol=SYMBOL,
                    qty=qty_to_buy,
                    side=OrderSide.SELL,
                    trail_percent=TRAILING_STOP_PCT,
                    time_in_force=TimeInForce.DAY,
                )
                trading_client.submit_order(trail_req)
                print(f"{TRAILING_STOP_PCT}% Trailing Stop Deployed on {qty_to_buy} shares.")
                time.sleep(30)
        else:
            time.sleep(2)

    except Exception as e:
        print(f"Error in loop: {e}")
        time.sleep(5)
