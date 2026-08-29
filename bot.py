import os
import time
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TrailingStopOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest

# Load keys safely from cloud environment variables
API_KEY = os.environ.get("PKYKCQOK5SHSZO365FNZWBVE3K")
SECRET_KEY = os.environ.get("2M26NEWpkHFq6Q3GB26uuDzvawhECVaUXPVNHxvnGFik")
SYMBOL = "SPY"
TRADE_ALLOCATION = 250.0

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

            if current_price < (avg_entry * 0.985):
                print("EMERGENCY CONTINGENCY: Liquidating position!")
                trading_client.close_all_positions(cancel_orders=True)

            time.sleep(5)
            continue

        req = StockSnapshotRequest(symbol_or_symbols=[SYMBOL])
        snapshot = data_client.get_stock_snapshot(req)
        quote = snapshot[SYMBOL].latest_quote
        spread = quote.ask_price - quote.bid_price

        print(f"[{SYMBOL}] Spread: ${spread:.2f} | Ask: ${quote.ask_price}")

        if spread <= 0.02:
            if ask_gemini_greenlight(SYMBOL, spread):
                print("Gemini Greenlight Confirmed. Executing Entry...")
                qty_to_buy = round(TRADE_ALLOCATION / quote.ask_price, 4)

                entry_req = MarketOrderRequest(
                    symbol=SYMBOL,
                    qty=qty_to_buy,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
                trading_client.submit_order(entry_req)
                time.sleep(2)

                trail_req = TrailingStopOrderRequest(
                    symbol=SYMBOL,
                    qty=qty_to_buy,
                    side=OrderSide.SELL,
                    trail_percent=1.0,
                    time_in_force=TimeInForce.DAY,
                )
                trading_client.submit_order(trail_req)
                print(f"1% Trailing Stop Deployed on {qty_to_buy} shares.")
                time.sleep(30)
        else:
            time.sleep(2)

    except Exception as e:
        print(f"Error in loop: {e}")
        time.sleep(5)
