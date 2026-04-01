import os, time, json, hmac, hashlib, requests, websocket, threading, math, csv, urllib.parse, logging
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SMC_Bot")

# --- CONFIG ---
API_KEY = os.getenv("BINANCE_API_KEY")
SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")
MODE = os.getenv("MODE", "TESTNET")
BASE_URL = "https://testnet.binancefuture.com" if MODE == "TESTNET" else "https://fapi.binance.com"
WS_BASE = "wss://stream.binancefuture.com" if MODE == "TESTNET" else "wss://fstream.binance.com"
MARGIN_USDT = float(os.getenv("MARGIN_USDT", 1.0))
LEVERAGE = int(os.getenv("LEVERAGE", 10))
MIN_RR = float(os.getenv("MIN_RR", 1.5))
SL_BUFFER_PCT = float(os.getenv("SL_BUFFER_PCT", 0.05))  # 0.05% buffer pada SL

symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "SUIUSDT", "AVAXUSDT", "BNBUSDT", "TRXUSDT"]

# --- GLOBAL STATE ---
klines_data = {s: {"1m": [], "5m": [], "15m": [], "1h": [], "4h": []} for s in symbols}
live_prices = {s: 0.0 for s in symbols}
config = {"ENABLE_1H": True, "ENABLE_4H": True}
positions = {}
active_signals = {}
CSV_FILE = "riwayat_trading.csv"

total_pnl, total_wins, total_losses = 0.0, 0, 0
current_month_str = datetime.now().strftime("%Y-%m")

# Thread lock untuk state yang dishare
state_lock = threading.Lock()

# --- UTILS ---
def sync_time():
    try:
        return requests.get(BASE_URL + "/fapi/v1/time", timeout=5).json()["serverTime"] - int(time.time() * 1000)
    except Exception as e:
        logger.warning(f"Time sync gagal: {e}")
        return 0

time_offset = sync_time()

def ts():
    return int(time.time() * 1000 + time_offset)

def send_telegram(msg):
    def run():
        t, c = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
        if t and c:
            try:
                # Amankan teks dari error Markdown (ubah _ jadi spasi)
                safe_msg = msg.replace("_", " ")
                res = requests.post(f"https://api.telegram.org/bot{t}/sendMessage",
                              json={"chat_id": c, "text": safe_msg, "parse_mode": "Markdown"}, timeout=10)
                if not res.json().get("ok"):
                    logger.error(f"Telegram Error: {res.text}")
            except Exception as e:
                logger.warning(f"Telegram send gagal: {e}")
    threading.Thread(target=run, daemon=True).start()

precisions = {}

def load_precisions():
    try:
        info = requests.get(BASE_URL + "/fapi/v1/exchangeInfo", timeout=10).json()
        for s in info["symbols"]:
            if s["symbol"] in symbols:
                filters = {f["filterType"]: f for f in s["filters"]}
                t = filters["PRICE_FILTER"]["tickSize"]
                st = filters["LOT_SIZE"]["stepSize"]
                min_qty = float(filters["LOT_SIZE"]["minQty"])
                min_notional = float(filters.get("MIN_NOTIONAL", {}).get("notional", 5))
                precisions[s["symbol"]] = {
                    "tick": max(0, int(round(-math.log10(float(t))))),
                    "step": max(0, int(round(-math.log10(float(st))))),
                    "min_qty": min_qty,
                    "min_notional": min_notional,
                }
    except Exception as e:
        logger.error(f"Load precisions gagal: {e}")

def round_v(v, p):
    return f"{round(v, p):.{p}f}" if p > 0 else str(int(round(v)))

def log_trade(symbol, side, pnl, mode):
    try:
        file_exists = os.path.isfile(CSV_FILE)
        with open(CSV_FILE, mode='a', newline='') as f:
            w = csv.writer(f)
            if not file_exists:
                w.writerow(['Waktu', 'Simbol', 'Posisi', 'PnL (USDT)', 'Mode', 'Status'])
            w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), symbol, side,
                        round(pnl, 4), mode, "WIN" if pnl > 0 else "LOSS"])
    except Exception as e:
        logger.error(f"Log trade error: {e}")

