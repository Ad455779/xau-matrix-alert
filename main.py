import asyncio
import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Optional
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "YOUR_KEY_HERE")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL", "60"))
TREND_LOOKBACK = 3
ALERT_HISTORY_MAX = 50
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
MATRIX = {
"UP-DOWN-DOWN": {"signal": "FORTEMENT HAUSSIER", "conviction": 5, "action": "LONG - entree "UP-DOWN-UP": {"signal": "HAUSSIER MODERE", "conviction": 3, "action": "LONG - taille "UP-UP-DOWN": {"signal": "NEUTRE / HAUSSIER", "conviction": 2, "action": "LONG - attendre "DOWN-DOWN-DOWN": {"signal": "NEUTRE / BAISSIER", "conviction": 2, "action": "FLAT ou SHORT "DOWN-UP-UP": {"signal": "FORTEMENT BAISSIER", "conviction": 5, "action": "SHORT - entree "DOWN-UP-DOWN": {"signal": "BAISSIER MODERE", "conviction": 3, "action": "SHORT - taille "UP-UP-UP": {"signal": "NEUTRE", "conviction": 1, "action": "FLAT - forces "DOWN-DOWN-UP": {"signal": "NEUTRE", "conviction": 1, "action": "FLAT - forces }
SYMBOLS = {
"GDX": "GDX",
"DXY": "UUP",
"TIPS": "TIP",
}
price_history = {k: deque(maxlen=TREND_LOOKBACK + 1) for k in SYMBOLS}
last_prices = {k: None for k in SYMBOLS}
current_trends = {k: None for k in SYMBOLS}
current_signal = None
alert_history = deque(maxlen=ALERT_HISTORY_MAX)
ws_clients = []
app = FastAPI(title="XAU/USD Matrix Alert API", version="1.0.0")
app.add_middleware(
CORSMiddleware,
allow_origins=["*"],
allow_methods=["*"],
allow_headers=["*"],
)
async def fetch_quote(symbol: str) -> Optional[float]:
url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
try:
async with httpx.AsyncClient(timeout=10) as client:
r = await client.get(url)
r.raise_for_status()
price = r.json().get("c")
if price and float(price) > 0:
logger.info(f" {symbol} -> {price:.4f}")
return float(price)
except Exception as e:
logger.warning(f"Finnhub error [{symbol}]: {e}")
return None
def compute_trend(prices: deque) -> Optional[str]:
if len(prices) < 2:
return None
latest = prices[-1]
prior_avg = sum(list(prices)[:-1]) / (len(prices) - 1)
pct = (latest - prior_avg) / prior_avg * 100
if pct > 0.10: return "UP"
elif pct < -0.10: return "DOWN"
return None
def evaluate_matrix(trends: dict) -> Optional[dict]:
gdx = trends.get("GDX")
dxy = trends.get("DXY")
tips = trends.get("TIPS")
if not all([gdx, dxy, tips]):
return None
tips_key = "DOWN" if tips == "UP" else "UP"
key = f"{gdx}-{dxy}-{tips_key}"
row = MATRIX.get(key)
if row:
return {**row, "key": key, "tips_etf_trend": tips, "tips_yield_direction": tips_key}
return None
async def broadcast(payload: dict):
dead = []
for ws in ws_clients:
try:
await ws.send_json(payload)
except Exception:
dead.append(ws)
for ws in dead:
ws_clients.remove(ws)
async def poll_and_evaluate():
global current_signal, current_trends
logger.info("Poll cycle starting")
for asset_id, symbol in SYMBOLS.items():
price = await fetch_quote(symbol)
if price is not None:
last_prices[asset_id] = price
price_history[asset_id].append(price)
new_trends = {k: compute_trend(price_history[k]) for k in SYMBOLS}
current_trends = new_trends
new_signal = evaluate_matrix(new_trends)
timestamp = datetime.now(timezone.utc).isoformat()
payload = {
"type": "state_update",
"timestamp": timestamp,
"prices": {k: round(v, 4) if v else None for k, v in last_prices.items()},
"trends": current_trends,
"signal": new_signal,
"data_points": {k: len(v) for k, v in price_history.items()},
}
prev_key = current_signal.get("key") if current_signal else None
new_key = new_signal.get("key") if new_signal else None
if new_signal and new_key != prev_key:
alert = {
"type": "alert",
"timestamp": timestamp,
"signal": new_signal["signal"],
"conviction": new_signal["conviction"],
"action": new_signal["action"],
"bias": new_signal["bias"],
"key": new_key,
"trends": current_trends,
"prices": payload["prices"],
}
alert_history.appendleft(alert)
payload["alert"] = alert
logger.info(f"ALERT -> {new_signal['signal']} (conviction {new_signal['conviction']}/current_signal = new_signal
await broadcast(payload)
logger.info(f"Broadcast -> {len(ws_clients)} client(s)")
scheduler = AsyncIOScheduler()
@app.on_event("startup")
async def startup():
scheduler.add_job(poll_and_evaluate, "interval", seconds=POLL_INTERVAL_SECONDS, id="poll")
scheduler.start()
logger.info(f"Scheduler started - poll every {POLL_INTERVAL_SECONDS}s")
asyncio.create_task(poll_and_evaluate())
@app.on_event("shutdown")
async def shutdown():
scheduler.shutdown()
@app.get("/")
async def root():
return {"status": "ok", "service": "XAU/USD Matrix Alert API v1.0"}
@app.get("/state")
async def get_state():
return {
"timestamp": datetime.now(timezone.utc).isoformat(),
"prices": last_prices,
"trends": current_trends,
"signal": current_signal,
"data_points": {k: len(v) for k, v in price_history.items()},
}
@app.get("/history")
async def get_history():
return {"alerts": list(alert_history)}
@app.get("/matrix")
async def get_matrix():
return {"matrix": MATRIX, "symbols": SYMBOLS}
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
await ws.accept()
ws_clients.append(ws)
logger.info(f"WS connected - total: {len(ws_clients)}")
try:
await ws.send_json({
"type": "connected",
"timestamp": datetime.now(timezone.utc).isoformat(),
"prices": last_prices,
"trends": current_trends,
"signal": current_signal,
"history": list(alert_history)[:10],
})
while True:
await ws.receive_text()
except WebSocketDisconnect:
pass
except Exception as e:
logger.warning(f"WS error: {e}")
finally:
if ws in ws_clients:
ws_clients.remove(ws)
logger.info(f"WS disconnected - total: {len(ws_clients)}")
