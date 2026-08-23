"""
fx_signal_exec.py — Learn2Trade forex signal auto-executor (EA + BA)
Listens to the LEARN2TRADE Telegram group (Ahmed's account, copied Telethon
session — same account as tg_signal_bot.py on Oracle, separate device session)
and places a fixed 0.01-lot trade on both EA (Exness E2) and BA (Bybit MT5)
using the signal's own entry zone / SL / TP1. Gold is blocked per Ahmed's
2026-07-04 rule (same as the crypto tg_signal_bot).
"""
import hashlib
import os
import re
import time
import json
from datetime import datetime, timezone

from telethon.sync import TelegramClient
from telethon import events
import MetaTrader5 as mt5
from bot_period_guard import manual_block_reason
from heartbeat import write_heartbeat  # 2026-08-06: functional layer added alongside the existing
                                        # connection_watchdog (kept as-is -- that's the loop/connection-
                                        # alive signal, this proves messages are actually being processed)

API_ID = <REDACTED_TG_API_ID>
API_HASH = "<REDACTED_TG_API_HASH>"
SESSION = r"C:\TradingBot\Bot_Active\fx_session_new"

LEARN2TRADE_ID = -1001263225860

# Crypto signal groups (same ones tg_signal_bot trades on BAA) — mirrored here
# for BTC/ETH only, since those are the only cryptos Exness carries as CFDs.
# Added 2026-07-06 per Ahmed ("شغل EA/EM أكتر").
CRYPTO_GROUPS = {
    -1002097370390: "SPARTA_CRYPTO",
    -1001553551852: "BINANCE_360",
    # WHALES_CIRCLE removed 2026-07-18: demoted on tg_signal_bot.py 2026-07-15
    # for the same signal-quality evidence (50% WR, realized -2.21) but that
    # demotion never carried over here — same underlying signals, same verdict.
}
CRYPTO_BASES = {"BTC": "BTCUSD", "ETH": "ETHUSD"}  # signal coin -> MT5 base (suffix added per account)
CRYPTO_ACCOUNTS = ("EA", "EM")  # BA has no crypto symbols at all

# Forex signal groups (added 2026-07-07, Ahmed: "في اشارات تليجرام كويسة مش بتعملها").
# Direction + entry zone come from the signal; SL/TP are OUR scalp numbers
# (5/15 pips) per feedback-signal-direction-our-risk. Gold blocked as always.
FX_GROUPS = {
    -1001759495587: "NEXA_TRADE",
    -1002046317788: "AYAN_FOREX",
    -1001175463265: "FOREX_GDP",
    # SMC_TRADER: was "notify-only" on tg_signal_bot.py (Bybit crypto exchange
    # can't trade forex) but that classification never carried over here —
    # MT5 (EA/BA/EM) CAN trade forex, so it sat un-executed with zero track
    # record despite Ahmed reporting good calls. Added 2026-07-09.
    -1002253964840: "SMC_TRADER",
    # Gold-signal groups, added 2026-07-09 after Ahmed authorized ("مع كل
    # الاذونات تمام كده") revisiting the 2026-07-04 gold-from-telegram block
    # now that direction-only + our-own-risk (feedback-signal-direction-our-risk)
    # already fixes the root cause of the original Gold98 -$8.36 loss. Only
    # evidence-backed groups (signal_eval.log, >=15 signals, WR>=60%) are in
    # GOLD_ALLOWED_GROUPS below — everyone else's gold stays blocked as before.
    -1002110392082: "XAUUSD_ANALYSIS",
    -1003021391947: "GOLD98_SURE",
    # Promoted 2026-07-15, Ahmed's explicit confirmation ("فعّل FOREX_GOLD_SIGNALS"):
    # 16 signals, WR=67% in signal_eval — cleared the >=15 / >=60% bar.
    -1003597716166: "FOREX_GOLD_SIGNALS",
    # Promoted 2026-07-16, Ahmed's explicit confirmation ("ضيفه"):
    # 15 signals, WR=75% in group_stats.json — cleared the >=15 / >=60% bar.
    -1001920682501: "GOLD98_PERCENT",
}
GOLD_ALLOWED_GROUPS = {"NEXA_TRADE", "GOLD98_SURE", "GOLD98_PERCENT"}
# XAUUSD_ANALYSIS + FOREX_GOLD_SIGNALS removed 2026-08-16 (Ahmed-approved,
# evidence-gated demotion): fresh signal_eval.log data shows both flipped
# net-negative (exp_R -0.11 n=22, exp_R -0.13 n=27) since promotion -- same
# symmetric evidence policy used to promote them, applied in reverse. Still
# recognized in FX_GROUPS (parsing/notify continues), just excluded from
# GOLD_ALLOWED_GROUPS so no new real gold order gets placed for either.
# 2026-07-16: overnight window (00:00-05:00 UTC) measured -$52.66/7 trades from
# gold telegram signals (thin Asian-session liquidity -> false breaks eat tight
# stops). Ahmed wants gold active at night anyway ("عايز نشغل xau عشان نعوض")
# but not blindly reopened to the exact setup that lost money, so only the
# highest-conviction groups (>=75% WR in group_stats.json) fire during this
# window; the rest still trade normally 05:00-24:00.
NIGHT_GOLD_GROUPS = {"NEXA_TRADE", "GOLD98_PERCENT"}  # 83.3% / 75% WR
GOLD_ACCOUNTS = ("EA", "BA")  # EM removed for gold 2026-08-11 (Ahmed): funded only $10.78, and 0.01 (min) lot gold risks ~$7-12 = most/all of the account. BTC/forex on EM still OK. Claude re-adds EM once funded ~$80+.
                                    # (feedback-no-xau-exness) -- this tuple was never
                                    # updated to match that decision until 2026-08-03.