def post_api(params, endpoint, method="POST"):
    params["timestamp"] = ts()
    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(SECRET_KEY.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    payload = f"{query_string}&signature={signature}"
    try:
        if method == "POST":
            resp = requests.post(f"{BASE_URL}{endpoint}", headers=headers, data=payload, timeout=10)
        elif method == "DELETE":
            resp = requests.delete(f"{BASE_URL}{endpoint}?{payload}", headers=headers, timeout=10)
        elif method == "GET":
            resp = requests.get(f"{BASE_URL}{endpoint}?{payload}", headers=headers, timeout=10)
        else:
            return {"error": f"Unknown method {method}"}
        return resp.json()
    except Exception as e:
        logger.error(f"API call error {method} {endpoint}: {e}")
        return {"error": str(e)}


# ==============================================================================
# SMC LOGIC — FVG, ORDER BLOCK, TARGET
# ==============================================================================

def get_unmitigated_poi(candles, depth=40, min_size_pct=0.1):
    if len(candles) < depth + 3: return []
    pois = []
    start_idx = max(0, len(candles) - depth)

    for i in range(start_idx, len(candles) - 2):
        c0, c1, c2 = candles[i], candles[i+1], candles[i+2]

        if c0["h"] < c2["l"]:
            if (c2["l"] - c0["h"]) / c0["h"] * 100 >= min_size_pct:
                if not any(candles[j]["l"] <= c2["l"] for j in range(i+3, len(candles))):
                    pois.append(("BUY", c0["h"], c2["l"], "FVG"))
        elif c0["l"] > c2["h"]:
            if (c0["l"] - c2["h"]) / c2["h"] * 100 >= min_size_pct:
                if not any(candles[j]["h"] >= c2["h"] for j in range(i+3, len(candles))):
                    pois.append(("SELL", c2["h"], c0["l"], "FVG"))

        if c0["c"] < c0["o"] and c1["c"] > c1["o"] and (c1["c"] - c1["o"]) > abs(c0["c"] - c0["o"]) * 1.5:
            if not any(candles[j]["l"] <= c0["h"] for j in range(i+2, len(candles))):
                pois.append(("BUY", c0["l"], c0["h"], "OB"))
        elif c0["c"] > c0["o"] and c1["c"] < c1["o"] and (c1["o"] - c1["c"]) > abs(c0["c"] - c0["o"]) * 1.5:
            if not any(candles[j]["h"] >= c0["l"] for j in range(i+2, len(candles))):
                pois.append(("SELL", c0["l"], c0["h"], "OB"))
    return pois

def is_price_in_zone(price, poi_tuple):
    direction, zone_low, zone_high, poi_type = poi_tuple
    return zone_low <= price <= zone_high

def get_target(candles, direction, depth=20):
    if len(candles) < 3: return None
    recent = candles[-depth:] if len(candles) >= depth else candles
    if direction == "BUY":
        swing_highs = [recent[i]["h"] for i in range(1, len(recent) - 1) if recent[i]["h"] > recent[i-1]["h"] and recent[i]["h"] > recent[i+1]["h"]]
        return max(swing_highs) if swing_highs else max(c["h"] for c in recent)
    else:
        swing_lows = [recent[i]["l"] for i in range(1, len(recent) - 1) if recent[i]["l"] < recent[i-1]["l"] and recent[i]["l"] < recent[i+1]["l"]]
        return min(swing_lows) if swing_lows else min(c["l"] for c in recent)


# ==============================================================================
# ENGINE CLASS (X-RAY RETROSPECTIVE SCAN)
# ==============================================================================

class Engine:
    def __init__(self, symbol, mode):
        self.symbol = symbol
        self.mode = mode
        self.reset()
        if mode == "1H_BIAS":
            self.tf_poi, self.tf_conf, self.tf_trig = "1h", "15m", "1m"
        else:
            self.tf_poi, self.tf_conf, self.tf_trig = "4h", "1h", "5m"
        self.last_signal_time = 0
        self.cooldown_seconds = 300 

    def reset(self):
        self.state = "IDLE"
        self.direction = None
        self.active_poi = None 
        self.fc1 = None
        self.fc2 = None
        self.entry = None
        self.sl = None
        self.tp = None
        self.pending_order_id = None
        self.pending_sl_algo_id = None
        self.setup_time = None
        self.last_processed_conf_t = 0 

    def check_and_trigger(self, log_prefix, c_trig, c_conf):
        """
        X-RAY SCAN: Mengambil 35 candle LTF terakhir (30 untuk di-scan + 5 konteks BOS)
        """
        ltf_scan_range = c_trig[-36:-1] 
        if len(ltf_scan_range) < 6: return False
        
        for i in range(len(ltf_scan_range) - 1, 4, -1):
            trigger_candle = ltf_scan_range[i]
            body = abs(trigger_candle["c"] - trigger_candle["o"])
            wick = trigger_candle["h"] - trigger_candle["l"]
            
            if wick > 0 and body / wick > 0.6:
                recent_5 = ltf_scan_range[i-5:i]
                recent_bodies_high = [max(x["o"], x["c"]) for x in recent_5]
                recent_bodies_low = [min(x["o"], x["c"]) for x in recent_5]
                
                if self.direction == "BUY" and trigger_candle["c"] > max(recent_bodies_high):
                    body_size = abs(trigger_candle["c"] - trigger_candle["o"])
                    self.entry = trigger_candle["c"] - (body_size * 0.25) 
                    
                    recent_low = min(c["l"] for c in ltf_scan_range[i-5:i+1])
                    self.sl = recent_low - (recent_low * SL_BUFFER_PCT / 100)
                    
                    self.tp = get_target(c_conf, self.direction)
                    if not self.tp: return False
                    
                    risk = abs(self.entry - self.sl)
                    reward = abs(self.tp - self.entry)
                    if risk == 0 or reward / risk < MIN_RR: return False
                    
                    logger.info(f"{self.symbol} [{self.mode}] {log_prefix} -> X-RAY CISD LTF Found! | Entry: {self.entry:.4f}")
                    self.state = "WAIT_ENTRY"
                    self.last_signal_time = time.time()
                    self.place_limit_and_sl()
                    return True
                    
                elif self.direction == "SELL" and trigger_candle["c"] < min(recent_bodies_low):
                    body_size = abs(trigger_candle["c"] - trigger_candle["o"])
                    self.entry = trigger_candle["c"] + (body_size * 0.25)
                    
                    recent_high = max(c["h"] for c in ltf_scan_range[i-5:i+1])
                    self.sl = recent_high + (recent_high * SL_BUFFER_PCT / 100)
                    
                    self.tp = get_target(c_conf, self.direction)
                    if not self.tp: return False
                    
                    risk = abs(self.entry - self.sl)
                    reward = abs(self.tp - self.entry)
                    if risk == 0 or reward / risk < MIN_RR: return False
                    
                    logger.info(f"{self.symbol} [{self.mode}] {log_prefix} -> X-RAY CISD LTF Found! | Entry: {self.entry:.4f}")
                    self.state = "WAIT_ENTRY"
                    self.last_signal_time = time.time()
                    self.place_limit_and_sl()
                    return True
        return False

    def tick(self):
        if (self.mode == "1H_BIAS" and not config["ENABLE_1H"]) or (self.mode == "4H_BIAS" and not config["ENABLE_4H"]):
            if self.state != "IDLE":
                self.cancel_pending_orders()
                self.reset()
            return

        if self.symbol in positions: return

        c_poi = klines_data[self.symbol][self.tf_poi]
        c_conf = klines_data[self.symbol][self.tf_conf]
        c_trig = klines_data[self.symbol][self.tf_trig]
        price = live_prices[self.symbol]

        if len(c_poi) < 10 or price == 0: return
        if time.time() - self.last_signal_time < self.cooldown_seconds and self.state == "IDLE": return

        if self.setup_time and time.time() - self.setup_time > 1800:
            if self.state in ["WAIT_C1", "WAIT_C2", "WAIT_C3"]:
                logger.info(f"{self.symbol} [{self.mode}] Setup timeout, reset")
                self.reset()
                return

        # === STATE: IDLE ===
        if self.state == "IDLE":
            pois = get_unmitigated_poi(c_poi)
            if not pois: return

            for poi in reversed(pois):
                if is_price_in_zone(price, poi):
                    self.direction = poi[0]
                    self.active_poi = poi
                    self.state = "WAIT_C1"
                    self.setup_time = time.time()
                    return

        # === X-RAY STATE MACHINE (C1 -> C2 -> C3) ===
        elif self.state in ["WAIT_C1", "WAIT_C2", "WAIT_C3"]:
            if not self.active_poi: self.reset(); return
            if len(c_conf) < 3: return
            
            _, zone_low, zone_high, _ = self.active_poi
            margin = (zone_high - zone_low) * 0.5
            
            if self.direction == "BUY" and price < zone_low - margin: self.reset(); return
            elif self.direction == "SELL" and price > zone_high + margin: self.reset(); return

            prev_candle = c_conf[-2] 
            
            if prev_candle["t"] == self.last_processed_conf_t: return
            self.last_processed_conf_t = prev_candle["t"]

            if self.state == "WAIT_C1":
                c1_valid = False
                if self.direction == "BUY" and prev_candle["l"] <= zone_high and prev_candle["c"] > zone_high: c1_valid = True
                elif self.direction == "SELL" and prev_candle["h"] >= zone_low and prev_candle["c"] < zone_low: c1_valid = True

                if c1_valid:
                    if self.check_and_trigger("C1 Reject", c_trig, c_conf): return
                    
                self.fc1 = prev_candle
                self.state = "WAIT_C2"
                return

            elif self.state == "WAIT_C2":
                c2_valid = False
                if self.direction == "BUY" and prev_candle["l"] < self.fc1["l"] and prev_candle["c"] > self.fc1["l"]: c2_valid = True
                elif self.direction == "SELL" and prev_candle["h"] > self.fc1["h"] and prev_candle["c"] < self.fc1["h"]: c2_valid = True

                if c2_valid:
                    if self.check_and_trigger("C2 Sweep", c_trig, c_conf): return
                
                self.fc2 = prev_candle
                self.state = "WAIT_C3"
                return

            elif self.state == "WAIT_C3":
                c3_valid = False
                if self.direction == "BUY":
                    fc2_body_high = max(self.fc2["o"], self.fc2["c"])
                    if prev_candle["c"] > fc2_body_high: c3_valid = True
                elif self.direction == "SELL":
                    fc2_body_low = min(self.fc2["o"], self.fc2["c"])
                    if prev_candle["c"] < fc2_body_low: c3_valid = True

                if c3_valid:
                    if self.check_and_trigger("C3 Engulfing", c_trig, c_conf): return
                
                self.reset()
                return

        # === STATE: WAIT_ENTRY ===
        elif self.state == "WAIT_ENTRY":
            if self.entry is None or self.tp is None: self.reset(); return

            if (self.direction == "BUY" and (price >= self.tp or price <= self.sl)) or \
               (self.direction == "SELL" and (price <= self.tp or price >= self.sl)):
                send_telegram(f"❌ {self.symbol} [{self.mode}] Batal: Harga sentuh TP/SL sebelum limit terisi.")
                self.cancel_pending_orders()
                self.reset()

    def place_limit_and_sl(self):
        def run():
            try:
                pr = precisions.get(self.symbol)
                if not pr: self.reset(); return

                qty_raw = (MARGIN_USDT * LEVERAGE) / self.entry
                qty = float(round_v(qty_raw, pr["step"]))

                if qty < pr["min_qty"] or (qty * self.entry) < pr["min_notional"]: self.reset(); return

                fe = round_v(self.entry, pr["tick"])
                fsl = round_v(self.sl, pr["tick"])
                ftp = round_v(self.tp, pr["tick"])
                fq = round_v(qty_raw, pr["step"])
                opp_side = "SELL" if self.direction == "BUY" else "BUY"

                post_api({"symbol": self.symbol, "leverage": LEVERAGE}, "/fapi/v1/leverage")

                res_limit = post_api({"symbol": self.symbol, "side": self.direction, "type": "LIMIT", "quantity": fq, "price": fe, "timeInForce": "GTC"}, "/fapi/v1/order")
                
                if "orderId" in res_limit:
                    self.pending_order_id = res_limit["orderId"]
                    
                    params_sl = {
                        "algoType": "CONDITIONAL",
                        "symbol": self.symbol, 
                        "side": opp_side, 
                        "type": "STOP_MARKET", 
                        "triggerPrice": fsl, 
                        "quantity": fq, 
                        "reduceOnly": "true"
                    }
                    res_sl = post_api(params_sl, "/fapi/v1/algoOrder") 
                    
                    if "algoId" in res_sl:
                        self.pending_sl_algo_id = res_sl["algoId"]
                    elif "orderId" in res_sl: 
                        self.pending_sl_algo_id = res_sl["orderId"]

                    with state_lock:
                        active_signals[self.symbol] = {"mode": self.mode, "tp": self.tp, "dir": self.direction, "qty": fq}

                    send_telegram(f"⏳ *{self.symbol}* LIMIT + SL ({self.mode})\n📍 Dir: {self.direction}\n💰 Entry: `{fe}`\n🛑 SL: `{fsl}`\n🎯 TP: `{ftp}`\n📦 Qty: {fq}")
                else:
                    self.reset()
            except Exception as e:
                logger.error(f"place_order error: {e}")
                self.reset()
        threading.Thread(target=run, daemon=True).start()

    def cancel_pending_orders(self):
        def run():
            try:
                if self.pending_order_id:
                    post_api({"symbol": self.symbol, "orderId": self.pending_order_id}, "/fapi/v1/order", method="DELETE")
                if self.pending_sl_algo_id:
                    post_api({"symbol": self.symbol, "algoId": self.pending_sl_algo_id}, "/fapi/v1/algoOrder", method="DELETE")
            except Exception as e: pass
        threading.Thread(target=run, daemon=True).start()
        self.pending_order_id = None
        self.pending_sl_algo_id = None


# ==============================================================================
# WS HANDLERS & STARTUP
# ==============================================================================

def on_market_msg(ws, msg):
    try:
        d = json.loads(msg)
        if "data" not in d: return
        k, s, tf = d["data"]["k"], d["data"]["s"], d["data"]["k"]["i"]
        if tf == "1m": live_prices[s] = float(k["c"])
        if k["x"]:
            klines_data[s][tf].append({"t": k["t"], "o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"]), "x": True})
            klines_data[s][tf] = klines_data[s][tf][-80:]
        for e in engines:
            if e.symbol == s: e.tick()
    except Exception as e: pass

