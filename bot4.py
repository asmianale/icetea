import os, time, json, hmac, hashlib, requests, websocket, threading, math, csv, urllib.parse, logging
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SMC_Bot")

# --- CONFIG (GLOBAL MUTABLE) ---
API_KEY = os.getenv("BINANCE_API_KEY")
SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")
MODE = os.getenv("MODE", "TESTNET")
BASE_URL = "https://testnet.binancefuture.com" if MODE == "TESTNET" else "https://fapi.binance.com"
WS_BASE = "wss://stream.binancefuture.com" if MODE == "TESTNET" else "wss://fstream.binance.com"
MARGIN_USDT = float(os.getenv("MARGIN_USDT", 1.0))
LEVERAGE = int(os.getenv("LEVERAGE", 10))
MIN_RR = float(os.getenv("MIN_RR", 1.5))
SL_BUFFER_PCT = float(os.getenv("SL_BUFFER_PCT", 0.05))

symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "SUIUSDT", "AVAXUSDT", "BNBUSDT", "TRXUSDT"]

# --- GLOBAL STATE ---
klines_data = {s: {"1m": [], "5m": [], "15m": [], "1h": [], "4h": []} for s in symbols}
live_prices = {s: 0.0 for s in symbols}
config = {"ENABLE_1H": True, "ENABLE_4H": True}
positions = {}
active_signals = {}
CSV_FILE = "riwayat_trading.csv"

total_pnl = 0.0
total_wins = 0
total_losses = 0
current_month_str = datetime.now().strftime("%Y-%m")
state_lock = threading.Lock()

# --- ENV UPDATER ---
def update_env_file(key, value):
    """Menulis ulang file .env agar perubahan permanen"""
    env_path = ".env"
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            lines = f.readlines()
    
    found = False
    with open(env_path, "w") as f:
        for line in lines:
            if line.startswith(f"{key}="):
                f.write(f"{key}={value}\n")
                found = True
            else:
                f.write(line)
        if not found:
            f.write(f"{key}={value}\n")

# --- UTILS ---
def sync_time():
    try:
        url = BASE_URL + "/fapi/v1/time"
        response = requests.get(url, timeout=5).json()
        return response["serverTime"] - int(time.time() * 1000)
    except: return 0

time_offset = sync_time()
def ts(): return int(time.time() * 1000 + time_offset)

def send_telegram(msg):
    def run():
        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if token and chat_id:
            try:
                safe_msg = msg.replace("_", " ")
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                requests.post(url, json={"chat_id": chat_id, "text": safe_msg, "parse_mode": "Markdown"}, timeout=10)
            except: pass
    threading.Thread(target=run, daemon=True).start()

precisions = {}
def load_precisions():
    try:
        info = requests.get(BASE_URL + "/fapi/v1/exchangeInfo", timeout=10).json()
        for s in info["symbols"]:
            if s["symbol"] in symbols:
                f = {flt["filterType"]: flt for flt in s["filters"]}
                precisions[s["symbol"]] = {
                    "tick": max(0, int(round(-math.log10(float(f["PRICE_FILTER"]["tickSize"]))))),
                    "step": max(0, int(round(-math.log10(float(f["LOT_SIZE"]["stepSize"]))))),
                    "min_qty": float(f["LOT_SIZE"]["minQty"]),
                    "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 5))
                }
    except: pass

def round_v(v, p):
    if p > 0: return f"{round(v, p):.{p}f}"
    else: return str(int(round(v)))