# 2026-08-01 (Ahmed's request "شوف باقي جروبات"): 112 gold/forex/crypto signal
# channels Ahmed is already a member of, discovered via get_dialogs() and kept
# if they weren't junk (apps/movies/real-estate channels were filtered out).
# RECORD-ONLY per feedback-discover-new-groups -- logged for a future win-rate
# review (same bar as GOLD_ALLOWED_GROUPS: >=15 signals, >=60% WR) before any
# of these ever gets promoted to real execution. Never touches execute_on_account.
RECORD_ONLY_GROUPS = {
    -1002007658025: "THE WOLF (CRYPTO)®",
    -1002847745811: "Arthur Hayes Signals",
    -1001252363647: "VIP Results (binance 360)",
    -1001787899566: "Signal forex",
    -1001806690343: "ZA trading توصيات تداول",
    -1001848972778: "اخبار تداول الفوركس العاجلة 🕐🌍",
    -1001552004524: "CRYPTO MONK",
    -1004298874694: "MAFIA GOLD",
    -1001443366294: "RAHMAN PRO FX",
    -1003626325572: "الصفوة الذهبية | صُنّاع الأرباح",
    -1001789800455: "KING FX💯🔥",
    -1001415365916: "تداول الداو والذهب بأمان🎯",
    -1002578445525: "Zalzail_El Gold 🥇",
    -1001594522150: "Whales_Pumps",
    -1001574201347: "We take off (crypto trading)",
    -1002078531832: "Crypto Mermaids",
    -1001703588804: "توصيات مجانية Afra Al Qasimi FX & Gold",
    -1001649102395: "LIMITLESS FX ♾️",
    -1002800449374: "GOLD AVI TRADER",
    -1002749968972: "حيتان التداول ❤️‍🔥🐳 chat",
    -1001656921849: "QuantEdge Signals Desk®",
    -1002339412040: "The magician GOLD 🔱🔱🔱",
    -1001783301467: "Crypto Safe Calls™",
    -1003405743337: "EX GROUP. s.a.r.l ( forex & synthetic)",
    -1001871200862: "سعر الذهب اليوم في مصر 🇪🇬",
    -1002122886546: "FOREX & MAN",
    -1004461128021: "Gorilla Signals",
    -1002161620633: "Trading volume number",
    -1001700314832: "WHALES CIRCLE",
    -1001543236863: "MOSTASHAR EL GOLD",
    -1001932070473: "♀️نادي فوركس الذهبي♀️",
    -1001727857237: "Crypto Musk",
    -1002537790930: "SNP GOLD QUEEN 👸🏦",
    -1001754095061: "Crypto Radar",
    -1001627669928: "✊FOREX MASTER TREDING👊",
    -1003427982261: "م.مصطفى زاهر |M.Z Forex🔱",
    -1001297561296: "Gorilla Crypto",
    -1002156141266: "♦️ Gold ♦️",
    -1001996547716: "✨مهندس الفوركس ✨",
    -1001536559796: "صياد الفوركس",
    -1001561306115: "⚜️ صياد الفوركس ⚜️",
    -1001918236146: "إشارات الذهب والعملات 💯😎",
    -1002001334877: "كينج الفوركس",
    -1002085295311: "BUY OR SELL GOLD",
    -1001498555047: "XAUUSD SIGNALS📊",
    -1002210668801: "متداول الذهب FX™ ✨",
    -1002930784475: "GREEN PIPS SOCIETY",
    -1002258613288: "QUBTAN EL GOLD",
    -1002128827128: "BTC ABUAZOZ",
    -1001622654998: "Scalping_300%",
    -1002951939930: "QAHER ELGOLD_قاهر الجولد⚜️",
    -1002182110475: "༺༽اسطورة الدهب༼༻",
    -1001927294039: "Vincent Gold Trader",
    -1002225009683: "Callofforex🇸🇦",
    -1002067177406: "Magic Trader Fx",
    -1002253949345: "قناة المعلم الذهبي",
    -1003813809289: "GOLD TRADER",
    -1003074793534: "ملوك التداول",
    -1003015528185: "قائد الذهب و فوركس",
    -1002518335599: "كتب التداول مجانا",
    -1002451714298: "GOLD SCALPING PRO TRADER",
    -1003665736363: "🏆توصيات 💎اسطوره 🏆💯🔥💪🐬",
    -1003555100982: "الفوركس الذهب إشارات تداول العملات الأجنبية",
    -1002010916528: "GOLD PRO TRADER",
    -1001235489570: "Crypto TA King 🐋",
    -1002745275908: "ملك الذهب",
    -1001592322990: "Hakim EL GOLD 👑",
    -1002289962757: "Sultan Gold 👑🦅🪄",
    -1002687214764: "قناة تعليمية مجانية التداول , سوق الاسهم الامريكي و العملات والذهب والمؤشرات spx , us500",
    -1003024628496: "XAUUSD SINGNALS⚜️",
    -1002081252671: "OMAR_GOLD🫅",
    -1001664435199: "المـصـري لتـداول الـذهب彡",
    -1003785697051: "VORTEX | GOLD",
    -1002317387243: "🔥💹المتخصص FX💹️",
    -1001875494349: "📊AZURE FOREX FREESIGNALS🏅",
    -1001468571729: "NAS100&US30 SPECIAL SIGNALS",
    -1004397421755: "توصيات فوركس ذهب {PRIME}",
    -1003670221163: "TRADE بالعقل 📊",
    -1002843156344: "ملك الذهب",
    -1003138894647: "Majd forex trading & market",
    -1003520136927: "4⃣🇸🇦 توصيات كريبتو Leverage200X🔴 🇸🇦🇶🇦",
    -1001765226347: "Ben, Gold Trader",
    -1001881834394: "XAUUSD BEST SIGNALS",
    -1001014346053: "XM-FreeSignal",
    -1002138937648: "PH TRADING 📊",
    -1001431871074: "EG FOREX",
    -1002193506861: "Precision Trade Signals🥇",
    -1001821930589: "🟢Elite Trade Signals",
    -1001604309645: "James Gold Master",
    -1001476143548: "FX RIVER ACADEMY™",
    -1002533196532: "Royal Pips Club",
    -1003995972761: "XAUUSD SIGNALS",
    -1002361402211: "Trend Rider FX",
    -1002720327480: "XAUUSD ROYAL KING",
    -1003638913183: "SMART TRADE ACADEMY",
    -1003900580386: "Pips Hunter 📊📊",
    -1001425255914: "Forex Xauusd & Binary Market Trader",
    -1002324770442: "TRADE WITH ROMEE",
    -1002341335556: "قناة زين الذهب",
    -1002160330855: "صقر الدهب_Saqr Gold 🦅",
    -1001423195402: "CPTrading",
    -1003180965374: "Leo i Trade 💵",
    -1001984099655: "ONLY FX",
    -1002290560729: "Sheikh Trader Gold 🇦🇪",
    -1003811607244: "XAUUSD GOLD TRADING",
    -1001591904294: "Trade With Malik Hussain",
    -1002142310788: "GOLD EMPIRE🦅",
    -1003242683940: "ZaidarFX📈",
    -1003674772206: "XAUUSD GOLD MARKET",
    -1003754141904: "Gold with Z 🖤",
    -1003687854406: "TRADING FX",
    -1003760468922: "Forex Cashback",
    # Added 2026-08-05 (Ahmed: "شوف تليجرام في جروبات جديده") -- discovered via
    # discover_new_groups.py (TelegramGroupAudit/), diffed against the full
    # known-ID list, filtered to trading-relevant channels only (gaming/
    # real-estate/apps/movies/personal chats excluded, same bar as the
    # original 112). RECORD-ONLY per feedback-discover-new-groups -- never
    # touches execute_on_account until a future WR/PF review clears them.
    -1001654705239: "William Forex Academy-Market Insights",
    -1002885911212: "A L W 7 S H TRADING",
    -1002326322546: "XAUUSD PIPS PROFIT",
    -1002415770017: "Capital.com International",
    -1003864644782: "PARWEEZ_TRDER",
    -1002494978108: "GOLD GBP Killer",
    -1003289559043: "SMC ICT KING",
    -1003941747706: "TRADING FANATIC",
    -1002128602498: "Saud binary",
    -1003710550706: "XAUUSD MASTER",
    -1001390568202: "PRO TRADING",
    -1001924687126: "CURRENCYBOY",
    -1001608128997: "XAUUSD EXPERT",
    -1001813227480: "Jaxon Pump",
    -1002216665319: "PROFESSIONAL RISK CONTROL",
    -1002703759874: "Professor Al-Dahap",
    -1002226409599: "XAUUSD SIGNAL FINDER",
    -1001975523010: "Eliz FX Academy",
    -1001780474473: "Quartz Academy",
    -1001566319279: "GOLDINFINITY",
    -1002193292133: "Binance futures trading 66",
    -1003210478272: "Gold Sniper",
    -1001572931574: "Mafia Quotex",
    -1001799995585: "تعليم التحليل الفني | REEM NASRI",
    -1001511003843: "GOLD VIP SIGNALS",
    -1002186655391: "GOLD FOREX SIGNALS",
    -1001580107051: "GPT PRO TRADER",
    -1001164302310: "ABNGAZA GOLD",
    -1002283762083: "Trading by Hayam",
    -1001833946395: "SMART FX TRADER",
    -1001379330907: "KING OF GOLD PROFIT",
    -1001768818138: "GOLD SCALPER",
    -1003855986764: "Hakeem pro trader",
    -1003820016064: "Xauusd Gold Vip",
    -1003307897378: "XAUUSD & FOREX TRADING",
    -1002405922226: "Mr Expert",
    -1002123596474: "AXIS Stock Market",
    -1003915619733: "Signolla ai",
    -1002007699575: "Absolute Futures",
    -1002164876109: "SMC VIP",
    -1002970521397: "BOT EA MT5",
}
FX_PAIR_RE = re.compile(r"\b(EUR|GBP|AUD|NZD|USD|CAD|CHF|JPY)\s*/?\s*(USD|JPY|CHF|CAD|AUD|NZD|GBP)\b")
FXG_PAIR_RE = re.compile(r"\b(XAU|EUR|GBP|AUD|NZD|USD|CAD|CHF|JPY)\s*/?\s*(USD|JPY|CHF|CAD|AUD|NZD|GBP)\b")
FXG_GOLD_RE = re.compile(r"\bGOLD\b")
FXS_NUM = r"([0-9]*\.?[0-9]+)"