def on_user_msg(ws, m):
    global total_pnl, total_wins, total_losses, current_month_str
    try:
        d = json.loads(m)
        if d.get("e") == "ORDER_TRADE_UPDATE":
            o, s, stat = d["o"], d["o"]["s"], d["o"]["X"]
            
            if stat == "FILLED" and o.get("ot") == "LIMIT" and s in active_signals:
                sig, pr = active_signals[s], precisions.get(s, {})
                opp = "SELL" if sig["dir"] == "BUY" else "BUY"
                
                params_tp = {
                    "algoType": "CONDITIONAL",
                    "symbol": s, 
                    "side": opp, 
                    "type": "TAKE_PROFIT_MARKET", 
                    "triggerPrice": round_v(sig["tp"], pr["tick"]), 
                    "closePosition": "true"
                }
                post_api(params_tp, "/fapi/v1/algoOrder")
                send_telegram(f"🚀 *{s}* LIMIT FILLED!\nTP dipasang otomatis di `{round_v(sig['tp'], pr['tick'])}`")

            if stat == "FILLED" and float(o.get("rp", 0)) != 0:
                rp = float(o["rp"])
                now_ym = datetime.now().strftime("%Y-%m")
                if now_ym != current_month_str:
                    total_pnl, total_wins, total_losses, current_month_str = 0.0, 0, 0, now_ym
                total_pnl += rp
                total_wins += 1 if rp > 0 else 0
                total_losses += 1 if rp < 0 else 0
                mode = active_signals.get(s, {}).get("mode", "UNKNOWN")
                log_trade(s, o["S"], rp, mode)
                send_telegram(f"{'✅' if rp > 0 else '❌'} *{s}* Closed | PnL: `{round(rp, 4)} USDT` ({mode})")

        if d.get("e") == "ACCOUNT_UPDATE":
            for p in d["a"]["P"]:
                sym, pa = p["s"], float(p["pa"])
                with state_lock:
                    if pa == 0:
                        positions.pop(sym, None)
                        active_signals.pop(sym, None)
                        for e in engines:
                            if e.symbol == sym: e.reset()
                    else:
                        positions[sym] = {
                            "side": "BUY" if pa > 0 else "SELL", 
                            "qty": abs(pa),
                            "ep": float(p.get("ep", 0)), 
                            "up": float(p.get("up", 0))  
                        }
    except Exception as e: pass

