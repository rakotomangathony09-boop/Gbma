#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║          GOLD BY MC ANTHONIO — TRADING TERMINAL              ║
║          Senior Quant | SMC + ICT Venom | PAXG/USDT         ║
╚══════════════════════════════════════════════════════════════╝
Single-file FastAPI app: backend + embedded HTML/CSS/JS frontend
Run: pip install fastapi uvicorn websockets httpx python-binance
     python gold_mc_anthonio.py
"""

import asyncio
import json
import math
import time
import hashlib
import hmac
import logging
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
BINANCE_API_KEY    = "NF1bq2dK9gQld7dJQJ9A9XahSFj3bxGEsMxKnKw802eRpEKhubJiQdlOlgR3Tj3D"
BINANCE_SECRET     = "wOqjKNT9aRd5dQOal7gyNBGpa0CHVV61Coo52jam2PZQLwJxCOJ8sMOZwmmZge8Z"
FINNHUB_KEY        = "d6og8phr01qnu98huumgd6og8phr01qnu98huun0"
SYMBOL             = "PAXGUSDT"
CAPITAL_USD        = 10.0
RISK_PCT           = 0.02          # 2% per trade
RR_BREAKEVEN       = 1.5
CANDLE_LIMIT       = 200
EMA_FAST           = 9
EMA_SLOW           = 21
MDG_TZ             = timezone(timedelta(hours=3))   # Madagascar UTC+3
WEEKEND_KILL_FROM  = (4, 22, 0)    # Friday 22:00
WEEKEND_KILL_TO    = (6, 23, 0)    # Sunday 23:00

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("GOLD_MC")

# ─────────────────────────────────────────────
#  GLOBAL STATE
# ─────────────────────────────────────────────
state = {
    "power": False,
    "price": 0.0,
    "bid": 0.0,
    "ask": 0.0,
    "spread": 0.0,
    "candles": [],          # list of OHLCV dicts
    "ob_bids": [],
    "ob_asks": [],
    "signal": None,         # latest signal dict
    "probability": 0,
    "news": [],
    "sentiment": 0.0,       # -1..+1
    "judas_active": False,
    "weekend_kill": False,
    "last_bos": None,
    "last_choch": None,
    "fvg_zones": [],
    "absorption": False,
    "trailing_sl": None,
    "account_balance": CAPITAL_USD,
    "open_pnl": 0.0,
    "trades_today": 0,
    "win_rate": 0.0,
    "last_update": "",
    "error": "",
}

clients: list[WebSocket] = []
bg_task: Optional[asyncio.Task] = None


# ─────────────────────────────────────────────
#  MATH / QUANT HELPERS
# ─────────────────────────────────────────────
def ema(values: list, period: int) -> float:
    if len(values) < period:
        return values[-1] if values else 0.0
    k = 2 / (period + 1)
    result = sum(values[:period]) / period
    for v in values[period:]:
        result = v * k + result * (1 - k)
    return result

def atr(candles: list, period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, min(period + 1, len(candles))):
        h = candles[-i]["h"]
        l = candles[-i]["l"]
        pc = candles[-i - 1]["c"] if i + 1 <= len(candles) - 1 else candles[-i]["o"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0

def detect_bos_choch(candles: list) -> tuple[str, float]:
    """Break of Structure / Change of Character detection."""
    if len(candles) < 5:
        return "", 0.0
    highs = [c["h"] for c in candles[-20:]]
    lows  = [c["l"] for c in candles[-20:]]
    last_close = candles[-1]["c"]
    # Swing high/low
    swing_high = max(highs[:-2])
    swing_low  = min(lows[:-2])
    if last_close > swing_high:
        return "BOS_BULL", swing_high
    if last_close < swing_low:
        return "BOS_BEAR", swing_low
    # CHoCH: recent opposing swing broken
    mid_high = max(highs[len(highs)//2:])
    mid_low  = min(lows[len(lows)//2:])
    if last_close > mid_high and last_close < swing_high:
        return "CHOCH_BULL", mid_high
    if last_close < mid_low and last_close > swing_low:
        return "CHOCH_BEAR", mid_low
    return "", 0.0

def detect_fvg(candles: list) -> list:
    """Fair Value Gaps (ICT Venom)."""
    zones = []
    for i in range(2, len(candles)):
        c0, c1, c2 = candles[i-2], candles[i-1], candles[i]
        # Bullish FVG: gap between c0 high and c2 low
        if c2["l"] > c0["h"]:
            zones.append({"type": "bull", "top": c2["l"], "bot": c0["h"],
                          "mid": (c2["l"] + c0["h"]) / 2})
        # Bearish FVG
        if c2["h"] < c0["l"]:
            zones.append({"type": "bear", "top": c0["l"], "bot": c2["h"],
                          "mid": (c0["l"] + c2["h"]) / 2})
    return zones[-5:] if zones else []

def detect_absorption(ob_bids: list, ob_asks: list, price: float) -> bool:
    """Order book absorption: huge bid wall being consumed at key level."""
    if not ob_bids or not ob_asks:
        return False
    top_bid_qty = sum(float(b[1]) for b in ob_bids[:5])
    top_ask_qty = sum(float(a[1]) for a in ob_asks[:5])
    ratio = top_bid_qty / (top_ask_qty + 1e-9)
    return ratio > 2.5 or ratio < 0.4

def lot_size(capital: float, risk_pct: float, sl_pips: float, price: float) -> float:
    """Fractional lot for small accounts."""
    risk_usd = capital * risk_pct
    if sl_pips <= 0 or price <= 0:
        return 0.0
    lot = risk_usd / (sl_pips * price)
    return round(max(lot, 0.000001), 6)

def probability_score(bos: str, fvg_zones: list, absorption: bool,
                      sentiment: float, judas: bool, price: float) -> int:
    score = 0
    if "BULL" in bos:
        score += 25
    elif "BEAR" in bos:
        score += 20
    if fvg_zones:
        near = [z for z in fvg_zones if abs(z["mid"] - price) / price < 0.002]
        score += min(len(near) * 15, 30)
    if absorption:
        score += 20
    if abs(sentiment) > 0.3:
        score += 10
    if judas:
        score -= 15  # Judas swing: wait
    return max(0, min(score, 100))

def weekend_kill_active() -> bool:
    now = datetime.now(MDG_TZ)
    wd = now.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
    h, m = now.hour, now.minute
    if wd == 4 and (h > 22 or (h == 22 and m >= 0)):
        return True
    if wd == 5:
        return True
    if wd == 6 and (h < 23 or (h == 23 and m == 0)):
        return True
    return False

def build_signal(candles, fvg_zones, bos, bos_lvl, absorption,
                 price, sentiment, judas) -> Optional[dict]:
    if len(candles) < 20 or price <= 0:
        return None
    at = atr(candles)
    sl_dist = at * 1.5
    tp_dist = sl_dist * RR_BREAKEVEN

    direction = None
    if "BULL" in bos and sentiment >= -0.5 and not judas:
        direction = "LONG"
    elif "BEAR" in bos and sentiment <= 0.5 and not judas:
        direction = "SHORT"

    if not direction:
        return None

    entry = price
    sl = entry - sl_dist if direction == "LONG" else entry + sl_dist
    tp = entry + tp_dist if direction == "LONG" else entry - tp_dist
    sl_pips = abs(entry - sl)
    qty = lot_size(state["account_balance"], RISK_PCT, sl_pips, entry)

    prob = probability_score(bos, fvg_zones, absorption, sentiment, judas, price)

    return {
        "direction":  direction,
        "entry":      round(entry, 4),
        "sl":         round(sl, 4),
        "tp":         round(tp, 4),
        "qty":        qty,
        "atr":        round(at, 4),
        "probability":prob,
        "bos":        bos,
        "fvg_count":  len(fvg_zones),
        "absorption": absorption,
        "sentiment":  round(sentiment, 3),
        "judas":      judas,
        "ts":         datetime.now(MDG_TZ).strftime("%H:%M:%S"),
    }


# ─────────────────────────────────────────────
#  BINANCE REST
# ─────────────────────────────────────────────
async def fetch_candles(client: httpx.AsyncClient, symbol: str, interval="5m", limit=CANDLE_LIMIT):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        r = await client.get(url, timeout=8)
        raw = r.json()
        candles = []
        for k in raw:
            candles.append({
                "t": int(k[0]),
                "o": float(k[1]), "h": float(k[2]),
                "l": float(k[3]), "c": float(k[4]),
                "v": float(k[5]),
            })
        return candles
    except Exception as e:
        log.error(f"candles: {e}")
        return []

async def fetch_orderbook(client: httpx.AsyncClient, symbol: str):
    url = f"https://api.binance.com/api/v3/depth?symbol={symbol}&limit=10"
    try:
        r = await client.get(url, timeout=5)
        data = r.json()
        return data.get("bids", []), data.get("asks", [])
    except:
        return [], []

async def fetch_ticker(client: httpx.AsyncClient, symbol: str):
    url = f"https://api.binance.com/api/v3/ticker/bookTicker?symbol={symbol}"
    try:
        r = await client.get(url, timeout=5)
        data = r.json()
        bid = float(data.get("bidPrice", 0))
        ask = float(data.get("askPrice", 0))
        return bid, ask, (bid + ask) / 2
    except:
        return 0, 0, 0


# ─────────────────────────────────────────────
#  FINNHUB
# ─────────────────────────────────────────────
async def fetch_finnhub_sentiment(client: httpx.AsyncClient) -> tuple[float, list]:
    """Fetch USD/Gold news sentiment from Finnhub."""
    url = f"https://finnhub.io/api/v1/news?category=forex&token={FINNHUB_KEY}"
    news_items = []
    sentiment = 0.0
    try:
        r = await client.get(url, timeout=8)
        data = r.json()
        if isinstance(data, list):
            for item in data[:5]:
                headline = item.get("headline", "")
                news_items.append(headline[:80])
            # Simple keyword sentiment
            bullish_kw = ["rise", "rally", "surge", "bullish", "gain", "higher", "record"]
            bearish_kw = ["fall", "drop", "slump", "bearish", "loss", "lower", "crash"]
            for h in news_items:
                hl = h.lower()
                for w in bullish_kw:
                    if w in hl:
                        sentiment += 0.2
                for w in bearish_kw:
                    if w in hl:
                        sentiment -= 0.2
            sentiment = max(-1.0, min(1.0, sentiment))
    except Exception as e:
        log.warning(f"Finnhub: {e}")
    return sentiment, news_items


# ─────────────────────────────────────────────
#  BACKGROUND ENGINE
# ─────────────────────────────────────────────
async def trading_engine():
    async with httpx.AsyncClient() as client:
        news_tick = 0
        while state["power"]:
            try:
                # Weekend kill-switch
                wk = weekend_kill_active()
                state["weekend_kill"] = wk
                if wk:
                    state["signal"] = None
                    await broadcast({"type": "state", "data": safe_state()})
                    await asyncio.sleep(30)
                    continue

                # Market data
                bid, ask, price = await fetch_ticker(client, SYMBOL)
                if price > 0:
                    state["price"]  = price
                    state["bid"]    = bid
                    state["ask"]    = ask
                    state["spread"] = round(ask - bid, 4)

                # Candles
                candles = await fetch_candles(client, SYMBOL)
                if candles:
                    state["candles"] = candles

                # Order book
                bids, asks = await fetch_orderbook(client, SYMBOL)
                state["ob_bids"] = bids
                state["ob_asks"] = asks

                # SMC
                bos, bos_lvl = detect_bos_choch(state["candles"])
                state["last_bos"] = bos

                # FVG
                fvgs = detect_fvg(state["candles"])
                state["fvg_zones"] = fvgs

                # Absorption
                absorption = detect_absorption(bids, asks, price)
                state["absorption"] = absorption

                # Judas Swing heuristic: if price moved >0.3% in last 2 candles with no structure
                judas = False
                if len(candles) >= 3:
                    c1, c2 = candles[-2], candles[-1]
                    move = abs(c2["c"] - c1["o"]) / (c1["o"] + 1e-9)
                    if move > 0.003 and not bos:
                        judas = True
                state["judas_active"] = judas

                # News every 60s
                news_tick += 1
                if news_tick >= 12:
                    news_tick = 0
                    sentiment, news = await fetch_finnhub_sentiment(client)
                    state["sentiment"] = sentiment
                    state["news"] = news

                # Signal
                sig = build_signal(
                    state["candles"], fvgs, bos, bos_lvl,
                    absorption, price, state["sentiment"], judas
                )
                state["signal"]      = sig
                state["probability"] = sig["probability"] if sig else 0
                state["last_update"] = datetime.now(MDG_TZ).strftime("%H:%M:%S")

                await broadcast({"type": "state", "data": safe_state()})

            except asyncio.CancelledError:
                break
            except Exception as e:
                state["error"] = str(e)
                log.error(f"Engine error: {e}")

            await asyncio.sleep(5)


def safe_state():
    """Return JSON-serialisable state snapshot."""
    return {
        "power":        state["power"],
        "price":        state["price"],
        "bid":          state["bid"],
        "ask":          state["ask"],
        "spread":       state["spread"],
        "signal":       state["signal"],
        "probability":  state["probability"],
        "news":         state["news"],
        "sentiment":    state["sentiment"],
        "judas":        state["judas_active"],
        "weekend_kill": state["weekend_kill"],
        "last_bos":     state["last_bos"],
        "fvg_count":    len(state["fvg_zones"]),
        "absorption":   state["absorption"],
        "last_update":  state["last_update"],
        "error":        state["error"],
        "balance":      state["account_balance"],
        "open_pnl":     state["open_pnl"],
        "trades_today": state["trades_today"],
        "win_rate":     state["win_rate"],
        "fvg_zones":    state["fvg_zones"][:3],
        "ob_bids":      state["ob_bids"][:5],
        "ob_asks":      state["ob_asks"][:5],
    }


async def broadcast(msg: dict):
    dead = []
    for ws in clients:
        try:
            await ws.send_json(msg)
        except:
            dead.append(ws)
    for ws in dead:
        clients.remove(ws)


# ─────────────────────────────────────────────
#  FASTAPI APP
# ─────────────────────────────────────────────
app = FastAPI(title="GOLD BY MC ANTHONIO")


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.append(ws)
    await ws.send_json({"type": "state", "data": safe_state()})
    try:
        while True:
            data = await ws.receive_json()
            await handle_command(data)
    except WebSocketDisconnect:
        if ws in clients:
            clients.remove(ws)


async def handle_command(data: dict):
    global bg_task
    cmd = data.get("cmd")
    if cmd == "power_on":
        state["power"] = True
        state["error"] = ""
        if bg_task is None or bg_task.done():
            bg_task = asyncio.create_task(trading_engine())
        await broadcast({"type": "state", "data": safe_state()})
    elif cmd == "power_off":
        state["power"] = False
        if bg_task and not bg_task.done():
            bg_task.cancel()
        state["signal"] = None
        await broadcast({"type": "state", "data": safe_state()})
    elif cmd == "set_balance":
        bal = float(data.get("value", CAPITAL_USD))
        state["account_balance"] = max(bal, 10.0)
        await broadcast({"type": "state", "data": safe_state()})


# ─────────────────────────────────────────────
#  HTML PAGE  (single-file embedded)
# ─────────────────────────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>GOLD BY MC ANTHONIO</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Bebas+Neue&family=Rajdhani:wght@400;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.tailwindcss.com"></script>
<style>
:root {
  --gold: #f0a500;
  --gold2: #ffd560;
  --dark: #080b10;
  --panel: #0d1117;
  --border: #1e2a38;
  --green: #00e676;
  --red: #ff1744;
  --blue: #00b0ff;
  --muted: #4a5568;
  --text: #c9d1d9;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--dark);
  color: var(--text);
  font-family: 'Rajdhani', sans-serif;
  min-height: 100vh;
  overflow-x: hidden;
}
/* Scanline overlay */
body::before {
  content: '';
  position: fixed; inset: 0; pointer-events: none; z-index: 9999;
  background: repeating-linear-gradient(0deg,
    transparent, transparent 2px,
    rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px);
}
/* Gold grid bg */
body::after {
  content: '';
  position: fixed; inset: 0; pointer-events: none; z-index: 0;
  background-image:
    linear-gradient(rgba(240,165,0,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(240,165,0,0.03) 1px, transparent 1px);
  background-size: 40px 40px;
}

.mono { font-family: 'Share Tech Mono', monospace; }
.bebas { font-family: 'Bebas Neue', cursive; }

/* Panel */
.panel {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 4px;
  position: relative;
  overflow: hidden;
}
.panel::before {
  content: '';
  position: absolute; top: 0; left: 0; right: 0; height: 1px;
  background: linear-gradient(90deg, transparent, var(--gold), transparent);
}

/* Glow effects */
.glow-gold { text-shadow: 0 0 12px var(--gold), 0 0 24px rgba(240,165,0,0.4); }
.glow-green { text-shadow: 0 0 10px var(--green); }
.glow-red   { text-shadow: 0 0 10px var(--red); }

/* Power button */
#powerBtn {
  width: 72px; height: 72px;
  border-radius: 50%;
  border: 3px solid var(--muted);
  background: transparent;
  cursor: pointer;
  font-size: 28px;
  transition: all .3s;
  position: relative;
  display: flex; align-items: center; justify-content: center;
}
#powerBtn.on {
  border-color: var(--gold);
  box-shadow: 0 0 20px var(--gold), 0 0 40px rgba(240,165,0,0.3);
  animation: pulse-gold 2s infinite;
}
@keyframes pulse-gold {
  0%,100% { box-shadow: 0 0 20px var(--gold), 0 0 40px rgba(240,165,0,0.2); }
  50%      { box-shadow: 0 0 30px var(--gold), 0 0 60px rgba(240,165,0,0.5); }
}

/* Probability bar */
#probBar {
  height: 8px;
  background: linear-gradient(90deg, var(--red), #ff9800, var(--green));
  border-radius: 4px;
  transition: clip-path .5s;
}

/* Signal card */
#signalCard {
  transition: all .4s;
}
.signal-long  { border-color: var(--green) !important; }
.signal-short { border-color: var(--red) !important; }

/* Ticker tape */
#tape {
  white-space: nowrap;
  animation: scroll-left 30s linear infinite;
}
@keyframes scroll-left {
  from { transform: translateX(100%); }
  to   { transform: translateX(-100%); }
}

/* OB depth bars */
.bid-bar { background: rgba(0,230,118,0.2); }
.ask-bar { background: rgba(255,23,68,0.2); }

/* Blink */
.blink { animation: blink 1s step-end infinite; }
@keyframes blink { 50% { opacity: 0; } }

/* Weekend overlay */
#weekendOverlay {
  display: none;
  position: fixed; inset: 0; z-index: 100;
  background: rgba(8,11,16,0.95);
  align-items: center; justify-content: center;
  flex-direction: column;
}
#weekendOverlay.show { display: flex; }

/* Copy flash */
@keyframes copyFlash {
  0%   { background: var(--gold); color: #000; }
  100% { background: transparent; color: var(--gold); }
}
.copy-flash { animation: copyFlash .4s ease-out; }

/* Scrollbar */
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-track { background: var(--dark); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

.stat-val { font-size: 1.4rem; font-weight: 700; }
</style>
</head>
<body class="relative z-10">

<!-- Weekend Kill Overlay -->
<div id="weekendOverlay">
  <div class="text-center">
    <div class="bebas text-7xl glow-red" style="color:var(--red)">MARKET CLOSED</div>
    <div class="mono text-xl mt-4" style="color:var(--muted)">Weekend Kill-Switch Active</div>
    <div class="mono text-lg mt-2" style="color:var(--gold)">Friday 22:00 → Sunday 23:00 (EAT/MDG)</div>
    <div class="mono text-3xl mt-6" id="wkClock"></div>
  </div>
</div>

<!-- HEADER -->
<header class="panel mx-4 mt-4 p-4 flex items-center justify-between z-10 relative">
  <div>
    <div class="bebas text-5xl glow-gold" style="color:var(--gold)">GOLD BY MC ANTHONIO</div>
    <div class="mono text-xs mt-1" style="color:var(--muted)">SMC · ICT VENOM · PAXG/USDT · SNIPER MODE</div>
  </div>
  <div class="flex items-center gap-6">
    <!-- Clock -->
    <div class="text-right">
      <div class="mono text-xs" style="color:var(--muted)">MADAGASCAR TIME (EAT)</div>
      <div class="mono text-2xl" id="clock" style="color:var(--gold2)">--:--:--</div>
      <div class="mono text-xs" id="dateStr" style="color:var(--muted)"></div>
    </div>
    <!-- Power -->
    <div class="flex flex-col items-center gap-1">
      <button id="powerBtn" onclick="togglePower()">⏻</button>
      <span class="mono text-xs" id="powerLabel" style="color:var(--muted)">OFF</span>
    </div>
  </div>
</header>

<!-- TAPE -->
<div class="overflow-hidden mx-4 mt-2 py-1" style="border-bottom:1px solid var(--border)">
  <div id="tape" class="mono text-xs" style="color:var(--gold)">
    PAXG/USDT · SMC ANALYSIS LOADING · WAITING FOR MARKET DATA…
  </div>
</div>

<!-- MAIN GRID -->
<main class="grid grid-cols-12 gap-3 p-4 relative z-10" style="min-height:calc(100vh - 160px)">

  <!-- LEFT COL: Price + Stats -->
  <div class="col-span-12 lg:col-span-3 flex flex-col gap-3">

    <!-- Live Price -->
    <div class="panel p-4">
      <div class="mono text-xs mb-1" style="color:var(--muted)">PAXG/USDT · LIVE</div>
      <div class="bebas text-5xl glow-gold" id="livePrice" style="color:var(--gold2)">-.----</div>
      <div class="flex justify-between mt-2 mono text-xs">
        <span>BID: <span id="bidPrice" style="color:var(--green)">-</span></span>
        <span>ASK: <span id="askPrice" style="color:var(--red)">-</span></span>
        <span>SPR: <span id="spread" style="color:var(--blue)">-</span></span>
      </div>
    </div>

    <!-- Account Stats -->
    <div class="panel p-4">
      <div class="mono text-xs mb-3" style="color:var(--muted)">ACCOUNT · SMALL CAP</div>
      <div class="grid grid-cols-2 gap-3">
        <div>
          <div class="mono text-xs" style="color:var(--muted)">BALANCE</div>
          <div class="stat-val" style="color:var(--gold)">$<span id="balance">10.00</span></div>
        </div>
        <div>
          <div class="mono text-xs" style="color:var(--muted)">OPEN P&L</div>
          <div class="stat-val" id="openPnl">$0.00</div>
        </div>
        <div>
          <div class="mono text-xs" style="color:var(--muted)">TRADES/DAY</div>
          <div class="stat-val" style="color:var(--blue)"><span id="tradesDay">0</span></div>
        </div>
        <div>
          <div class="mono text-xs" style="color:var(--muted)">WIN RATE</div>
          <div class="stat-val" style="color:var(--green)"><span id="winRate">0</span>%</div>
        </div>
      </div>
      <div class="mt-3">
        <div class="mono text-xs mb-1" style="color:var(--muted)">SET BALANCE ($)</div>
        <div class="flex gap-2">
          <input id="balInput" type="number" min="10" step="1" value="10"
            class="mono text-sm flex-1 px-2 py-1 rounded"
            style="background:var(--dark);border:1px solid var(--border);color:var(--gold)"/>
          <button onclick="setBalance()" class="mono text-xs px-3 py-1 rounded"
            style="background:var(--gold);color:#000;font-weight:700">SET</button>
        </div>
      </div>
    </div>

    <!-- Structure Analysis -->
    <div class="panel p-4">
      <div class="mono text-xs mb-3" style="color:var(--muted)">SMC STRUCTURE</div>
      <div class="flex flex-col gap-2">
        <div class="flex justify-between items-center">
          <span class="mono text-xs">BOS/CHoCH</span>
          <span class="mono text-sm font-bold" id="bosLabel">—</span>
        </div>
        <div class="flex justify-between items-center">
          <span class="mono text-xs">FVG ZONES</span>
          <span class="mono text-sm" id="fvgCount" style="color:var(--blue)">0</span>
        </div>
        <div class="flex justify-between items-center">
          <span class="mono text-xs">ABSORPTION</span>
          <span class="mono text-sm" id="absorptionLabel">—</span>
        </div>
        <div class="flex justify-between items-center">
          <span class="mono text-xs">JUDAS SWING</span>
          <span class="mono text-sm" id="judaLabel">—</span>
        </div>
      </div>
    </div>
  </div>

  <!-- CENTER COL: Signal + Probability -->
  <div class="col-span-12 lg:col-span-6 flex flex-col gap-3">

    <!-- Probability Score -->
    <div class="panel p-4">
      <div class="flex justify-between items-center mb-3">
        <div class="mono text-xs" style="color:var(--muted)">SNIPER PROBABILITY SCORE</div>
        <div class="mono text-3xl font-bold" id="probScore" style="color:var(--gold)">0%</div>
      </div>
      <div style="background:rgba(255,255,255,0.05);border-radius:4px;height:8px;overflow:hidden">
        <div id="probBar" style="height:100%;width:0%;transition:width .5s;background:linear-gradient(90deg,var(--red),#ff9800,var(--green))"></div>
      </div>
      <div class="flex justify-between mono text-xs mt-1" style="color:var(--muted)">
        <span>AVOID</span><span>WATCH</span><span>SNIPER ENTRY</span>
      </div>
    </div>

    <!-- Signal Card -->
    <div id="signalCard" class="panel p-5" style="border:2px solid var(--border);min-height:240px">
      <div id="noSignal" class="flex flex-col items-center justify-center h-40 gap-3">
        <div class="text-5xl">🎯</div>
        <div class="mono text-sm" style="color:var(--muted)">Awaiting sniper entry conditions…</div>
        <div class="mono text-xs" style="color:var(--border)">Power ON + Weekend unlocked required</div>
      </div>
      <div id="hasSignal" class="hidden">
        <div class="flex justify-between items-start mb-4">
          <div>
            <div class="mono text-xs mb-1" style="color:var(--muted)">SIGNAL · <span id="sigTime">--:--:--</span></div>
            <div class="bebas text-5xl" id="sigDirection">LONG</div>
            <div class="mono text-xs mt-1" id="sigBos" style="color:var(--muted)"></div>
          </div>
          <div class="flex flex-col gap-2 items-end">
            <button onclick="copySignal()" id="copyBtn"
              class="mono text-xs px-4 py-2 rounded border"
              style="border-color:var(--gold);color:var(--gold);background:transparent;cursor:pointer">
              📋 COPY SIGNAL
            </button>
            <div class="mono text-xs" style="color:var(--muted)">PAXG/USDT · BINANCE</div>
          </div>
        </div>

        <div class="grid grid-cols-3 gap-4 mb-4">
          <div class="text-center">
            <div class="mono text-xs" style="color:var(--muted)">ENTRY</div>
            <div class="mono text-xl font-bold" id="sigEntry" style="color:var(--gold2)">-</div>
          </div>
          <div class="text-center">
            <div class="mono text-xs" style="color:var(--red)">STOP LOSS</div>
            <div class="mono text-xl font-bold" id="sigSL" style="color:var(--red)">-</div>
          </div>
          <div class="text-center">
            <div class="mono text-xs" style="color:var(--green)">TAKE PROFIT</div>
            <div class="mono text-xl font-bold" id="sigTP" style="color:var(--green)">-</div>
          </div>
        </div>

        <div class="grid grid-cols-4 gap-2">
          <div class="panel p-2 text-center">
            <div class="mono text-xs" style="color:var(--muted)">QTY</div>
            <div class="mono text-sm" id="sigQty" style="color:var(--gold)">-</div>
          </div>
          <div class="panel p-2 text-center">
            <div class="mono text-xs" style="color:var(--muted)">ATR</div>
            <div class="mono text-sm" id="sigAtr" style="color:var(--blue)">-</div>
          </div>
          <div class="panel p-2 text-center">
            <div class="mono text-xs" style="color:var(--muted)">RR</div>
            <div class="mono text-sm" style="color:var(--gold)">1:1.5</div>
          </div>
          <div class="panel p-2 text-center">
            <div class="mono text-xs" style="color:var(--muted)">RISK</div>
            <div class="mono text-sm" style="color:var(--red)">2%</div>
          </div>
        </div>

        <!-- Trailing BE note -->
        <div class="mt-3 mono text-xs" style="color:var(--muted)">
          🔄 Trailing Breakeven activates at 1:1.5 R/R · Structural SL placement
        </div>
      </div>
    </div>

    <!-- Sentiment + News -->
    <div class="panel p-4">
      <div class="mono text-xs mb-2" style="color:var(--muted)">USD/GOLD NEWS SENTIMENT · FINNHUB</div>
      <div class="flex items-center gap-4 mb-3">
        <div class="mono text-xs">SENTIMENT:</div>
        <div id="sentGauge" class="flex-1 h-4 rounded relative"
          style="background:linear-gradient(90deg,var(--red),#555,var(--green))">
          <div id="sentPtr" class="absolute top-0 bottom-0 w-1 rounded"
            style="background:white;left:50%;transition:left .5s;"></div>
        </div>
        <div class="mono text-sm font-bold" id="sentScore">0.00</div>
      </div>
      <div id="newsList" class="flex flex-col gap-1">
        <div class="mono text-xs" style="color:var(--border)">No news loaded yet…</div>
      </div>
    </div>
  </div>

  <!-- RIGHT COL: Order Book + FVG -->
  <div class="col-span-12 lg:col-span-3 flex flex-col gap-3">

    <!-- Order Book -->
    <div class="panel p-4">
      <div class="mono text-xs mb-3" style="color:var(--muted)">ORDER BOOK DEPTH</div>
      <div class="mono text-xs mb-1" style="color:var(--red)">ASKS</div>
      <div id="obAsks" class="flex flex-col gap-1 mb-2"></div>
      <div class="h-px my-2" style="background:var(--border)"></div>
      <div class="mono text-xs mb-1" style="color:var(--green)">BIDS</div>
      <div id="obBids" class="flex flex-col gap-1"></div>
      <div class="mt-3 flex justify-between items-center">
        <span class="mono text-xs" style="color:var(--muted)">ABSORPTION</span>
        <span class="mono text-sm font-bold" id="obAbsorption">—</span>
      </div>
    </div>

    <!-- FVG Zones -->
    <div class="panel p-4">
      <div class="mono text-xs mb-3" style="color:var(--muted)">ICT VENOM · FVG ZONES</div>
      <div id="fvgList" class="flex flex-col gap-2">
        <div class="mono text-xs" style="color:var(--border)">No zones detected</div>
      </div>
    </div>

    <!-- System Log -->
    <div class="panel p-4 flex-1">
      <div class="mono text-xs mb-2" style="color:var(--muted)">SYSTEM STATUS</div>
      <div id="systemLog" class="mono text-xs flex flex-col gap-1" style="max-height:180px;overflow-y:auto"></div>
      <div class="mt-2 flex justify-between mono text-xs" style="color:var(--border)">
        <span>LAST UPDATE: <span id="lastUpdate">—</span></span>
      </div>
    </div>
  </div>

</main>

<!-- FOOTER -->
<footer class="mx-4 mb-4 py-2 text-center mono text-xs" style="color:var(--border);border-top:1px solid var(--border)">
  GOLD BY MC ANTHONIO · PAXG/USDT · SNIPER MODE · 2% RISK · 1:1.5 R/R · NOT FINANCIAL ADVICE
</footer>

<!-- AUDIO (Web Audio API beep) -->
<script>
const AudioCtx = window.AudioContext || window.webkitAudioContext;
let audioCtx = null;
function beep(freq=880, duration=0.15, type='sine') {
  if (!audioCtx) audioCtx = new AudioCtx();
  const o = audioCtx.createOscillator();
  const g = audioCtx.createGain();
  o.connect(g); g.connect(audioCtx.destination);
  o.type = type; o.frequency.value = freq;
  g.gain.setValueAtTime(0.3, audioCtx.currentTime);
  g.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + duration);
  o.start(); o.stop(audioCtx.currentTime + duration);
}
function sniperBeep() {
  beep(660, 0.1); setTimeout(()=>beep(880, 0.15), 120); setTimeout(()=>beep(1100, 0.2), 270);
}

// ── WebSocket ──
let ws, power = false, lastSigDir = null;
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'state') render(msg.data);
  };
  ws.onclose = () => setTimeout(connect, 2000);
}
connect();

function togglePower() {
  if (!audioCtx) audioCtx = new AudioCtx(); // unlock audio on click
  ws.send(JSON.stringify({ cmd: power ? 'power_off' : 'power_on' }));
}
function setBalance() {
  const v = parseFloat(document.getElementById('balInput').value) || 10;
  ws.send(JSON.stringify({ cmd: 'set_balance', value: v }));
}

// ── Clock ──
function updateClock() {
  const now = new Date();
  // Madagascar UTC+3
  const mdg = new Date(now.getTime() + (3 * 60 - now.getTimezoneOffset()) * 60000);
  document.getElementById('clock').textContent =
    mdg.toTimeString().slice(0,8);
  document.getElementById('dateStr').textContent =
    mdg.toDateString();
}
setInterval(updateClock, 1000); updateClock();

// ── Weekend overlay clock ──
function updateWkClock() {
  const now = new Date();
  const mdg = new Date(now.getTime() + (3 * 60 - now.getTimezoneOffset()) * 60000);
  document.getElementById('wkClock').textContent = mdg.toTimeString().slice(0,8);
}
setInterval(updateWkClock, 1000);

// ── Render ──
function render(d) {
  power = d.power;
  const btn = document.getElementById('powerBtn');
  const lbl = document.getElementById('powerLabel');
  if (d.power) { btn.classList.add('on'); lbl.textContent = 'ON'; lbl.style.color = 'var(--gold)'; }
  else          { btn.classList.remove('on'); lbl.textContent = 'OFF'; lbl.style.color = 'var(--muted)'; }

  // Weekend overlay
  const wkEl = document.getElementById('weekendOverlay');
  d.weekend_kill ? wkEl.classList.add('show') : wkEl.classList.remove('show');

  // Price
  if (d.price) {
    document.getElementById('livePrice').textContent = d.price.toFixed(4);
    document.getElementById('bidPrice').textContent  = d.bid.toFixed(4);
    document.getElementById('askPrice').textContent  = d.ask.toFixed(4);
    document.getElementById('spread').textContent    = d.spread.toFixed(4);
  }

  // Account
  document.getElementById('balance').textContent   = (d.balance||10).toFixed(2);
  const pnlEl = document.getElementById('openPnl');
  pnlEl.textContent = '$' + (d.open_pnl||0).toFixed(2);
  pnlEl.style.color = (d.open_pnl||0) >= 0 ? 'var(--green)' : 'var(--red)';
  document.getElementById('tradesDay').textContent = d.trades_today||0;
  document.getElementById('winRate').textContent   = (d.win_rate||0).toFixed(0);

  // Probability
  const prob = d.probability||0;
  document.getElementById('probScore').textContent = prob + '%';
  document.getElementById('probBar').style.width = prob + '%';

  // SMC
  const bos = d.last_bos || '';
  const bosEl = document.getElementById('bosLabel');
  if (bos.includes('BULL')) { bosEl.textContent = bos; bosEl.style.color = 'var(--green)'; }
  else if (bos.includes('BEAR')) { bosEl.textContent = bos; bosEl.style.color = 'var(--red)'; }
  else { bosEl.textContent = '—'; bosEl.style.color = 'var(--muted)'; }

  document.getElementById('fvgCount').textContent = d.fvg_count||0;

  const absEl = document.getElementById('absorptionLabel');
  if (d.absorption) { absEl.textContent = 'DETECTED ⚡'; absEl.style.color = 'var(--gold)'; }
  else { absEl.textContent = 'None'; absEl.style.color = 'var(--muted)'; }

  const judEl = document.getElementById('judaLabel');
  if (d.judas) { judEl.textContent = 'ACTIVE ⚠'; judEl.style.color = 'var(--red)'; }
  else { judEl.textContent = 'Clear'; judEl.style.color = 'var(--green)'; }

  // Signal
  const noSig = document.getElementById('noSignal');
  const hasSig = document.getElementById('hasSignal');
  const sigCard = document.getElementById('signalCard');
  if (d.signal) {
    const s = d.signal;
    noSig.classList.add('hidden');
    hasSig.classList.remove('hidden');
    sigCard.style.borderColor = s.direction === 'LONG' ? 'var(--green)' : 'var(--red)';
    const dirEl = document.getElementById('sigDirection');
    dirEl.textContent = s.direction;
    dirEl.style.color = s.direction === 'LONG' ? 'var(--green)' : 'var(--red)';
    document.getElementById('sigTime').textContent   = s.ts||'--:--:--';
    document.getElementById('sigEntry').textContent  = s.entry?.toFixed(4)||'-';
    document.getElementById('sigSL').textContent     = s.sl?.toFixed(4)||'-';
    document.getElementById('sigTP').textContent     = s.tp?.toFixed(4)||'-';
    document.getElementById('sigQty').textContent    = s.qty||'-';
    document.getElementById('sigAtr').textContent    = s.atr||'-';
    document.getElementById('sigBos').textContent    = `Structure: ${s.bos||'—'} | FVG: ${s.fvg_count} | Absorption: ${s.absorption}`;
    // Sound alert on new signal direction
    if (s.direction !== lastSigDir && prob >= 60) { sniperBeep(); }
    lastSigDir = s.direction;
  } else {
    noSig.classList.remove('hidden');
    hasSig.classList.add('hidden');
    sigCard.style.borderColor = 'var(--border)';
    lastSigDir = null;
  }

  // Sentiment
  const sent = d.sentiment||0;
  document.getElementById('sentScore').textContent = sent.toFixed(2);
  document.getElementById('sentScore').style.color = sent > 0 ? 'var(--green)' : sent < 0 ? 'var(--red)' : 'var(--muted)';
  const pct = ((sent + 1) / 2 * 100).toFixed(1);
  document.getElementById('sentPtr').style.left = pct + '%';

  // News
  if (d.news && d.news.length) {
    document.getElementById('newsList').innerHTML = d.news.map(n =>
      `<div class="mono text-xs py-1" style="border-bottom:1px solid var(--border);color:var(--text)">▸ ${n}</div>`
    ).join('');
    document.getElementById('tape').textContent = d.news.join(' · ');
  }

  // Order book
  renderOB('obAsks', d.ob_asks||[], 'ask');
  renderOB('obBids', d.ob_bids||[], 'bid');
  const obAbsEl = document.getElementById('obAbsorption');
  if (d.absorption) { obAbsEl.textContent = '⚡ ABSORBING'; obAbsEl.style.color = 'var(--gold)'; }
  else { obAbsEl.textContent = 'Normal'; obAbsEl.style.color = 'var(--muted)'; }

  // FVG
  const fvgEl = document.getElementById('fvgList');
  if (d.fvg_zones && d.fvg_zones.length) {
    fvgEl.innerHTML = d.fvg_zones.map(z =>
      `<div class="panel px-3 py-2 flex justify-between items-center">
        <span class="mono text-xs font-bold" style="color:${z.type==='bull'?'var(--green)':'var(--red)'}">
          ${z.type.toUpperCase()} FVG
        </span>
        <span class="mono text-xs">${z.bot?.toFixed(2)} — ${z.top?.toFixed(2)}</span>
      </div>`
    ).join('');
  }

  // Log
  const logEl = document.getElementById('systemLog');
  const ts = d.last_update||'—';
  const entry = `<div style="color:var(--muted)">[${ts}] P=${d.price?.toFixed(4)||'-'} BOS=${d.last_bos||'—'} PROB=${d.probability||0}%</div>`;
  logEl.insertAdjacentHTML('afterbegin', entry);
  while (logEl.children.length > 20) logEl.removeChild(logEl.lastChild);
  document.getElementById('lastUpdate').textContent = ts;
  if (d.error) {
    const errEl = `<div style="color:var(--red)">[ERR] ${d.error}</div>`;
    logEl.insertAdjacentHTML('afterbegin', errEl);
  }
}

function renderOB(elId, rows, side) {
  const el = document.getElementById(elId);
  if (!rows.length) { el.innerHTML = '<div class="mono text-xs" style="color:var(--border)">—</div>'; return; }
  const maxQty = Math.max(...rows.map(r => parseFloat(r[1]||0)));
  el.innerHTML = rows.map(r => {
    const price = parseFloat(r[0]||0).toFixed(4);
    const qty   = parseFloat(r[1]||0).toFixed(4);
    const pct   = maxQty > 0 ? (parseFloat(r[1]) / maxQty * 100).toFixed(0) : 0;
    const color = side === 'bid' ? 'var(--green)' : 'var(--red)';
    const bg    = side === 'bid' ? 'rgba(0,230,118,0.07)' : 'rgba(255,23,68,0.07)';
    return `<div class="mono text-xs flex justify-between px-1 py-0.5 rounded relative overflow-hidden"
               style="background:linear-gradient(90deg,${bg} ${pct}%,transparent ${pct}%)">
              <span style="color:${color}">${price}</span>
              <span style="color:var(--muted)">${qty}</span>
            </div>`;
  }).join('');
}

function copySignal() {
  const s = document.getElementById;
  const dir = s('sigDirection').textContent;
  const entry = s('sigEntry').textContent;
  const sl = s('sigSL').textContent;
  const tp = s('sigTP').textContent;
  const qty = s('sigQty').textContent;
  const prob = document.getElementById('probScore').textContent;
  const ts = s('sigTime').textContent;
  const text = `🎯 GOLD BY MC ANTHONIO — SNIPER SIGNAL\n` +
    `━━━━━━━━━━━━━━━━━━━━━━\n` +
    `Pair:    PAXG/USDT (Binance)\n` +
    `Signal:  ${dir}\n` +
    `Entry:   ${entry}\n` +
    `SL:      ${sl}\n` +
    `TP:      ${tp} (1:1.5 R/R)\n` +
    `Qty:     ${qty} PAXG\n` +
    `Prob:    ${prob}\n` +
    `Time:    ${ts} (MDG/EAT)\n` +
    `━━━━━━━━━━━━━━━━━━━━━━\n` +
    `Risk: 2% | Trailing BE at 1:1.5`;
  navigator.clipboard.writeText(text).catch(()=>{});
  const btn = document.getElementById('copyBtn');
  btn.textContent = '✅ COPIED!';
  btn.classList.add('copy-flash');
  setTimeout(()=>{ btn.textContent = '📋 COPY SIGNAL'; btn.classList.remove('copy-flash'); }, 2000);
}
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════════════╗
║          GOLD BY MC ANTHONIO — TRADING TERMINAL v1.0         ║
║          PAXG/USDT · SMC + ICT Venom · Sniper Mode           ║
╠══════════════════════════════════════════════════════════════╣
║  → http://localhost:8000                                      ║
║  Dependencies: fastapi uvicorn httpx                          ║
╚══════════════════════════════════════════════════════════════╝
""")
    uvicorn.run("__main__:app", host="0.0.0.0", port=8000, reload=False, log_level="warning")