ACCOUNTS = {
    "EA": dict(path=r"C:\MT5_Portable_2\terminal64.exe",
               login=<REDACTED_MT5_LOGIN_EA>, password="<REDACTED_MT5_PASSWORD_EA>", server="Exness-MT5Real33",
               suffix="m"),
    "BA": dict(path=r"C:\Program Files\MetaTrader 5\terminal64.exe",
               login=<REDACTED_MT5_LOGIN_BA>, password="<REDACTED_MT5_PASSWORD_BA>", server="Bybit-Live-5",
               suffix=".s"),
    # EM added 2026-07-05 by Ahmed's instruction ("شغله على كله بلاش الذهب") —
    # gold already blocked globally via BLOCKED_BASES.
    "EM": dict(path=r"C:\MT5_Portable_3\terminal64.exe",
               login=<REDACTED_MT5_LOGIN_EM>, password="<REDACTED_MT5_PASSWORD_EM>", server="Exness-MT5Real35",
               suffix="m"),
}

BLOCKED_BASES = {"XAU", "XAG", "GOLD"}  # Ahmed 2026-07-04: no gold from telegram signals
# 2026-07-15: EA/BA doubled to 0.02 (Ahmed's explicit approval — accounts grew,
# $4 XAU risk was only ~2% of balance vs the ~4-9% it was at the original
# baseline). EM kept at 0.01, still small relative to EA/BA.
LOT = {"EA": 0.02, "BA": 0.02, "EM": 0.01}
# 2026-07-17: gold telegram trades halved back to 0.01 (Ahmed's decision after the
# overnight -$55 gold whipsaw with BA slippage -$31.87 vs -$18.5 expected — thin
# night liquidity + gold's $/point makes 0.02 too heavy for this signal source).
# Forex/crypto telegram stay on LOT above (their per-trade risk is naturally small).
GOLD_LOT = {"EA": 0.01, "BA": 0.01, "EM": 0.01}
ENTRY_TOL = 0.001  # 0.1% around the signal's entry zone — don't chase
MIN_EQUITY_USD = 5.0  # 2026-08-06: below this, order_send just fails "No money"
# (retcode=10019) -- skip with one clean log line instead of a failed attempt
LOG_PATH = r"C:\TradingBot\Bot_Active\fx_signal_exec.log"
INIT_RETRY_BASE = 5
INIT_RETRY_MAX = 60
# 2026-08-07 (Ahmed-approved): same portfolio-wide circuit breaker
# gold_btc_bot.py already respects. Blocks NEW entries only -- open
# positions stay managed normally by ladder_guard.py/ea_shield.py.
RISK_HALT_FLAG_PATH = r"C:\TradingBot\Bot_Active\RISK_HALT.flag"

PAIR_RE = re.compile(r"\b([A-Z]{3})/([A-Z]{3})\b")
SIDE_RE = re.compile(r"\b(LONG|BUY|SHORT|SELL)\b", re.I)
ENTRY_RE = re.compile(r"Entry(?:\s*Zone)?[:\s]*\**\$?([\d.]+)\s*[-–]\s*\$?([\d.]+)", re.I)
SL_RE = re.compile(r"SL[:\s]*\**\$?([\d.]+)", re.I)
TP1_RE = re.compile(r"TP1?[:\s]*\**\$?([\d.]+)", re.I)


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


RECORD_LOG_PATH = r"C:\TradingBot\Bot_Active\record_only_signals.log"