def telegram_cmd():
    global total_pnl, total_wins, total_losses, current_month_str
    t = os.getenv("TELEGRAM_TOKEN")
    if not t: return
    lid = 0
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{t}/getUpdates", params={"offset": lid, "timeout": 10}, timeout=15).json()
            if not r.get("ok"): time.sleep(5); continue
            for i in r["result"]:
                lid = i["update_id"] + 1
                
                text = i.get("message", {}).get("text", "")
                if not text: continue
                
                msg = str(text).strip().lower()
                chat_id = i.get("message", {}).get("chat", {}).get("id")
                if not chat_id: continue

                def reply(reply_text):
                    safe_text = reply_text.replace("_", " ") 
                    res = requests.post(
                        f"https://api.telegram.org/bot{t}/sendMessage", 
                        json={"chat_id": chat_id, "text": safe_text, "parse_mode": "Markdown"}, 
                        timeout=10
                    )
                    if not res.json().get("ok"): 
                        logger.error(f"TG Error: {res.text}")

                if msg.startswith("/pnl"):
                    wr = (total_wins / (total_wins + total_losses) * 100) if (total_wins + total_losses) > 0 else 0
                    mode_str = "DOUBLE (1H & 4H)" if config["ENABLE_1H"] and config["ENABLE_4H"] else "1H BIAS" if config["ENABLE_1H"] else "4H BIAS"
                    reply(f"📊 *PnL Bulan Ini* ({current_month_str})\n💰 Total: `{round(total_pnl, 4)} USDT`\n📈 Winrate: {round(wr, 1)}%\n✅ Wins: {total_wins} | ❌ Loss: {total_losses}\n📌 Active: {len(positions)}\n⚙️ Mode: {mode_str}")
                
                elif msg in ["/mode 1h", "/mode 4h", "/mode double"]:
                    config["ENABLE_1H"] = msg in ["/mode 1h", "/mode double"]
                    config["ENABLE_4H"] = msg in ["/mode 4h", "/mode double"]
                    for e in engines: e.cancel_pending_orders(); e.reset()
                    reply(f"✅ Mode diubah ke: {msg.upper()}")
                
                elif msg.startswith("/status"):
                    lines = ["📊 *Bot Status Saat Ini*\n"]
                    
                    pending = [e for e in engines if e.state == "WAIT_ENTRY"]
                    if pending:
                        lines.append("⏳ *Limit Belum Kejemput:*")
                        for e in pending:
                            lines.append(f"• {e.symbol} ({e.direction}) | Entry: `{e.entry:.4f}`")
                        lines.append("")
                        
                    if positions:
                        lines.append("📈 *Posisi Floating Aktif:*")
                        for sym, pos in positions.items():
                            ep = pos.get('ep', 0)
                            qty = pos.get('qty', 0)
                            
                            # Kalkulasi PnL MANDIRI (Super Akurat & Real-time)
                            current_price = live_prices.get(sym, ep)
                            if pos['side'] == "BUY":
                                pnl = (current_price - ep) * qty
                            else:
                                pnl = (ep - current_price) * qty
                                
                            emo = "🟩" if pnl > 0 else "🟥"
                            lines.append(f"{emo} {sym} ({pos['side']}) | Entry: `{ep}` | PnL: `{pnl:.2f} USDT`")
                        lines.append("")
                    else:
                        lines.append("📈 *Posisi Floating:* Tidak ada\n")

                    analyzing = [e for e in engines if e.state in ["WAIT_C1", "WAIT_C2", "WAIT_C3"]]
                    if analyzing:
                        lines.append("🔍 *Sedang Analisa (Setup Ditemukan):*")
                        for e in analyzing:
                            lines.append(f"• {e.symbol} [{e.mode}] ➔ {e.state}")

                    if not pending and not positions and not analyzing:
                        lines = ["📊 *Bot Status Saat Ini*\n\n💤 Semua mata uang sedang IDLE (Mencari zona baru)."]
                        
                    reply("\n".join(lines))
                
                elif msg.startswith("/help"):
                    reply("*📖 Commands:*\n/pnl — Lihat PnL bulan ini\n/mode 1h — Hanya 1H bias\n/mode 4h — Hanya 4H bias\n/mode double — Kedua mode aktif\n/status — Status engine & posisi\n/help — Menu ini")
        except Exception as ex: 
            logger.error(f"Telegram polling error: {ex}")
            time.sleep(5)

