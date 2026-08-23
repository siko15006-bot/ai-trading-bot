"""
ea_shield.py — Auto-closes trades from bad EAs (all symbols)
==============================================================
Runs in background. Every few seconds, checks for any BA-account
positions (any symbol) opened by known losing EAs and closes them.

Broadened 2026-07-10: was gold-only (XAUUSD.s) until TrendRider FX8
(magic 991199, forex-only, 0/7 pairs profitable) was found losing money
unchecked because the shield never looked past gold.

This lets us keep the London Breakout running (Python/magic=990099)
while neutralizing bad EAs without having to remove them from charts.
"""
import MetaTrader5 as mt5
import sys
import time
import logging
from datetime import datetime, timezone
from ladder_guard import ACCOUNTS  # reuse the one place credentials live, don't duplicate them
from ntfy_alert import alert  # 2026-08-03: push Ahmed's phone even with no Claude session open
from heartbeat import write_heartbeat  # 2026-08-06: functional heartbeat rollout, see NOTIFICATION_POLICY.md

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SHIELD] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

CHECK_INTERVAL = 1  # seconds — tightened 2026-08-16 (Ahmed: minimize the naked-position
# exposure window) from 5s. Pure poll-frequency change, zero logic touched -- still
# catches fast scalpers like 999921, just faster. 3 accounts x 1 call/sec is well
# under any MT5/broker rate limit.
INIT_RETRY_BASE = 5
INIT_RETRY_MAX = 60

# 2026-07-10: was BA-only (bare initialize() attaches to whatever terminal is
# active). Extended to EA/EM after finding magic 990101 (XAUUSDm, -$9.31/44
# trades/18.2%WR, 07-08 to 07-10) trading completely unwatched on EA. Run one
# process per account (argv[1]), same ACCOUNTS dict ladder_guard.py already uses.
ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else "BA"

# Ahmed opted out of discretionary trading. MT5 tags desktop, mobile, and web
# entries distinctly from Expert/MCP entries, so bots remain unaffected.
MANUAL_REASONS = {
    mt5.POSITION_REASON_CLIENT,
    mt5.POSITION_REASON_MOBILE,
    mt5.POSITION_REASON_WEB,
}

# Magic numbers to block (confirmed losing EAs)
BLOCKED_MAGICS = {
    770705,   # AI_Bridge_EA → -$237, 17% WR
    770706,   # Unknown-706  → -$218, 30% WR
    9999,     # Unknown-9999 → -$194, 12% WR
    770007,   # Unknown-v7   → -$152, 16% WR
    770001,   # Unknown-v1   →  -$83,  7% WR
    770006,   # Unknown-v6   →  -$43, 30% WR
    778801,   # unidentified gold trader, comment "SB Manual/SB Gold" (NOT the SB_FX8_Elite EA
              # -- that one's real magic is 778802, see below) → -$194.06/212 trades/36.3% WR
    778802,   # SB_FX8_Elite (real .mq5 source, forex 8-pair swing+impulse zone system) → never
              # actually live-traded, but faithful full-logic backtest (backtest_sb_fx8.py,
              # M5, ~10mo, 3-stage partial close + trailing) = -$4.73/1952 slices/67.3%WR,
              # consistent loser both halves. Blocked preemptively before it's ever attached.
    770708,
    11111,
    999921,   # rapid scalper → destroyed $50→$7 on 2026-06-29, 15+ SL hits in 3hrs
    123456,   # "HG-BUY" → BA -$20 net in <3hrs on 2026-07-06, repeated SL hits
    991199,   # TrendRider FX8 (forex) → moved from SAFE 2026-07-10: real 60-day history shows
              # net loss on EVERY pair it trades (0/7 profitable) -- GBPUSD -$4.42/12.5%WR worst,
              # -$11.86 total/49 trades. Was wrongly protected; had 2 open positions when caught.
    # 990101 REMOVED 2026-08-02: this is NOT an unidentified scalper -- it's
    # MT5Bridge_E2's own London Breakout magic (see MT5Bridge_E2/.env
    # MAGIC_NUMBER=990101). The 2026-07-10 identification never made that
    # connection and blocked it preemptively; root-cause investigation
    # confirmed via ea_shield_ea_out.log (9 matched BLOCKED/CLOSED pairs)
    # that every EA-leg London Breakout trade was being force-closed within
    # its 5s poll cycle. Moved to SAFE_MAGICS below instead of just deleting,
    # so it's never re-added here by mistake.
}

# Magic numbers to PROTECT (do not touch)
SAFE_MAGICS = {
    990099,   # MT5 Bridge (London Breakout, BA leg)
    990101,   # MT5 Bridge (London Breakout, EA leg) -- wrongly blocked 2026-07-10,
              # corrected 2026-08-02, see removal note in BLOCKED_MAGICS above
    995500,   # swing_pending_bot (EA+BA) -- gained its own magic 2026-08-02, was
              # magic=0 before (see swing_pending_bot.py's MAGIC constant docstring)
    995501,   # participation_pilot_btc (EA) -- split off from 995500 on 2026-08-06
              # after discovering it collided with swing_pending_bot's magic
    880088,   # IFVGBridge
    993399,   # EMA-ADX bot on BA (added 2026-07-03)
    993400,   # EMA-ADX bot on EA (moved here 2026-07-07 after BA's 3 consecutive SLs) —
              # was missing, spammed "Unknown magic" every 5s into ea_shield_ea_out.log
              # for over a week (found during 2026-07-15 cleanup, log had grown past 4MB)
    884400,   # fx_signal_exec (Learn2Trade + gold groups), same reason as above
    992200,   # orb_eth_exness on EA, same reason
    992201,   # orb_eth_exness on EM, same reason
    770005,   # The only profitable magic (+$2.17)
    0,        # Expert/MCP trades may use magic=0; manual reason is blocked first
    996600,   # gold_btc_bot.py (XAU+BTC conservative trend bot, EA/EM/BA) -- added
              # 2026-08-04 ahead of its Pilot per Ahmed's explicit request, before
              # the bot itself ever runs live (learned from the 990101/993400
              # incidents above: add the magic before first launch, not after).
}