def log_record(msg):
    """Same shape as log(), separate file -- keeps the 112 record-only
    channels' volume out of the real execution log Ahmed actually watches."""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    with open(RECORD_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


TRACE_LOG_PATH = r"C:\TradingBot\Bot_Active\fx_signal_trace.jsonl"
SHADOW_LOG_PATH = r"C:\TradingBot\Bot_Active\fx_signal_exec_shadow.jsonl"


def _write_shadow(record):
    """Durable shadow log for immediate before/after signal review."""
    try:
        with open(SHADOW_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _event_time_iso(event):
    dt = getattr(getattr(event, "message", None), "date", None)
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _signal_fingerprint(text):
    """Short, stable identifier for a raw signal message -- lets the same
    signal be recognized across the trace file, the MT5 comment field, and
    (if ever needed) the raw Telegram text, independent of any log file."""
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:8]


def _comment_tag(group, fingerprint):
    """Compact group+fingerprint tag for MT5's own comment field -- lives on
    the order/position in the broker's history, so it survives even if all
    of our own log/trace files are lost. Sanitized + length-capped since
    MT5's comment field limit varies by broker."""
    safe_group = re.sub(r"[^A-Za-z0-9_]", "", (group or "unknown"))[:14]
    return f"L2T:{safe_group}:{(fingerprint or '')[:6]}"


def _write_trace(record):
    """2026-08-04 (Ahmed): trade-source traceability must not depend solely
    on fx_signal_exec.log -- a 7.5-day log gap during a Telegram session
    crash-loop (2026-07-23 to 2026-07-31) left 10 real BA gold trades with
    no recoverable group attribution at all. This writes one durable line
    per actual order_send ATTEMPT (success or failure), independent of the
    regular log() stream, so group/signal/receive-time/ticket survive even
    if fx_signal_exec.log itself is lost or has a gap. Append-only, one
    flush per record -- same crash-resistant open/write/close pattern as
    log() above."""
    try:
        with open(TRACE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass  # tracing must never block or crash order execution itself


def _connect_mt5(cfg, acc_name):
    delay = INIT_RETRY_BASE
    while True:
        mt5.shutdown()
        if mt5.initialize(path=cfg["path"], login=cfg["login"], password=cfg["password"], server=cfg["server"]):
            return
        log(f"{acc_name}: MT5 init failed {mt5.last_error()} -- retrying in {delay}s")
        time.sleep(delay)
        delay = min(INIT_RETRY_MAX, delay * 2)


def parse_signal(text):
    pair_m = PAIR_RE.search(text)
    side_m = SIDE_RE.search(text)
    entry_m = ENTRY_RE.search(text)
    sl_m = SL_RE.search(text)
    tp_m = TP1_RE.search(text)
    if not (pair_m and side_m and entry_m and sl_m and tp_m):
        return None
    base, quote = pair_m.group(1).upper(), pair_m.group(2).upper()
    if base in BLOCKED_BASES or quote in BLOCKED_BASES:
        log(f"SKIPPED: {base}/{quote} — gold/metals blocked by Ahmed's rule.")
        return None
    side = "buy" if side_m.group(1).upper() in ("LONG", "BUY") else "sell"
    return {
        "symbol_base": base + quote,
        "side": side,
        "entry_low": min(float(entry_m.group(1)), float(entry_m.group(2))),
        "entry_high": max(float(entry_m.group(1)), float(entry_m.group(2))),
        "sl": float(sl_m.group(1)),
        "tp": float(tp_m.group(1)),
    }


CRYPTO_NUM = r"([0-9]*\.?[0-9]+)"


def parse_crypto_signal(text):
    """Compact port of the Oracle tg_signal_bot parser, BTC/ETH only."""
    if not text:
        return None
    t = text.upper()
    m_sym = re.search(r"([A-Z0-9]{2,15})\s*/\s*USDT", t)
    if not m_sym or m_sym.group(1) not in CRYPTO_BASES:
        return None
    side = None
    if re.search(r"#?\bLONG\b", t):
        side = "buy"
    elif re.search(r"#?\bSHORT\b", t):
        side = "sell"
    m_entry = re.search(r"ENTRY(?:\s*(?:ZONE|PRICE))?\s*[:\-]*\s*" + CRYPTO_NUM
                        + r"(?:\s*(?:[-–~]|TO)\s*" + CRYPTO_NUM + r")?", t)
    m_sl = re.search(r"(?:STOP(?:[\s\-]?LOSS)?|\bSL\b)\s*[:\-]*\s*" + CRYPTO_NUM, t)
    targets = [float(x) for x in re.findall(r"^\s*\d\s*[:\-]\s*" + CRYPTO_NUM, t, re.MULTILINE)]
    if not targets:
        m_tg = re.search(r"TARGET[S]?\s*[:\-]*\s*" + CRYPTO_NUM, t)
        if m_tg:
            targets = [float(m_tg.group(1))]
    if not targets:
        targets = [float(x) for x in re.findall(r"TAKE\s*PROFITS?\s*[:\-]*\s*" + CRYPTO_NUM, t)]
    if not (m_entry and m_sl and targets) or side is None:
        return None
    e1 = float(m_entry.group(1))
    e2 = float(m_entry.group(2)) if m_entry.group(2) else e1
    return {
        "symbol_base": CRYPTO_BASES[m_sym.group(1)],
        "side": side,
        "entry_low": min(e1, e2),
        "entry_high": max(e1, e2),
        "sl": float(m_sl.group(1)),
        "tp": targets[0],
        # 2026-08-07 (bug fix, Ahmed flagged a live BINANCE_360 trade with the
        # group's raw SL=$90/TP=$7 -- a ~1:13 RR): this parser never carried
        # use_our_risk, unlike parse_fx_group_signal/parse_fx_gold_signal, so
        # crypto signals bypassed the "direction only, our own risk" policy
        # entirely and traded on the group's own far-SL/near-TP numbers. The
        # Oracle tg_signal_bot.py (Bybit/BAA) already had this exact fix
        # (2026-07-16, Ahmed-approved: ATR-based stop, fixed 3:1 RR) -- this
        # MT5 "compact port" just never inherited it. Ported below as
        # atr_stop_crypto(), same math as Oracle's _crypto_atr_dist().
        "use_our_risk": True,
    }


def parse_fx_group_signal(text):
    """Forex-group format: pair + side + optional entry zone (also right after side)
    + dot/colon separators. Returns sig with use_our_risk=True; gold rejected."""
    if not text:
        return None
    t = text.upper()
    m_pair = FX_PAIR_RE.search(t)
    m_side = re.search(r"\b(BUY|LONG|SELL|SHORT)\b", t)
    if not (m_pair and m_side):
        return None
    base, quote = m_pair.group(1), m_pair.group(2)
    if base in BLOCKED_BASES or quote in BLOCKED_BASES:
        return None
    m_entry = re.search(r"ENTRY(?:\s*(?:ZONE|PRICE))?\s*[.:\-]*\s*\**\$?" + FXS_NUM
                        + r"(?:\s*[-\u2013]\s*\$?" + FXS_NUM + r")?", t)
    if not m_entry:
        m_entry = re.search(m_side.group(1) + r"\s+(?:NOW\s+)?\$?" + FXS_NUM
                            + r"(?:\s*[-:\u2013]+\s*\$?" + FXS_NUM + r")?", t)
    if not m_entry:
        return None
    e1 = float(m_entry.group(1))
    e2 = float(m_entry.group(2)) if m_entry.group(2) else e1
    return {"symbol_base": base + quote,
            "side": "buy" if m_side.group(1) in ("BUY", "LONG") else "sell",
            "entry_low": min(e1, e2), "entry_high": max(e1, e2),
            "sl": None, "tp": None, "use_our_risk": True}


def parse_fx_gold_signal(text):
    """Same message formats as the proven Oracle parser (tg_signal_bot.parse_fx_gold_signal) —
    handles bare 'GOLD' mentions (no explicit XAU/USD pair) and zone-right-after-side style.
    Direction + entry zone only; SL/TP always overridden to our own scalp numbers."""
    if not text:
        return None
    t = text.upper()
    m_pair = FXG_PAIR_RE.search(t)
    m_side = re.search(r"\b(BUY|LONG|SELL|SHORT)\b", t)
    if not m_pair and FXG_GOLD_RE.search(t):
        base, quote = "XAU", "USD"
    elif m_pair:
        base, quote = m_pair.group(1), m_pair.group(2)
    else:
        return None
    if base != "XAU" or not m_side:
        return None
    m_entry = re.search(r"ENTRY(?:\s*(?:ZONE|PRICE))?\s*[.:\-]*\s*\**\$?" + FXS_NUM
                        + r"(?:\s*[-–]\s*\$?" + FXS_NUM + r")?", t)
    if not m_entry:
        m_entry = re.search(m_side.group(1) + r"\s+\$?" + FXS_NUM
                            + r"(?:\s*[-–]\s*\$?" + FXS_NUM + r")?", t)
    if not m_entry:
        return None
    e1 = float(m_entry.group(1))
    e2 = float(m_entry.group(2)) if m_entry.group(2) else e1
    return {"symbol_base": base + quote,
            "side": "buy" if m_side.group(1) in ("BUY", "LONG") else "sell",
            "entry_low": min(e1, e2), "entry_high": max(e1, e2),
            "sl": None, "tp": None, "use_our_risk": True}


XAU_ATR_PERIOD = 14
XAU_ATR_MULT = 0.5
XAU_SL_MIN, XAU_SL_MAX = 3.0, 25.0
# 2026-08-05 (Ahmed: manual/our-risk gold sells keep getting stopped by one
# strong push -- "ليه كل الصفقات اليدوية عندي بتخرج بموجب واحد"): the old
# MAX=10 was sized for a normal day. Today's actual H1 ATR ran ~20-22
# (0.5x that = ~10-11) -- the cap was silently clipping the stop right at
# the edge of what current volatility needed, on top of 0.5xATR already
# being tight for a COUNTERTREND (sell-into-an-uptrend) fill specifically.
# Raised so the ATR math has real room in an outlier-volatility regime
# instead of hitting a ceiling calibrated for calmer conditions.

# 2026-08-05 (Ahmed approved the structural follow-up): a SELL into a
# confirmed H1 uptrend (or BUY into a downtrend) is fighting the dominant
# flow. The lazy fix is to skip the setup entirely, not just widen the stop.
# Same H1-EMA50 trend check already validated in Research_ParticipationAnomaly_2026
# (no pandas_ta here to avoid that numba import issue -- plain EMA, same math).


def _calc_ema(values, period):
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def h1_trend_up(symbol):
    """True/False = confirmed H1 trend direction (close vs EMA50), None if
    not enough data -- caller must treat None as 'unknown, don't widen'."""
    try:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 60)
        if rates is None or len(rates) < 55:
            return None
        closes = [float(r["close"]) for r in rates[:-1]]  # exclude forming bar
        ema50 = _calc_ema(closes[-50:], 50)
        return closes[-1] > ema50
    except Exception as e:
        log(f"h1_trend_up error: {e}")
        return None


def _atr_sl_from_bars(bars):
    """bars: list of (high, low, prev_close). Pure math, no MT5 — testable."""
    trs = [max(h - l, abs(h - pc), abs(l - pc)) for h, l, pc in bars]
    atr = sum(trs) / len(trs)
    return max(XAU_SL_MIN, min(XAU_SL_MAX, atr * XAU_ATR_MULT))


def atr_stop_xau(symbol):
    """ATR14 (H1) * 0.5, clamped — falls back to the old fixed $4 if data is
    unavailable (must never block a trade over a data hiccup)."""
    try:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, XAU_ATR_PERIOD + 1)
        if rates is None or len(rates) < XAU_ATR_PERIOD + 1:
            return 4.0
        bars = [(rates[i]["high"], rates[i]["low"], rates[i - 1]["close"]) for i in range(1, len(rates))]
        return _atr_sl_from_bars(bars)
    except Exception as e:
        log(f"atr_stop_xau error: {e} — falling back to fixed $4")
        return 4.0


