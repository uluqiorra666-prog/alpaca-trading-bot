import os
import time
import asyncio
from datetime import datetime, timezone, timedelta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, TrailingStopOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.live import StockDataStream
from alpaca.data.enums import DataFeed

# API Credentials
API_KEY = os.environ.get("ALPACA_API_KEY", "PKYKCQOK5SHSZO365FNZWBVE3K")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "2M26NEWpkHFq6Q3GB26uuDzvawhECVaUXPVNHxvnGFik")

if not API_KEY or not SECRET_KEY:
    raise ValueError("Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in environment variables.")

# Strategy Parameters
BUCKET_ALLOCATION_PCT = 0.50   # 50% account equity per sub-account bucket ($250 on $500 balance)
MAX_SPREAD = 0.04              # Maximum allowed bid-ask spread
TRAILING_STOP_PCT = 1.0        # 1.0% Trailing Stop
EMERGENCY_STOP_RATIO = 0.99    # -1.0% Hard Emergency Drop
STALL_TIMEOUT_SEC = 600        # 10 Minutes stall threshold
STALL_MIN_GAIN_PCT = 0.004     # Minimum +0.4% gain required after stall window

# High Volatility Target Watchlist (~7% ATR)
WATCHLIST = ["TQQQ", "SOXL", "LABU", "BITO", "UPRO"]

# Initialize Alpaca Clients
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
stream_client = StockDataStream(API_KEY, SECRET_KEY, feed=DataFeed.IEX)

# Strategy State
state = {
    "active_position": None,
    "entry_time": None,
    "is_processing": False
}

def sync_position_state():
    """Sync active position state directly from Alpaca."""
    positions = trading_client.get_all_positions()
    if len(positions) > 0:
        pos = positions[0]
        state["active_position"] = {
            "symbol": pos.symbol,
            "qty": float(pos.qty),
            "avg_entry": float(pos.avg_entry_price),
            "current_price": float(pos.current_price)
        }
        if state["entry_time"] is None:
            state["entry_time"] = time.time()
    else:
        state["active_position"] = None
        state["entry_time"] = None

def evaluate_technical_setup(symbol):
    """Calculates SMA-9, 15-bar average volume, and 10-bar price expansion."""
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

        return current_price > sma_9 and current_vol > (avg_vol_15 * 1.2) and price_expansion_pct >= 0.008
    except Exception as e:
        print(f"Technical setup error for {symbol}: {e}")
        return False

async def handle_trade_tick(data):
    """Real-time WebSocket event handler for incoming live trade ticks."""
    if state["is_processing"]:
        return

    state["is_processing"] = True
    try:
        symbol = data.symbol
        price = float(data.price)

        # 1. LIVE POSITION MONITORING
        if state["active_position"]:
            pos = state["active_position"]
            if pos["symbol"] == symbol:
                avg_entry = pos["avg_entry"]
                unrealized_pl_pct = (price - avg_entry) / avg_entry

                # Emergency Hard Stop Loss (-1.0%)
                if price <= (avg_entry * EMERGENCY_STOP_RATIO):
                    print(f"[LIVE TICK ALERT] EMERGENCY EXIT: {symbol} dropped to ${price:.2f} (-1.0%). Liquidating...")
                    trading_client.close_all_positions(cancel_orders=True)
                    sync_position_state()
                    state["is_processing"] = False
                    return

                # Stall Timeout Exit (10 Mins)
                elapsed = time.time() - (state["entry_time"] or time.time())
                if elapsed > STALL_TIMEOUT_SEC and unrealized_pl_pct < STALL_MIN_GAIN_PCT:
                    print(f"[LIVE TICK ALERT] STALL EXIT: {symbol} stagnant for {int(elapsed/60)} mins. Exiting...")
                    trading_client.close_all_positions(cancel_orders=True)
                    sync_position_state()
                    state["is_processing"] = False
                    return

        # 2. SCANNING LIVE TICKS FOR ENTRY SIGNALS
        elif symbol in WATCHLIST:
            if evaluate_technical_setup(symbol):
                account = trading_client.get_account()
                equity = float(account.equity)
                trade_allocation = equity * BUCKET_ALLOCATION_PCT
                qty_to_buy = int(trade_allocation // price)

                if qty_to_buy > 0:
                    print(f"[LIVE SIGNAL] Submitting entry order: {qty_to_buy} shares of {symbol} @ ${price:.2f}")

                    # Limit Entry
                    entry_order = LimitOrderRequest(
                        symbol=symbol,
                        qty=qty_to_buy,
                        limit_price=price,
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.DAY
                    )
                    trading_client.submit_order(entry_order)
                    await asyncio.sleep(2)

                    # 1.0% Trailing Stop Loss
                    trail_order = TrailingStopOrderRequest(
                        symbol=symbol,
                        qty=qty_to_buy,
                        side=OrderSide.SELL,
                        trail_percent=TRAILING_STOP_PCT,
                        time_in_force=TimeInForce.DAY
                    )
                    trading_client.submit_order(trail_order)
                    print(f"[LIVE SIGNAL] 1.0% Trailing Stop deployed for {symbol}.")

                    sync_position_state()

    except Exception as e:
        print(f"Live processing error: {e}")
    finally:
        state["is_processing"] = False

async def main():
    clock = trading_client.get_clock()
    if not clock.is_open:
        print("Market is currently closed. Exiting.")
        return

    sync_position_state()
    
    # Ensure active position has a trailing stop loss attached
    if state["active_position"]:
        pos = state["active_position"]
        symbol = pos["symbol"]
        order_params = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
        open_orders = trading_client.get_orders(filter=order_params)
        has_trailing_stop = any(o.order_type == "trailing_stop" for o in open_orders)

        if not has_trailing_stop:
            print(f"Attaching 1.0% trailing stop to active position {symbol}...")
            trail_order = TrailingStopOrderRequest(
                symbol=symbol,
                qty=pos["qty"],
                side=OrderSide.SELL,
                trail_percent=TRAILING_STOP_PCT,
                time_in_force=TimeInForce.DAY
            )
            trading_client.submit_order(trail_order)

    print(f"Connected to Live Alpaca WebSocket Stream (IEX Feed)... Monitoring: {WATCHLIST}")

    # Subscribe WebSocket handlers for real-time tick streaming
    stream_client.subscribe_trades(handle_trade_tick, *WATCHLIST)
    
    # Run the live stream task asynchronously for 20 minutes before clean shutdown
    stream_task = asyncio.create_task(stream_client._run_forever())
    await asyncio.sleep(1200)
    
    print("Execution window ending. Stopping WebSocket stream cleanly...")
    await stream_client.stop_ws()

if __name__ == "__main__":
    asyncio.run(main())