def post_api(params, endpoint, method="POST"):
    params["timestamp"] = ts()
    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(SECRET_KEY.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    url = f"{BASE_URL}{endpoint}"
    try:
        if method == "POST": r = requests.post(url, headers=headers, data=f"{query_string}&signature={signature}", timeout=10)
        elif method == "DELETE": r = requests.delete(f"{url}?{query_string}&signature={signature}", headers=headers, timeout=10)
        elif method == "GET": r = requests.get(f"{url}?{query_string}&signature={signature}", headers=headers, timeout=10)
        return r.json()
    except: return {"error": "API Request Failed"}

# ==============================================================================
# LOGIKA SMC — VIRGIN FVG (IDENTIK DENGAN V8.2)
# ==============================================================================
def get_unmitigated_poi(candles, depth=20, min_size_pct=0.1): 
    if len(candles) < depth + 4: return []
    pois = []
    start_idx = max(0, len(candles) - depth)
    for i in range(start_idx, len(candles) - 3):
        c0, c1, c2 = candles[i], candles[i+1], candles[i+2]
        if c0["h"] < c2["l"] and c1["c"] > c1["o"]:
            if (c2["l"] - c0["h"]) / c0["h"] * 100 >= min_size_pct:
                fvg_b, fvg_t = c0["h"], c2["l"]
                is_valid = True
                for j in range(i+3, len(candles) - 1):
                    if candles[j]["l"] <= fvg_t: is_valid = False; break 
                if is_valid: pois.append({"dir": "BUY", "fvg_b": fvg_b, "fvg_t": fvg_t, "ob_b": c0["l"], "ob_t": c0["h"], "t": c0["t"]})
        elif c0["l"] > c2["h"] and c1["c"] < c1["o"]:
            if (c0["l"] - c2["h"]) / c2["h"] * 100 >= min_size_pct:
                fvg_t, fvg_b = c0["l"], c2["h"]
                is_valid = True
                for j in range(i+3, len(candles) - 1):
                    if candles[j]["h"] >= fvg_b: is_valid = False; break 
                if is_valid: pois.append({"dir": "SELL", "fvg_b": fvg_b, "fvg_t": fvg_t, "ob_b": c0["l"], "ob_t": c0["h"], "t": c0["t"]})
    return pois

def get_target(candles, direction, depth=20):
    recent = candles[-depth:]
    if len(recent) < 3: return None
    if direction == "BUY":
        highs = [c["h"] for i, c in enumerate(recent[1:-1]) if c["h"] > recent[i]["h"] and c["h"] > recent[i+2]["h"]]
        return highs[-1] if highs else None
    else:
        lows = [c["l"] for i, c in enumerate(recent[1:-1]) if c["l"] < recent[i]["l"] and c["l"] < recent[i+2]["l"]]
        return lows[-1] if lows else None

# ==============================================================================
# ENGINE (V8.2 CORE)
# ==============================================================================
class Engine:
    def __init__(self, symbol, mode):
        self.symbol, self.mode = symbol, mode
        self.ignored_pois = []
        self.reset()
        if mode == "1H_BIAS": self.tf_poi, self.tf_conf, self.tf_trig = "1h", "15m", "1m"
        else: self.tf_poi, self.tf_conf, self.tf_trig = "4h", "1h", "5m"
        self.last_signal_time, self.cooldown = 0, 300

    def reset(self):
        self.state, self.direction = "IDLE", None
        self.active_poi, self.active_zone = None, None
        self.fc1, self.fc2 = None, None
        self.entry, self.sl, self.tp = None, None, None
        self.pending_order_id, self.pending_sl_algo_id = None, None
        self.setup_time, self.last_processed_conf_t = None, 0

    def check_and_trigger(self, c_trig, c_conf, scan_depth):
        ltf_scan = c_trig[-scan_depth:]; if len(ltf_scan) < 6: return False
        for i in range(len(ltf_scan)-1, 4, -1):
            curr = ltf_scan[i]; body = abs(curr["c"] - curr["o"]); wick = curr["h"] - curr["l"]
            if wick > 0 and body / wick > 0.6:
                prev_5 = ltf_scan[i-5:i]
                highs, lows = [max(x["o"], x["c"]) for x in prev_5], [min(x["o"], x["c"]) for x in prev_5]
                if self.direction == "BUY" and curr["c"] > max(highs):
                    cisd_low = min(c["l"] for c in ltf_scan[i-5:i+1])
                    if cisd_low > self.active_poi["fvg_t"] or cisd_low < self.active_poi["ob_b"]: continue
                    self.entry = curr["c"] - (body * 0.25)
                    self.sl = cisd_low - (cisd_low * SL_BUFFER_PCT / 100)
                    if self.sl >= self.entry: continue 
                    self.tp = get_target(c_conf, self.direction) or (self.entry + (self.entry-self.sl)*2)
                    if (self.tp-self.entry)/(self.entry-self.sl) >= MIN_RR:
                        self.state, self.last_signal_time = "WAIT_ENTRY", time.time(); self.place_limit_and_sl(); return True
                elif self.direction == "SELL" and curr["c"] < min(lows):
                    cisd_high = max(c["h"] for c in ltf_scan[i-5:i+1])
                    if cisd_high < self.active_poi["fvg_b"] or cisd_high > self.active_poi["ob_t"]: continue
                    self.entry = curr["c"] + (body * 0.25)
                    self.sl = cisd_high + (cisd_high * SL_BUFFER_PCT / 100)
                    if self.sl <= self.entry: continue
                    self.tp = get_target(c_conf, self.direction) or (self.entry - (self.sl-self.entry)*2)
                    if (self.entry-self.tp)/(self.sl-self.entry) >= MIN_RR:
                        self.state, self.last_signal_time = "WAIT_ENTRY", time.time(); self.place_limit_and_sl(); return True
        return False

    def tick(self):
        if (self.mode == "1H_BIAS" and not config["ENABLE_1H"]) or (self.mode == "4H_BIAS" and not config["ENABLE_4H"]):
            if self.state != "IDLE": self.cancel_pending_orders(); self.reset()
            return
        if self.symbol in positions: return
        c_p, c_c, c_t, price = klines_data[self.symbol][self.tf_poi], klines_data[self.symbol][self.tf_conf], klines_data[self.symbol][self.tf_trig], live_prices[self.symbol]
        if not c_p or price == 0: return
        if time.time() - self.last_signal_time < self.cooldown and self.state == "IDLE": return
        if self.setup_time and time.time() - self.setup_time > 3600 and self.state != "WAIT_ENTRY":
            if self.active_poi: self.ignored_pois.append(self.active_poi["t"])
            self.reset(); return
        
        if self.state == "IDLE":
            pois = get_unmitigated_poi(c_p)
            for poi in reversed(pois):
                if poi["t"] in self.ignored_pois: continue
                in_f = poi["fvg_b"] <= price <= poi["fvg_t"]; in_o = poi["ob_b"] <= price <= poi["ob_t"]
                if in_f or in_o: self.direction, self.active_poi, self.active_zone, self.state, self.setup_time = poi["dir"], poi, ("FVG" if in_f else "OB"), "WAIT_C1", time.time(); return
        elif self.state == "WAIT_C1":
            if len(c_c) > 0 and c_c[-1]["t"] != self.last_processed_conf_t:
                self.last_processed_conf_t = c_c[-1]["t"]
                if self.check_and_trigger(c_t, c_c, 30): return
        elif self.state == "WAIT_ENTRY":
            last_k = c_t[-1] if len(c_t) > 0 else None
            if self.direction == "BUY":
                if price >= self.tp or price <= self.sl or (last_k and (last_k["h"] >= self.tp or last_k["l"] <= self.sl)):
                    self.cancel_pending_orders(); self.ignored_pois.append(self.active_poi["t"]); self.reset()
            elif self.direction == "SELL":
                if price <= self.tp or price >= self.sl or (last_k and (last_k["l"] <= self.tp or last_k["h"] >= self.sl)):
                    self.cancel_pending_orders(); self.ignored_pois.append(self.active_poi["t"]); self.reset()

    def place_limit_and_sl(self):
        def run():
            p = precisions.get(self.symbol); q = (MARGIN_USDT * LEVERAGE) / self.entry
            if q < p["min_qty"] or q * self.entry < p["min_notional"]: self.reset(); return
            post_api({"symbol": self.symbol, "leverage": LEVERAGE}, "/fapi/v1/leverage")
            with state_lock: active_signals[self.symbol] = {"mode": self.mode, "tp": self.tp, "sl": self.sl, "dir": self.direction, "qty": q}
            res = post_api({"symbol": self.symbol, "side": self.direction, "type": "LIMIT", "quantity": round_v(q, p["step"]), "price": round_v(self.entry, p["tick"]), "timeInForce": "GTC"}, "/fapi/v1/order")
            if "orderId" in res:
                self.pending_order_id = res["orderId"]; opp = "SELL" if self.direction == "BUY" else "BUY"
                sl_res = post_api({"symbol": self.symbol, "side": opp, "algoType": "CONDITIONAL", "type": "STOP_MARKET", "triggerPrice": round_v(self.sl, p["tick"]), "quantity": round_v(q, p["step"]), "reduceOnly": "true"}, "/fapi/v1/algoOrder")
                if "algoId" in sl_res: self.pending_sl_algo_id = sl_res["algoId"]
                send_telegram(f"⏳ *{self.symbol}* LIMIT + SL\n💰 Entry: `{round_v(self.entry, p['tick'])}` | SL: `{round_v(self.sl, p['tick'])}`")
            else: self.reset()
        threading.Thread(target=run, daemon=True).start()

    def cancel_pending_orders(self):
        if self.pending_order_id: post_api({"symbol": self.symbol, "orderId": self.pending_order_id}, "/fapi/v1/order", method="DELETE")
        if self.pending_sl_algo_id: post_api({"symbol": self.symbol, "algoId": self.pending_sl_algo_id}, "/fapi/v1/algoOrder", method="DELETE")

# ==============================================================================
# 📱 TELEGRAM CMD (V8.3 - REMOTE CONTROL ADDED)
# ==============================================================================
def telegram_cmd():
    global total_pnl, total_wins, total_losses, API_KEY, SECRET_KEY, MARGIN_USDT, LEVERAGE, SL_BUFFER_PCT, MIN_RR
    t = os.getenv("TELEGRAM_TOKEN"); lid = 0
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{t}/getUpdates", params={"offset": lid, "timeout": 10}).json()
            for i in r.get("result", []):
                lid = i["update_id"] + 1; msg = i.get("message", {}); txt = msg.get("text", "").strip().lower(); def rep(m): send_telegram(m)
                if not txt: continue
                args = txt.split()
                cmd = args[0]

                # --- FITUR EDIT ENV VIA TELEGRAM ---
                if cmd == "/setapi" and len(args) > 1:
                    API_KEY = args[1]; update_env_file("BINANCE_API_KEY", API_KEY); rep("✅ API KEY Berhasil diperbarui!")
                elif cmd == "/setsecret" and len(args) > 1:
                    SECRET_KEY = args[1]; update_env_file("BINANCE_SECRET_KEY", SECRET_KEY); rep("✅ SECRET KEY Berhasil diperbarui!")
                elif cmd == "/margin" and len(args) > 1:
                    MARGIN_USDT = float(args[1]); update_env_file("MARGIN_USDT", MARGIN_USDT); rep(f"✅ Margin diubah ke: {MARGIN_USDT} USDT")
                elif cmd == "/leverage" and len(args) > 1:
                    LEVERAGE = int(args[1]); update_env_file("LEVERAGE", LEVERAGE); rep(f"✅ Leverage diubah ke: {LEVERAGE}x")
                elif cmd == "/buffer" and len(args) > 1:
                    SL_BUFFER_PCT = float(args[1]); update_env_file("SL_BUFFER_PCT", SL_BUFFER_PCT); rep(f"✅ SL Buffer diubah ke: {SL_BUFFER_PCT}%")
                elif cmd == "/minrr" and len(args) > 1:
                    MIN_RR = float(args[1]); update_env_file("MIN_RR", MIN_RR); rep(f"✅ Min RR diubah ke: {MIN_RR}")
                
                # --- COMMAND STANDARD ---
                elif cmd == "/status":
                    lines = ["📊 *STATUS BOT V8.3*\n", "⚙️ *CONFIG:*", f"   Margin: `{MARGIN_USDT}` | Lev: `{LEVERAGE}x`", f"   Buffer: `{SL_BUFFER_PCT}%` | MinRR: `{MIN_RR}`\n"]
                    if positions:
                        for s, p in positions.items():
                            cur = live_prices.get(s, 0.0); pnl = (cur-p['ep'])*p['qty'] if p['side']=="BUY" else (p['ep']-cur)*p['qty']
                            lines.append(f"{'🟩' if pnl>0 else '🟥'} *{s}* ({p['side']})\n   PnL: `{pnl:+.2f} USDT` (`{((pnl/((p['qty']*p['ep'])/LEVERAGE))*100):+.2f}%`)\n")
                    else: lines.append("💤 Tidak ada posisi aktif.\n")
                    rep("\n".join(lines).strip())
                elif cmd == "/pnl":
                    rep(f"📊 *PnL Bulan Ini*\n💰 Total: `{total_pnl:.4f} USDT`\n✅ Wins: {total_wins} | ❌ Loss: {total_losses}")
                elif cmd.startswith("/close"):
                    target = args[1].upper() if len(args)>1 else "ALL"
                    for s in ([s for s in positions] if target=="ALL" else ([target] if target in positions else [])):
                        p = positions[s]; post_api({"symbol": s, "side": "SELL" if p["side"]=="BUY" else "BUY", "type": "MARKET", "quantity": round_v(p["qty"], precisions[s]["step"]), "reduceOnly": "true"}, "/fapi/v1/order")
                        rep(f"🛑 {s} ditutup paksa.")
                elif cmd == "/help":
                    rep("*📖 REMOTE CONTROL:* \n/setapi <val>\n/setsecret <val>\n/margin <val>\n/leverage <val>\n/buffer <val>\n/minrr <val>\n\n*📊 MONITOR:* \n/status, /pnl, /close <koin/all>")
        except: time.sleep(5)

# ==============================================================================
# USER DATA STREAM (THE TRUTH TELLER LOGIC)
# ==============================================================================
def on_user_msg(ws, m):
    global total_pnl, total_wins, total_losses
    try:
        d = json.loads(m)
        if d.get("e") == "ORDER_TRADE_UPDATE":
            o, s = d["o"], d["o"]["s"]
            if o["X"] == "FILLED" and float(o.get("rp", 0)) != 0:
                rp = float(o["rp"]); total_pnl += rp
                if rp > 0: total_wins += 1; emo = "✅"
                else: total_losses += 1; emo = "❌"
                
                ot, ort = o.get("o"), o.get("ot", o.get("o"))
                if ot in ["STOP_MARKET", "STOP"] or ort in ["STOP_MARKET", "STOP"]: reason = "Hit Stop Loss 🛑"
                elif ot in ["TAKE_PROFIT_MARKET", "TAKE_PROFIT"]: reason = "Hit Take Profit 🎯"
                else: reason = "Profit Taken 🎯" if rp > 0 else "Wick Sweep 🛑"
                
                send_telegram(f"{emo} *{s}* CLOSED!\nReason: {reason}\nPnL: `{rp:+.4f} USDT`")
        if d.get("e") == "ACCOUNT_UPDATE":
            for p in d["a"]["P"]:
                pa, s = float(p["pa"]), p["s"]
                if pa == 0:
                    with state_lock: positions.pop(s, None); active_signals.pop(s, None)
                    post_api({"symbol": s}, "/fapi/v1/allOpenOrders", method="DELETE"); post_api({"symbol": s}, "/fapi/v1/algoOpenOrders", method="DELETE")
                    for e in engines: 
                        if e.symbol == s: e.reset()
                else: positions[s] = {"side": "BUY" if pa > 0 else "SELL", "qty": abs(pa), "ep": float(p["ep"])}
    except: pass

# --- STARTUP ---
def on_market_msg(ws, m):
    try:
        d = json.loads(m); if "data" not in d: return
        k, s, tf = d["data"]["k"], d["data"]["s"], d["data"]["k"]["i"]
        if tf == "1m": live_prices[s] = float(k["c"])
        if k["x"]:
            klines_data[s][tf].append({"t": k["t"], "o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"])})
            klines_data[s][tf] = klines_data[s][tf][-80:]
        for e in engines: 
            if e.symbol == s: e.tick()
    except: pass