def is_countertrend(trend_is_up, side):
    return (trend_is_up is True and side == "sell") or (trend_is_up is False and side == "buy")


FX_ATR_PERIOD = 14
FX_ATR_MULT = 0.5
# 2026-08-01 (Ahmed flagged the flat 3-pip stop as "too tight"): same fix as
# gold's atr_stop_xau -- a fixed number is blind to conditions (that's what
# made the OLD 5-pip stop lose -13.86$/0 wins on 07-29, and what made
# swing_pending_bot's 0.8xATR stop lose -52%/-44% in a day). ATR-scaled,
# floored at 3 pips (keeps Ahmed's "tight" ask when the market's quiet),
# capped at 12 (room to breathe when it's genuinely volatile).
FX_SL_MIN_PIPS, FX_SL_MAX_PIPS = 3.0, 12.0


def _fx_atr_sl_from_bars(bars, pip):
    """bars: list of (high, low, prev_close). Pure math, no MT5 — testable."""
    atr = sum(max(h - l, abs(h - pc), abs(l - pc)) for h, l, pc in bars) / len(bars)
    return max(FX_SL_MIN_PIPS * pip, min(FX_SL_MAX_PIPS * pip, atr * FX_ATR_MULT))


def atr_stop_fx(symbol, pip):
    """ATR14(H1)*0.5 in price units, clamped to [3,12] pips. Falls back to
    the floor (3 pips) if H1 data is unavailable -- must never block a trade
    over a data hiccup."""
    try:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, FX_ATR_PERIOD + 1)
        if rates is None or len(rates) < FX_ATR_PERIOD + 1:
            return FX_SL_MIN_PIPS * pip
        bars = [(rates[i]["high"], rates[i]["low"], rates[i - 1]["close"]) for i in range(1, len(rates))]
        return _fx_atr_sl_from_bars(bars, pip)
    except Exception as e:
        log(f"atr_stop_fx error: {e} — falling back to fixed {FX_SL_MIN_PIPS} pips")
        return FX_SL_MIN_PIPS * pip


CRYPTO_ATR_PERIOD = 14
CRYPTO_ATR_MULT = 0.5
# 2026-08-07: port of Oracle tg_signal_bot.py's _crypto_atr_dist (Ahmed-approved
# 2026-07-16 for BAA/Bybit crypto groups) -- same 0.5xATR sizing as gold/FX
# above, but floored as a PERCENT of price (0.3%) instead of a fixed dollar
# amount, since BTC and ETH price levels differ too much for one $ constant
# to make sense for both. No upper clamp, same as the Oracle version.


def _crypto_sl_from_bars(bars, price):
    """bars: list of (high, low, prev_close). Pure math, no MT5 -- testable."""
    atr = sum(max(h - l, abs(h - pc), abs(l - pc)) for h, l, pc in bars) / len(bars)
    return max(atr * CRYPTO_ATR_MULT, price * 0.003)


def atr_stop_crypto(symbol):
    """ATR14(H1)*0.5 in price units, floored at 0.3% of price. Falls back to
    1% of last known price if H1 data or a live tick is unavailable -- must
    never block a trade over a data hiccup (same fallback philosophy as
    atr_stop_xau/atr_stop_fx)."""
    try:
        tick = mt5.symbol_info_tick(symbol)
        price = tick.bid if tick else None
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, CRYPTO_ATR_PERIOD + 1)
        if price is None and rates is not None and len(rates):
            price = rates[-1]["close"]
        if rates is None or len(rates) < CRYPTO_ATR_PERIOD + 1 or price is None:
            return price * 0.01 if price else 0.0
        bars = [(rates[i]["high"], rates[i]["low"], rates[i - 1]["close"]) for i in range(1, len(rates))]
        return _crypto_sl_from_bars(bars, price)
    except Exception as e:
        log(f"atr_stop_crypto error: {e} — falling back to 1% of last close")
        try:
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 1)
            return rates[-1]["close"] * 0.01 if rates is not None and len(rates) else 0.0
        except Exception:
            return 0.0


