#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║          GOLD BY MC ANTHONIO — TRADING TERMINAL              ║
║          Senior Quant | SMC + ICT Venom | PAXG/USDT         ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# ─────────────────────────────────────────────
#  CONFIG & STATE
# ─────────────────────────────────────────────
BINANCE_API_KEY = "NF1bq2dK9gQld7dJQJ9A9XahSFj3bxGEsMxKnKw802eRpEKhubJiQdlOlgR3Tj3D"
BINANCE_SECRET  = "wOqjKNT9aRd5dQOal7gyNBGpa0CHVV61Coo52jam2PZQLwJxCOJ8sMOZwmmZge8Z"
SYMBOL          = "PAXGUSDT"
MDG_TZ          = timezone(timedelta(hours=3)) # Madagascar EAT

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("GOLD_MC")

state = {
    "power": True, # Activé par défaut pour Render
    "price": 0.0,
    "bid": 0.0,
    "ask": 0.0,
    "spread": 0.0,
    "probability": 0,
    "last_update": "",
    "error": ""
}

clients = []
app = FastAPI()

# ─────────────────────────────────────────────
#  MOTEUR DE DONNÉES (CORRIGÉ)
# ─────────────────────────────────────────────
async def trading_engine():
    async with httpx.AsyncClient() as client:
        while True:
            if state["power"]:
                try:
                    # Récupération directe du ticker Binance
                    url = f"https://api.binance.com/api/v3/ticker/bookTicker?symbol={SYMBOL}"
                    r = await client.get(url, timeout=5)
                    if r.status_code == 200:
                        data = r.json()
                        bid = float(data.get("bidPrice", 0))
                        ask = float(data.get("askPrice", 0))
                        state["bid"] = bid
                        state["ask"] = ask
                        state["price"] = (bid + ask) / 2
                        state["spread"] = round(ask - bid, 4)
                        state["last_update"] = datetime.now(MDG_TZ).strftime("%H:%M:%S")
                        state["error"] = ""
                    else:
                        state["error"] = f"Binance API Error: {r.status_code}"
                except Exception as e:
                    state["error"] = f"Connexion Error: {str(e)}"
                
                await broadcast({"type": "state", "data": state})
            await asyncio.sleep(2) # Mise à jour toutes les 2 secondes

async def broadcast(msg):
    for ws in clients:
        try:
            await ws.send_json(msg)
        except:
            clients.remove(ws)

# ─────────────────────────────────────────────
#  FRONTEND HTML & JS (CORRIGÉ POUR L'HEURE)
# ─────────────────────────────────────────────
HTML_PAGE = """
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <title>GOLD BY MC ANTHONIO</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        :root { --gold: #f0a500; --dark: #080b10; }
        body { background: var(--dark); color: #c9d1d9; font-family: sans-serif; }
        .glow-gold { text-shadow: 0 0 15px var(--gold); }
    </style>
</head>
<body class="p-4 md:p-10">
    <div class="max-w-5xl mx-auto border border-yellow-700/30 p-6 rounded-lg bg-black/40">
        <header class="flex justify-between items-center border-b border-yellow-900/50 pb-6 mb-8">
            <div>
                <h1 class="text-4xl font-bold text-yellow-500 glow-gold uppercase">Gold By Mc Anthonio</h1>
                <p class="text-xs text-gray-500 font-mono mt-1">SMC • ICT VENOM • PAXG/USDT</p>
            </div>
            <div class="text-right">
                <p class="text-[10px] text-gray-500 font-mono">MADAGASCAR TIME (EAT)</p>
                <p id="clock" class="text-3xl text-yellow-400 font-mono font-bold">--:--:--</p>
                <p id="dateStr" class="text-xs text-gray-600 font-mono"></p>
            </div>
        </header>

        <main class="grid grid-cols-1 md:grid-cols-2 gap-8">
            <div class="bg-gray-900/50 p-8 rounded-xl border border-gray-800 text-center">
                <p class="text-xs text-gray-400 font-mono mb-2 uppercase">Live Price</p>
                <p id="livePrice" class="text-7xl font-bold text-yellow-100 font-mono">-.----</p>
                <div class="flex justify-between mt-6 text-sm font-mono border-t border-gray-800 pt-4">
                    <span class="text-green-500">BID: <span id="bidPrice">-</span></span>
                    <span class="text-red-500">ASK: <span id="askPrice">-</span></span>
                </div>
            </div>

            <div class="flex flex-col items-center justify-center">
                <button id="powerBtn" onclick="togglePower()" class="w-24 h-24 rounded-full border-4 border-gray-700 text-4xl transition-all duration-300">⏻</button>
                <p id="powerStatus" class="mt-3 font-mono text-gray-500 uppercase tracking-widest">Off</p>
            </div>
        </main>
    </div>

    <script>
        let ws;
        function connect() {
            const protocol = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
            ws = new WebSocket(protocol + window.location.host + '/ws');
            ws.onmessage = (e) => {
                const d = JSON.parse(e.data).data;
                document.getElementById('livePrice').innerText = d.price.toFixed(4);
                document.getElementById('bidPrice').innerText = d.bid.toFixed(4);
                document.getElementById('askPrice').innerText = d.ask.toFixed(4);
                
                const btn = document.getElementById('powerBtn');
                const status = document.getElementById('powerStatus');
                if(d.power) {
                    btn.style.borderColor = '#f0a500';
                    btn.style.color = '#f0a500';
                    btn.style.boxShadow = '0 0 20px rgba(240, 165, 0, 0.3)';
                    status.innerText = 'System Live';
                    status.classList.add('text-yellow-500');
                } else {
                    btn.style.borderColor = '#374151';
                    btn.style.color = '#374151';
                    btn.style.boxShadow = 'none';
                    status.innerText = 'Off';
                    status.classList.remove('text-yellow-500');
                }
            };
            ws.onclose = () => setTimeout(connect, 2000);
        }

        // CORRECTION EXPERTE DE L'HEURE (MADAGASCAR)
        function updateClock() {
            const now = new Date();
            const options = { 
                timeZone: 'Indian/Antananarivo', 
                hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false 
            };
            const timeStr = new Intl.DateTimeFormat('fr-FR', options).format(now);
            const dateStr = now.toLocaleDateString('fr-FR', { timeZone: 'Indian/Antananarivo', weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' });
            
            document.getElementById('clock').innerText = timeStr;
            document.getElementById('dateStr').innerText = dateStr;
        }

        setInterval(updateClock, 1000);
        updateClock();
        connect();

        function togglePower() {
            ws.send(JSON.stringify({cmd: 'toggle'}));
        }
    </script>
</body>
</html>
"""

# ─────────────────────────────────────────────
#  ROUTES & STARTUP
# ─────────────────────────────────────────────
@app.get("/")
async def index(): return HTMLResponse(HTML_PAGE)

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.append(ws)
    try:
        while True:
            data = await ws.receive_json()
            if data.get('cmd') == 'toggle':
                state["power"] = not state["power"]
                await broadcast({"type": "state", "data": state})
    except WebSocketDisconnect:
        if ws in clients: clients.remove(ws)

@app.on_event("startup")
async def startup():
    asyncio.create_task(trading_engine())

if __name__ == "__main__":
    # Gestion du port pour Render
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