def keep_alive_listenkey():
    while True:
        time.sleep(30 * 60)
        try: requests.put(BASE_URL + "/fapi/v1/listenKey", headers={"X-MBX-APIKEY": API_KEY}, timeout=10)
        except: pass

def load_monthly_pnl():
    global total_pnl, total_wins, total_losses, current_month_str
    current_month_str = datetime.now().strftime("%Y-%m")
    if not os.path.isfile(CSV_FILE): return
    try:
        with open(CSV_FILE, mode='r') as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) >= 6 and row[0].startswith(current_month_str):
                    pnl = float(row[3])
                    total_pnl += pnl
                    if pnl > 0: total_wins += 1
                    else: total_losses += 1
    except: pass

def start_ws_with_reconnect(url, on_message, name="WS"):
    def run():
        while True:
            try:
                ws = websocket.WebSocketApp(url, on_message=on_message)
                ws.run_forever(ping_interval=60, ping_timeout=30)
            except: pass
            time.sleep(5)
    threading.Thread(target=run, daemon=True).start()

def start():
    load_precisions()
    load_monthly_pnl()
    
    # --- [OBAT AMNESIA] Tarik Posisi Floating dari Binance saat bot baru nyala ---
    try:
        pos_data = post_api({}, "/fapi/v2/positionRisk", method="GET")
        if isinstance(pos_data, list):
            for p in pos_data:
                amt = float(p["positionAmt"])
                sym = p["symbol"]
                if amt != 0 and sym in symbols:
                    positions[sym] = {
                        "side": "BUY" if amt > 0 else "SELL",
                        "qty": abs(amt),
                        "ep": float(p["entryPrice"]),
                        "up": float(p["unRealizedProfit"])
                    }
    except Exception as e:
        logger.error(f"Gagal memuat riwayat posisi: {e}")
    # ----------------------------------------------------------------------------

    for s in symbols:
        for tf in ["4h", "1h", "15m", "5m", "1m"]:
            try:
                res = requests.get(f"{BASE_URL}/fapi/v1/klines", params={"symbol": s, "interval": tf, "limit": 80}, timeout=10).json()
                klines_data[s][tf] = [{"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]), "x": (i < len(res) - 1)} for i, k in enumerate(res)]
            except: pass

    threading.Thread(target=telegram_cmd, daemon=True).start()
    streams = "/".join([f"{s.lower()}@kline_{tf}" for s in symbols for tf in ["1m", "5m", "15m", "1h", "4h"]])
    start_ws_with_reconnect(f"{WS_BASE}/stream?streams={streams}", on_market_msg, "MarketWS")

    try:
        lk = requests.post(BASE_URL + "/fapi/v1/listenKey", headers={"X-MBX-APIKEY": API_KEY}, timeout=10).json().get("listenKey")
        if lk:
            threading.Thread(target=keep_alive_listenkey, daemon=True).start()
            start_ws_with_reconnect(f"{WS_BASE}/ws/{lk}", on_user_msg, "UserWS")
    except: pass

engines = [Engine(s, m) for s in symbols for m in ["1H_BIAS", "4H_BIAS"]]

if __name__ == "__main__":
    start()
    print("🔥 BOT v5.7 (ANTI-AMNESIA + LIVE PNL FIXED) ACTIVE...")
    while True: time.sleep(1)