def _selftest():
    """ponytail: smallest check that fails if the ATR-clamp math breaks."""
    # quiet market: $2 true range per bar -> ATR=2 -> 0.5*2=1, clamped up to XAU_SL_MIN=3
    quiet = [(4000 + i, 3998 + i, 3999 + i) for i in range(15)]
    assert abs(_atr_sl_from_bars(quiet) - XAU_SL_MIN) < 1e-9, "quiet market should clamp to the floor"
    # normal market: $16 true range per bar -> ATR=16 -> 0.5*16=8, inside [3,10]
    normal = [(4016 + i, 4000 + i, 4008 + i) for i in range(15)]
    assert abs(_atr_sl_from_bars(normal) - 8.0) < 1e-9, "normal volatility should size the stop at 0.5xATR"
    # 2026-08-06: XAU_SL_MAX raised 10->25 (see fx_signal_exec ATR Cap Fix) --
    # $40 true range/bar (0.5*40=20) no longer exceeds the ceiling, so this
    # needs enough range to actually clamp at the new value.
    # wild market: $60 true range per bar -> ATR=60 -> 0.5*60=30, clamped down to XAU_SL_MAX=25
    wild = [(4060 + i, 4000 + i, 4030 + i) for i in range(15)]
    assert abs(_atr_sl_from_bars(wild) - XAU_SL_MAX) < 1e-9, "extreme volatility should clamp to the ceiling"
    print("fx_signal_exec ATR selftest OK")

    # same three regimes for the forex ATR stop, pip-denominated (EURUSD, pip=0.0001)
    # each bar's true-range is constant across the 15-bar window on purpose --
    # keeps the expected ATR exact and the test trivial to verify by hand.
    pip = 0.0001
    quiet_fx = [(1.10002, 1.09998, 1.10000)] * 15  # TR=0.00004/bar -> ATR*0.5=0.00002 -> clamp up to 3-pip floor
    assert abs(_fx_atr_sl_from_bars(quiet_fx, pip) - FX_SL_MIN_PIPS * pip) < 1e-9, "quiet fx market should clamp to the 3-pip floor"
    normal_fx = [(1.1016, 1.1000, 1.1008)] * 15  # TR=0.0016/bar -> ATR*0.5=0.0008=8 pips, inside [3,12]
    assert abs(_fx_atr_sl_from_bars(normal_fx, pip) - 8 * pip) < 1e-9, "normal fx volatility should size the stop at 0.5xATR"
    wild_fx = [(1.1040, 1.1000, 1.1020)] * 15  # TR=0.0040/bar -> ATR*0.5=0.0020=20 pips, clamp down to 12-pip ceiling
    assert abs(_fx_atr_sl_from_bars(wild_fx, pip) - FX_SL_MAX_PIPS * pip) < 1e-9, "extreme fx volatility should clamp to the 12-pip ceiling"
    print("fx_signal_exec FX ATR selftest OK")

    # crypto ATR stop: floor is 0.3% of price, no ceiling (BTC/ETH price
    # levels differ too much for one $ constant to make sense for both).
    price = 64000.0
    quiet_crypto = [(price + 20, price - 20, price)] * 15  # TR=40/bar -> 0.5*40=20 -> below 0.3%*64000=192, floors at 192
    assert abs(_crypto_sl_from_bars(quiet_crypto, price) - price * 0.003) < 1e-6, "quiet crypto market should clamp to the 0.3% floor"
    wild_crypto = [(price + 500, price - 500, price)] * 15  # TR=1000/bar -> 0.5*1000=500 -> above the 192 floor, sizes off ATR
    assert abs(_crypto_sl_from_bars(wild_crypto, price) - 500.0) < 1e-6, "volatile crypto market should size the stop at 0.5xATR (no ceiling)"
    print("fx_signal_exec crypto ATR selftest OK")

    # use_our_risk must now be set for crypto (the actual bug this all fixes:
    # a live BINANCE_360 trade executed with the group's raw SL/TP, ~1:13 RR).
    sig = parse_crypto_signal("BTC/USDT LONG\nEntry: 64000\nSL: 62000\n1: 68000")
    assert sig is not None and sig.get("use_our_risk") is True, "crypto signals must use our own risk sizing"
    print("fx_signal_exec crypto use_our_risk selftest OK")

    # 2026-08-04: signal fingerprint must be deterministic (join key between
    # the durable trace file and the compact MT5 comment tag) and must
    # actually distinguish different raw signal text.
    fp1 = _signal_fingerprint("BUY EURUSD 1.1000 SL 1.0990 TP 1.1030")
    fp2 = _signal_fingerprint("BUY EURUSD 1.1000 SL 1.0990 TP 1.1030")
    fp3 = _signal_fingerprint("SELL EURUSD 1.1000 SL 1.1010 TP 1.0970")
    assert fp1 == fp2, "same raw text must produce the same fingerprint"
    assert fp1 != fp3, "different raw text must produce a different fingerprint"
    assert len(fp1) == 8
    print("fx_signal_exec fingerprint selftest OK")

    # comment tag must survive a group name with spaces/punctuation (MT5
    # comment charset is limited) and stay short regardless of input length.
    tag = _comment_tag("Some Group! (v2)", fp1)
    assert tag == f"L2T:SomeGroupv2:{fp1[:6]}", tag
    assert len(tag) <= 26, "comment tag must stay well under MT5's length limit"
    tag_unknown = _comment_tag(None, None)
    assert tag_unknown == "L2T:unknown:"
    print("fx_signal_exec comment-tag selftest OK")

    dummy = type("E", (), {"message": type("M", (), {"date": datetime(2026, 8, 15, 5, 0, 0)})()})()
    assert _event_time_iso(dummy) == "2026-08-15T05:00:00+00:00"
    print("fx_signal_exec event-time selftest OK")

    # 2026-08-11 (Ahmed): EM removed from GOLD_ACCOUNTS -- funded to only $10.78,
    # and 0.01 (min) lot gold risks ~$7-12 = most/all of the account. BTC/forex on
    # EM stay active (smaller risk). Claude re-adds EM once funded to ~$80+. This is
    # a deliberate reversal of the 2026-07-23 lift, for account-size safety not policy.
    assert "EM" not in GOLD_ACCOUNTS, "EM must stay out of GOLD_ACCOUNTS until funded ~$80+ (Ahmed 2026-08-11)"
    print("fx_signal_exec GOLD_ACCOUNTS selftest OK")

    assert is_countertrend(True, "sell")
    assert is_countertrend(False, "buy")
    assert not is_countertrend(True, "buy")
    assert not is_countertrend(False, "sell")
    print("fx_signal_exec countertrend selftest OK")