def close_position(pos):
    tick = mt5.symbol_info_tick(pos.symbol)
    if not tick:
        log.error("Can't get tick for %s", pos.symbol)
        return False

    close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
    price      = tick.bid if pos.type == 0 else tick.ask

    req = {
        "action":      mt5.TRADE_ACTION_DEAL,
        "position":    pos.ticket,
        "symbol":      pos.symbol,
        "volume":      pos.volume,
        "type":        close_type,
        "price":       price,
        "deviation":   30,
        "magic":       pos.magic,
        "comment":     "EA-Shield",
        "type_time":   mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(req)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info("CLOSED #%d magic=%d pnl=%.2f", pos.ticket, pos.magic, pos.profit)
        alert(f"EA-Shield [{ACCOUNT}] closed #{pos.ticket} magic={pos.magic} pnl={pos.profit:.2f}",
              key=f"eashield_close_{ACCOUNT}")
        return True
    else:
        err = result.comment if result else "no result"
        log.error("Failed to close #%d: %s", pos.ticket, err)
        return False


def cancel_pending(order):
    result = mt5.order_send({
        "action": mt5.TRADE_ACTION_REMOVE,
        "order": order.ticket,
        "comment": "Manual-Lock",
    })
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info("CANCELLED manual pending #%d symbol=%s", order.ticket, order.symbol)
        alert(f"EA-Shield [{ACCOUNT}] cancelled manual pending #{order.ticket}",
              key=f"eashield_manual_{ACCOUNT}")
        return True
    err = result.comment if result else "no result"
    log.error("Failed to cancel manual pending #%d: %s", order.ticket, err)
    return False


def is_manual_trade(trade):
    return getattr(trade, "reason", None) in MANUAL_REASONS


def _connect_mt5(cfg, account):
    delay = INIT_RETRY_BASE
    while True:
        mt5.shutdown()
        if mt5.initialize(**cfg):
            log.info("MT5 connected to %s. Checking every %ds", account, CHECK_INTERVAL)
            return
        log.error("MT5 init failed: %s -- retrying in %ds", mt5.last_error(), delay)
        time.sleep(delay)
        delay = min(INIT_RETRY_MAX, delay * 2)


def check_and_shield():
    # 2026-08-19 (Ahmed, explicit live request + explicit confirmation after
    # his own manual BA trade got auto-closed within ~1s): genuine manual
    # trades (MT5 reason=CLIENT/MOBILE/WEB) are no longer auto-closed or
    # cancelled -- only logged. This reverses the 2026-07 "opted out of
    # discretionary trading" block at Ahmed's direct instruction.
    # BLOCKED_MAGICS closing (bad EAs) below is untouched -- this only stops
    # the manual-trade branch. See backup ea_shield.py.bak_20260819_manual_unblock
    # for the prior behavior.
    for order in mt5.orders_get() or ():
        if is_manual_trade(order):
            log.info("MANUAL pending (not touched): #%d symbol=%s", order.ticket, order.symbol)

    positions = mt5.positions_get()
    if not positions:
        return

    for pos in positions:
        if is_manual_trade(pos):
            log.info(
                "MANUAL position (not touched): #%d symbol=%s vol=%.2f pnl=%.2f",
                pos.ticket, pos.symbol, pos.volume, pos.profit,
            )
            continue
        if pos.magic in SAFE_MAGICS:
            continue
        if pos.magic in BLOCKED_MAGICS:
            log.warning(
                "BLOCKED EA detected: #%d magic=%d type=%s vol=%.2f pnl=%.2f — closing",
                pos.ticket, pos.magic,
                "BUY" if pos.type == 0 else "SELL",
                pos.volume, pos.profit,
            )
            close_position(pos)
        else:
            log.info("Unknown magic %d on #%d — monitoring (not closing)", pos.magic, pos.ticket)


def run():
    log.info("EA Shield [%s] started — protecting all symbols from %d blocked magics", ACCOUNT, len(BLOCKED_MAGICS))
    log.info("Safe magics (never touch): %s", SAFE_MAGICS)

    # explicit path+login always — bare initialize() attaches to whatever terminal
    # is last-active, which silently pointed the shield at the wrong account before
    cfg = ACCOUNTS[ACCOUNT]
    _connect_mt5(cfg, ACCOUNT)

    while True:
        try:
            if mt5.terminal_info() is None:
                _connect_mt5(cfg, ACCOUNT)
                continue
            check_and_shield()
            write_heartbeat(f"ea_shield_{ACCOUNT}")
        except Exception as e:
            log.error("Shield error: %s", e)
            _connect_mt5(cfg, ACCOUNT)
        time.sleep(CHECK_INTERVAL)


def self_test():
    from types import SimpleNamespace

    assert is_manual_trade(SimpleNamespace(reason=mt5.POSITION_REASON_CLIENT))
    assert is_manual_trade(SimpleNamespace(reason=mt5.POSITION_REASON_MOBILE))
    assert is_manual_trade(SimpleNamespace(reason=mt5.POSITION_REASON_WEB))
    assert not is_manual_trade(SimpleNamespace(reason=mt5.POSITION_REASON_EXPERT))
    print("ea_shield manual-lock self-test passed")


if __name__ == "__main__":
    self_test() if "--test" in sys.argv else run()
