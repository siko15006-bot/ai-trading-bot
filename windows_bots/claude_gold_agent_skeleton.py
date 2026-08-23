"""
سكيلتون بوت ذهب مستقل بيستخدم Anthropic API مباشرة (tool calling) بدل Claude Code.
مش شغال لايف - محتاج مراجعة وتفعيل صريح من أحمد قبل أي تشغيل حقيقي.
"""
import MetaTrader5 as mt5
import pandas as pd
import anthropic
from anthropic import beta_tool

SYMBOL = "XAUUSDm"
MAGIC = 990100  # ماجيك مخصص للبوت ده، منفصل عن الأنظمة التانية

# ---------- أدوات قراءة السوق ----------

@beta_tool
def get_current_price(symbol: str = SYMBOL) -> dict:
    """Get current bid/ask price for a symbol."""
    tick = mt5.symbol_info_tick(symbol)
    return {"bid": tick.bid, "ask": tick.ask, "time": tick.time}


@beta_tool
def get_historical_rates(symbol: str = SYMBOL, timeframe: str = "H1", count: int = 50) -> list:
    """Get past candles (OHLC) for trend/indicator analysis. timeframe: M15|H1|H4|D1."""
    tf_map = {"M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1,
              "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1}
    rates = mt5.copy_rates_from_pos(symbol, tf_map[timeframe], 0, count)
    return [{"time": int(r["time"]), "open": r["open"], "high": r["high"],
              "low": r["low"], "close": r["close"], "volume": int(r["tick_volume"])}
             for r in rates]

@beta_tool
def get_technical_indicators(symbol: str = SYMBOL, timeframe: str = "H1") -> dict:
    """Get current RSI(14), MACD(12,26,9), and EMA20/EMA50 for a symbol/timeframe."""
    tf_map = {"M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1,
              "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1}
    rates = mt5.copy_rates_from_pos(symbol, tf_map[timeframe], 0, 200)
    close = pd.Series([r["close"] for r in rates])

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = 100 - (100 / (1 + gain / loss))

    ema12, ema26 = close.ewm(span=12).mean(), close.ewm(span=26).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9).mean()

    ema20, ema50 = close.ewm(span=20).mean(), close.ewm(span=50).mean()

    return {
        "rsi_14": round(rsi.iloc[-1], 2),
        "macd": round(macd_line.iloc[-1], 4),
        "macd_signal": round(signal_line.iloc[-1], 4),
        "macd_histogram": round((macd_line.iloc[-1] - signal_line.iloc[-1]), 4),
        "ema20": round(ema20.iloc[-1], 2),
        "ema50": round(ema50.iloc[-1], 2),
        "trend": "bullish" if ema20.iloc[-1] > ema50.iloc[-1] else "bearish",
    }

# ---------- أدوات الحساب والمراكز ----------

@beta_tool
def get_account_info() -> dict:
    """Get balance, equity, free margin."""
    a = mt5.account_info()
    return {"balance": a.balance, "equity": a.equity, "free_margin": a.margin_free}


@beta_tool
def get_open_positions(symbol: str = SYMBOL) -> list:
    """Get open positions for a symbol with floating P/L."""
    positions = mt5.positions_get(symbol=symbol) or []
    return [{"ticket": p.ticket, "type": "BUY" if p.type == 0 else "SELL",
              "volume": p.volume, "price_open": p.price_open,
              "sl": p.sl, "tp": p.tp, "profit": p.profit} for p in positions]

# ---------- أدوات التنفيذ (حساسة) ----------

@beta_tool
def send_market_order(order_type: str, volume: float, sl: float, tp: float, symbol: str = SYMBOL) -> dict:
    """Send a market order. order_type: BUY|SELL. sl/tp are absolute prices, required."""
    tick = mt5.symbol_info_tick(symbol)
    price = tick.ask if order_type == "BUY" else tick.bid
    request = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": volume,
        "type": mt5.ORDER_TYPE_BUY if order_type == "BUY" else mt5.ORDER_TYPE_SELL,
        "price": price, "sl": sl, "tp": tp, "magic": MAGIC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    return {"retcode": result.retcode, "ticket": result.order, "price": result.price}


@beta_tool
def modify_position(ticket: int, sl: float, tp: float) -> dict:
    """Modify SL/TP on an open position (e.g. move SL to breakeven)."""
    request = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "sl": sl, "tp": tp}
    result = mt5.order_send(request)
    return {"retcode": result.retcode}


@beta_tool
def close_position(ticket: int) -> dict:
    """Close an open position immediately by ticket."""
    pos = mt5.positions_get(ticket=ticket)[0]
    tick = mt5.symbol_info_tick(pos.symbol)
    close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
    price = tick.bid if pos.type == 0 else tick.ask
    request = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": pos.volume,
        "type": close_type, "position": ticket, "price": price, "magic": MAGIC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    return {"retcode": result.retcode}

# ---------- التشغيل ----------

def run_once(user_instruction: str):
    mt5.initialize()
    client = anthropic.Anthropic()
    runner = client.beta.messages.tool_runner(
        model="claude-opus-4-8",
        max_tokens=4096,
        thinking={"type": "adaptive"},
        tools=[get_current_price, get_historical_rates, get_technical_indicators,
               get_account_info, get_open_positions,
               send_market_order, modify_position, close_position],
        messages=[{"role": "user", "content": user_instruction}],
    )
    for message in runner:
        for block in message.content:
            if block.type == "text":
                print(block.text)
    mt5.shutdown()


if __name__ == "__main__":
    run_once("افحص XAUUSD الحالي، واقترح دخول لو فيه فرصة واضحة حسب فوليوم وهيكل الشمعة.")