def execute_on_account(acc_name, cfg, sig, group=None, fingerprint=None, receive_time=None, published_time=None):
    symbol = sig["symbol_base"] + cfg["suffix"]
    try:
        _connect_mt5(cfg, acc_name)
        acc_info = mt5.account_info()
        if acc_info is not None and acc_info.equity < MIN_EQUITY_USD:
            log(f"{acc_name}: SKIPPED insufficient equity ${acc_info.equity:.2f} < ${MIN_EQUITY_USD} floor")
            return
        block_reason = manual_block_reason("fx_signal_exec", acc_name)
        if block_reason:
            log(f"{acc_name}: {symbol} ENTRY BLOCKED -- {block_reason}")
            return
        if os.path.exists(RISK_HALT_FLAG_PATH):
            log(f"{acc_name}: {symbol} ENTRY BLOCKED -- portfolio-wide RISK_HALT.flag active "
                f"(open positions still managed normally by ladder_guard.py/ea_shield.py)")
            return
        if not mt5.symbol_select(symbol, True):
            log(f"{acc_name}: symbol {symbol} not available, skipping.")
            return
        if any(p.symbol == symbol for p in (mt5.positions_get() or [])):
            log(f"{acc_name}: {symbol} already has an open position, skipping.")
            return
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            log(f"{acc_name}: no tick for {symbol}, skipping.")
            return
        price = tick.ask if sig["side"] == "buy" else tick.bid
        lo, hi = sig["entry_low"] * (1 - ENTRY_TOL), sig["entry_high"] * (1 + ENTRY_TOL)
        if not (lo <= price <= hi):
            log(f"{acc_name}: {symbol} price {price} outside entry zone {lo}-{hi}, not chasing.")
            return
        if sig["symbol_base"].startswith("XAU"):
            # 2026-07-17 (Ahmed): adverse-momentum filter — the 22:21 UTC short
            # executed while the current candle was already running against the
            # signal and got stopped in the rally. Same rule as our manual
            # entry-timing rule: skip if price moved >=$3 against the signal's
            # direction from the current M15 candle open.
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 1)
            if rates is not None and len(rates):
                candle_open = float(rates[0][1])
                adverse = (price - candle_open) if sig["side"] == "sell" else (candle_open - price)
                if adverse >= 3.0:
                    log(f"{acc_name}: {symbol} SKIPPED adverse momentum ${adverse:.2f} against {sig['side']} since candle open")
                    return
        if sig.get("use_our_risk"):
            if sig["symbol_base"].startswith("XAU"):
                # ponytail: 2026-07-14 — fixed $4 SL was ~25% of the average H1
                # range (~$16 in current conditions), so ordinary noise clipped
                # it before the real move played out (Ahmed noticed the pattern
                # of trades reversing right after hitting a trailed stop). Size
                # the stop off actual recent volatility (ATR14 on H1) instead,
                # clamped to a sane band so a dead-quiet or a blown-out market
                # doesn't push it to an extreme. RR stays 3:1 as before.
                sl_dist = atr_stop_xau(symbol)
                trend_up = h1_trend_up(symbol)
                countertrend = is_countertrend(trend_up, sig["side"])
                if countertrend:
                    log(f"{acc_name}: {symbol} SKIPPED countertrend {sig['side']} against confirmed H1 EMA50 trend")
                    return
                tp_dist = sl_dist * 3.0
            elif sig["symbol_base"] in CRYPTO_BASES.values():
                # 2026-08-07: crypto (BTC/ETH) was missing this entirely --
                # see the use_our_risk comment in parse_crypto_signal for the
                # incident this fixes. Same 0.5xATR, RR=3:1 approach as gold/FX,
                # ported from the already-proven Oracle tg_signal_bot.py fix.
                sl_dist = atr_stop_crypto(symbol)
                tp_dist = sl_dist * 3.0
            else:
                # 2026-08-01: Ahmed first asked for a flat 3-pip stop (from 5)
                # after the 2026-07-29 losing day, then flagged a flat 3 pips
                # as "too tight" -- same lesson as gold above: ATR-scaled
                # instead, floored at 3 pips (his original ask, held when the
                # market's quiet) and capped at 12 (room to breathe when it
                # genuinely isn't). RR stays 3:1.
                pip = 0.01 if "JPY" in sig["symbol_base"] else 0.0001
                sl_dist = atr_stop_fx(symbol, pip)
                tp_dist = sl_dist * 3.0
            if sig["side"] == "buy":
                sig = {**sig, "sl": price - sl_dist, "tp": price + tp_dist}
            else:
                sig = {**sig, "sl": price + sl_dist, "tp": price - tp_dist}
        order_type = mt5.ORDER_TYPE_BUY if sig["side"] == "buy" else mt5.ORDER_TYPE_SELL
        # 2026-08-04: comment now carries a compact group+fingerprint tag
        # instead of the static "learn2trade" -- this field lives on the
        # order/position itself in MT5's own history, so it survives even a
        # total loss of our own log files (see _write_trace docstring for
        # why this matters). Kept short (MT5 comment limits vary by broker).
        comment_tag = _comment_tag(group, fingerprint)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": GOLD_LOT.get(acc_name, 0.01) if sig["symbol_base"].startswith("XAU") else LOT[acc_name],
            "type": order_type,
            "price": price,
            "sl": sig["sl"],
            "tp": sig["tp"],
            "deviation": 20,
            "magic": 884400,
            "comment": comment_tag,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        _write_trace({
            "phase": "before_send",
            "published_time": published_time,
            "receive_time": receive_time.isoformat() if receive_time else None,
            "sent_time": None,
            "group": group,
            "fingerprint": fingerprint,
            "account": acc_name,
            "symbol": symbol,
            "side": sig["side"],
            "signal_entry_low": sig.get("entry_low"),
            "signal_entry_high": sig.get("entry_high"),
            "signal_sl": sig.get("sl"),
            "signal_tp": sig.get("tp"),
            "planned_price": price,
        })
        result = mt5.order_send(request)
        fill_price = getattr(result, "price", None)
        trace = {
            "phase": "after_send",
            "published_time": published_time,
            "receive_time": receive_time.isoformat() if receive_time else None,
            "sent_time": datetime.now(timezone.utc).isoformat(),
            "group": group,
            "fingerprint": fingerprint,
            "account": acc_name,
            "symbol": symbol,
            "side": sig["side"],
            "sl": sig["sl"],
            "tp": sig["tp"],
            "planned_price": price,
            "fill_price": fill_price,
            "slippage": round((fill_price - price), 6) if isinstance(fill_price, (int, float)) else None,
            "retcode": result.retcode,
            "order_ticket": result.order if result.retcode == mt5.TRADE_RETCODE_DONE else None,
        }
        _write_trace(trace)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            log(f"{acc_name}: order failed {symbol} retcode={result.retcode} {result.comment}")
        else:
            log(f"{acc_name}: EXECUTED {sig['side'].upper()} {symbol} @ {price} fill={fill_price} SL={sig['sl']} TP={sig['tp']}")
    except Exception as e:
        log(f"{acc_name}: error — {e}")
    finally:
        mt5.shutdown()