def start_user_ws():
    while True:
        try:
            lk = requests.post(f"{BASE_URL}/fapi/v1/listenKey", headers={"X-MBX-APIKEY": API_KEY}).json().get("listenKey")
            ws = websocket.WebSocketApp(f"{WS_BASE}/ws/{lk}", on_message=on_user_msg)
            ws.run_forever(ping_interval=60, ping_timeout=30)
        except: time.sleep(5)

def start():
    load_precisions(); threading.Thread(target=telegram_cmd, daemon=True).start()
    for s in symbols:
        for tf in ["1h", "4h", "15m", "1m"]:
            r = requests.get(f"{BASE_URL}/fapi/v1/klines", params={"symbol": s, "interval": tf, "limit": 80}).json()
            klines_data[s][tf] = [{"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])} for k in r[:-1]]
    
    streams = "/".join([f"{s.lower()}@kline_{tf}" for s in symbols for tf in ["1m", "5m", "15m", "1h", "4h"]])
    threading.Thread(target=lambda: websocket.WebSocketApp(f"{WS_BASE}/stream?streams={streams}", on_message=on_market_msg).run_forever(), daemon=True).start()
    threading.Thread(target=start_user_ws, daemon=True).start()

engines = [Engine(s, m) for s in symbols for m in ["1H_BIAS", "4H_BIAS"]]
if __name__ == "__main__":
    start()
    print("🔥 BOT v8.3 (THE REMOTE CONTROLLER) ACTIVE...")
    while True: time.sleep(1)