def _ensure_socks_tunnel():
    """2026-07-19: local ISP started blocking MTProto (HTTPS fine, telethon TimeoutError).
    Route Telegram through a SOCKS tunnel over our Oracle SSH (where Telegram works).
    Self-healing: bat loop restarts us -> we respawn the tunnel if the port is dead."""
    import socket, subprocess
    try:
        s = socket.create_connection(("127.0.0.1", 1080), timeout=2)
        s.close()
        return True
    except OSError:
        pass
    subprocess.Popen(
        ["ssh", "-i", r"C:\Users\ahmed\.ssh\oracle_key", "-N", "-D", "127.0.0.1:1080",
         "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
         "-o", "StrictHostKeyChecking=no", "ubuntu@<REDACTED_ORACLE_IP>"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import time as _t
    for _ in range(10):
        _t.sleep(1)
        try:
            s = socket.create_connection(("127.0.0.1", 1080), timeout=2)
            s.close()
            return True
        except OSError:
            continue
    return False


def main():
    log("fx_signal_exec started — watching LEARN2TRADE for forex signals (EA + BA)")
    tunnel_ok = _ensure_socks_tunnel()
    log(f"socks tunnel via oracle: {'up' if tunnel_ok else 'FAILED — trying direct'}")
    proxy = {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080} if tunnel_ok else None
    client = TelegramClient(SESSION, API_ID, API_HASH, proxy=proxy)

    @client.on(events.NewMessage(chats=[LEARN2TRADE_ID]))
    async def handler(event):
        text = event.raw_text or ""
        write_heartbeat("fx_signal_exec")
        sig = parse_signal(text)
        if sig is None:
            return
        log(f"SIGNAL: {sig}")
        receive_time = datetime.now(timezone.utc)
        published_time = _event_time_iso(event)
        fp = _signal_fingerprint(text)
        _write_shadow({
            "type": "open",
            "id": f"fx-{fp}-{int(receive_time.timestamp())}",
            "ts": receive_time.isoformat(),
            "agent": "fx_signal_exec",
            "group": "LEARN2TRADE",
            "fingerprint": fp,
            "published_time": published_time,
            "received_time": receive_time.isoformat(),
            "symbol": sig["symbol_base"],
            "side": sig["side"],
            "entry_low": sig.get("entry_low"),
            "entry_high": sig.get("entry_high"),
            "sl": sig.get("sl"),
            "tp": sig.get("tp"),
        })
        for acc_name, cfg in ACCOUNTS.items():
            execute_on_account(acc_name, cfg, sig, group="LEARN2TRADE", fingerprint=fp, receive_time=receive_time, published_time=published_time)

    @client.on(events.NewMessage(chats=list(CRYPTO_GROUPS.keys())))
    async def crypto_handler(event):
        text = event.raw_text or ""
        write_heartbeat("fx_signal_exec")
        sig = parse_crypto_signal(text)
        if sig is None:
            return
        group = CRYPTO_GROUPS.get(event.chat_id)
        log(f"CRYPTO SIGNAL ({group}): {sig}")
        receive_time = datetime.now(timezone.utc)
        published_time = _event_time_iso(event)
        fp = _signal_fingerprint(text)
        _write_shadow({
            "type": "open",
            "id": f"fx-{fp}-{int(receive_time.timestamp())}",
            "ts": receive_time.isoformat(),
            "agent": "fx_signal_exec",
            "group": group,
            "fingerprint": fp,
            "published_time": published_time,
            "received_time": receive_time.isoformat(),
            "symbol": sig["symbol_base"],
            "side": sig["side"],
            "entry_low": sig.get("entry_low"),
            "entry_high": sig.get("entry_high"),
            "sl": sig.get("sl"),
            "tp": sig.get("tp"),
        })
        for acc_name in CRYPTO_ACCOUNTS:
            execute_on_account(acc_name, ACCOUNTS[acc_name], sig, group=group, fingerprint=fp, receive_time=receive_time, published_time=published_time)

    @client.on(events.NewMessage(chats=list(FX_GROUPS.keys())))
    async def fx_group_handler(event):
        text = event.raw_text or ""
        write_heartbeat("fx_signal_exec")
        group = FX_GROUPS.get(event.chat_id)
        receive_time = datetime.now(timezone.utc)
        published_time = _event_time_iso(event)
        fp = _signal_fingerprint(text)
        sig = parse_fx_group_signal(text)
        if sig is not None:
            log(f"FX GROUP SIGNAL ({group}): {sig}")
            _write_shadow({
                "type": "open",
                "id": f"fx-{fp}-{int(receive_time.timestamp())}",
                "ts": receive_time.isoformat(),
                "agent": "fx_signal_exec",
                "group": group,
                "fingerprint": fp,
                "published_time": published_time,
                "received_time": receive_time.isoformat(),
                "symbol": sig["symbol_base"],
                "side": sig["side"],
                "entry_low": sig.get("entry_low"),
                "entry_high": sig.get("entry_high"),
                "sl": sig.get("sl"),
                "tp": sig.get("tp"),
            })
            for acc_name, cfg in ACCOUNTS.items():
                execute_on_account(acc_name, cfg, sig, group=group, fingerprint=fp, receive_time=receive_time, published_time=published_time)
            return
        if group in GOLD_ALLOWED_GROUPS:
            gsig = parse_fx_gold_signal(text)
            if gsig is not None:
                night_hour = datetime.now(timezone.utc).hour
                if 0 <= night_hour < 5 and group not in NIGHT_GOLD_GROUPS:
                    log(f"GOLD SIGNAL ({group}) SKIPPED: below night-window conviction bar ({night_hour:02d}:00 UTC): {gsig}")
                    return
                log(f"GOLD SIGNAL ({group}): {gsig}")
                _write_shadow({
                    "type": "open",
                    "id": f"fx-{fp}-{int(receive_time.timestamp())}",
                    "ts": receive_time.isoformat(),
                    "agent": "fx_signal_exec",
                    "group": group,
                    "fingerprint": fp,
                    "published_time": published_time,
                    "received_time": receive_time.isoformat(),
                    "symbol": gsig["symbol_base"],
                    "side": gsig["side"],
                    "entry_low": gsig.get("entry_low"),
                    "entry_high": gsig.get("entry_high"),
                    "sl": gsig.get("sl"),
                    "tp": gsig.get("tp"),
                })
                for acc_name in GOLD_ACCOUNTS:
                    execute_on_account(acc_name, ACCOUNTS[acc_name], gsig, group=group, fingerprint=fp, receive_time=receive_time, published_time=published_time)

    @client.on(events.NewMessage(chats=list(RECORD_ONLY_GROUPS.keys())))
    async def record_only_handler(event):
        # never executes -- logs only, for a future win-rate review before any
        # of these 112 channels could ever be promoted (see RECORD_ONLY_GROUPS).
        text = event.raw_text or ""
        write_heartbeat("fx_signal_exec")
        group = RECORD_ONLY_GROUPS.get(event.chat_id, str(event.chat_id))
        sig = parse_fx_gold_signal(text) or parse_fx_group_signal(text) or parse_crypto_signal(text)
        if sig is not None:
            log_record(f"PARSED ({group}): {sig}")
        else:
            log_record(f"RAW ({group}): {text[:300]!r}")

    client.start()

    # Telethon can lose the connection and keep the process alive without ever
    # raising out of run_until_disconnected (happened twice: 5.5h silent outage
    # 2026-07-08 morning + again same evening). The .bat self-heal loop only
    # restarts on process EXIT, so a dead-connection watchdog must hard-exit.
    import os
    import threading

    def connection_watchdog():
        misses = 0
        while True:
            time.sleep(60)
            write_heartbeat("fx_signal_exec_loop")  # loop-alive layer, mirrors ladder_guard's
                                                      # dual heartbeat -- proves this thread itself
                                                      # hasn't hung, independent of message traffic
            try:
                connected = client.is_connected()
            except Exception:
                connected = False
            misses = misses + 1 if not connected else 0
            if misses >= 3:
                log("connection watchdog: Telegram dead 3+ min — exiting so .bat relaunches")
                os._exit(1)

    threading.Thread(target=connection_watchdog, daemon=True).start()
    client.run_until_disconnected()


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        _selftest()
    else:
        main()
