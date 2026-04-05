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
    except Exception as e:
        return 0

time_offset = sync_time()

def ts():
    return int(time.time() * 1000 + time_offset)

def send_telegram(msg):
    def run():
        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if token and chat_id:
            try:
                safe_msg = msg.replace("_", " ")
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                payload = {"chat_id": chat_id, "text": safe_msg, "parse_mode": "Markdown"}
                res = requests.post(url, json=payload, timeout=10)
                if res.status_code != 200:
                    print(f"\n[TELEGRAM ERROR] Telegram menolak pesan! Alasan: {res.text}\n")
            except Exception as e:
                print(f"\n[TELEGRAM ERROR KONEKSI] {e}\n")
    threading.Thread(target=run, daemon=True).start()

precisions = {}

def load_precisions():
    try:
        url = BASE_URL + "/fapi/v1/exchangeInfo"
        info = requests.get(url, timeout=10).json()
        for s in info["symbols"]:
            if s["symbol"] in symbols:
                filters = {flt["filterType"]: flt for flt in s["filters"]}
                tick_size = float(filters["PRICE_FILTER"]["tickSize"])
                step_size = float(filters["LOT_SIZE"]["stepSize"])
                min_qty = float(filters["LOT_SIZE"]["minQty"])
                min_notional = float(filters.get("MIN_NOTIONAL", {}).get("notional", 5))
                
                precisions[s["symbol"]] = {
                    "tick": max(0, int(round(-math.log10(tick_size)))),
                    "step": max(0, int(round(-math.log10(step_size)))),
                    "min_qty": min_qty,
                    "min_notional": min_notional
                }
    except Exception as e:
        logger.error(f"Load precisions gagal: {e}")

def round_v(v, p):
    if p > 0: return f"{round(v, p):.{p}f}"
    else: return str(int(round(v)))

def log_trade(symbol, side, pnl, mode):
    try:
        file_exists = os.path.isfile(CSV_FILE)
        with open(CSV_FILE, mode='a', newline='') as f:
            w = csv.writer(f)
            if not file_exists:
                w.writerow(['Waktu', 'Simbol', 'Posisi', 'PnL (USDT)', 'Mode', 'Status'])
            
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            status = "WIN" if pnl > 0 else "LOSS"
            w.writerow([now_str, symbol, side, round(pnl, 4), mode, status])
    except Exception as e:
        pass

def post_api(params, endpoint, method="POST"):
    params["timestamp"] = ts()
    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(SECRET_KEY.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    payload = f"{query_string}&signature={signature}"
    url = f"{BASE_URL}{endpoint}"
    
    try:
        if method == "POST": response = requests.post(url, headers=headers, data=payload, timeout=10)
        elif method == "DELETE": response = requests.delete(f"{url}?{payload}", headers=headers, timeout=10)
        elif method == "GET": response = requests.get(f"{url}?{payload}", headers=headers, timeout=10)
        else: return {"error": "Unknown method"}
        return response.json()
    except Exception as e:
        return {"error": str(e)}

# ==============================================================================
# LOGIKA SMC — STRICT VIRGIN FVG
# ==============================================================================
def get_unmitigated_poi(candles, depth=20, min_size_pct=0.1): 
    if len(candles) < depth + 4: return []
    pois = []
    start_idx = max(0, len(candles) - depth)

    for i in range(start_idx, len(candles) - 3):
        c0 = candles[i]
        c1 = candles[i+1] 
        c2 = candles[i+2]

        if c0["h"] < c2["l"] and c1["c"] > c1["o"]:
            if (c2["l"] - c0["h"]) / c0["h"] * 100 >= min_size_pct:
                fvg_b, fvg_t = c0["h"], c2["l"]
                ob_b, ob_t = c0["l"], c0["h"] 
                
                is_valid = True
                for j in range(i+3, len(candles) - 1):
                    if candles[j]["l"] <= fvg_t: 
                        is_valid = False; break 
                        
                if is_valid: 
                    pois.append({"dir": "BUY", "fvg_b": fvg_b, "fvg_t": fvg_t, "ob_b": ob_b, "ob_t": ob_t, "t": c0["t"]})

        elif c0["l"] > c2["h"] and c1["c"] < c1["o"]:
            if (c0["l"] - c2["h"]) / c2["h"] * 100 >= min_size_pct:
                fvg_t, fvg_b = c0["l"], c2["h"]
                ob_b, ob_t = c0["l"], c0["h"] 
                
                is_valid = True
                for j in range(i+3, len(candles) - 1):
                    if candles[j]["h"] >= fvg_b: 
                        is_valid = False; break 
                        
                if is_valid: 
                    pois.append({"dir": "SELL", "fvg_b": fvg_b, "fvg_t": fvg_t, "ob_b": ob_b, "ob_t": ob_t, "t": c0["t"]})

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
# ENGINE (V9.0 - THE TRUE SMC FRACTAL EXECUTION)
# ==============================================================================
class Engine:
    def __init__(self, symbol, mode):
        self.symbol = symbol
        self.mode = mode
        self.ignored_pois = []
        self.reset()
        if mode == "1H_BIAS": self.tf_poi, self.tf_conf, self.tf_trig = "1h", "15m", "1m"
        else: self.tf_poi, self.tf_conf, self.tf_trig = "4h", "1h", "5m"
        self.last_signal_time = 0
        self.cooldown = 300

    def reset(self):
        self.state = "IDLE"
        self.direction = None
        self.active_poi = None
        self.active_zone = None
        self.fc1 = None
        self.fc2 = None
        self.entry = None
        self.sl = None
        self.tp = None
        self.pending_order_id = None
        self.pending_sl_algo_id = None
        self.setup_time = None
        self.last_processed_conf_t = 0

    def check_and_trigger(self, log_prefix, c_trig, c_conf, scan_depth):
        # [V9.0: FRACTAL BOS & LTF FVG ENTRY]
        ltf_scan = c_trig[-80:] # Ambil data utuh agar swing terlihat jelas
        if len(ltf_scan) < 10: return False
        poi = self.active_poi

        if self.direction == "BUY":
            # 1. Cari titik terendah absolut (Sweep/Inducement)
            min_c = min(ltf_scan, key=lambda x: x["l"])
            min_idx = ltf_scan.index(min_c)

            # Pastikan Sweep berada di dalam zona POI kita
            if min_c["l"] > poi["fvg_t"] or min_c["l"] < poi["ob_b"]: return False

            # 2. Cari True Swing High terakhir SEBELUM titik terendah
            swing_highs = []
            for i in range(1, min_idx):
                if ltf_scan[i]["h"] > ltf_scan[i-1]["h"] and ltf_scan[i]["h"] > ltf_scan[i+1]["h"]:
                    swing_highs.append(ltf_scan[i])
            if not swing_highs: return False
            last_swing_high = swing_highs[-1]

            # 3. Cari True BOS/ChoCh (Body Close menembus Swing High)
            choch_idx = -1
            for i in range(min_idx + 1, len(ltf_scan)):
                if ltf_scan[i]["c"] > last_swing_high["h"]:
                    choch_idx = i
                    break
            if choch_idx == -1: return False # Belum ada BOS
            
            # Abaikan jika BOS terlalu basi (sudah terjadi lebih dari 5 candle lalu)
            if choch_idx < len(ltf_scan) - 6: return False

            # 4. Cari LTF FVG yang tercipta akibat dorongan BOS
            ltf_fvg_top = None
            for i in range(min_idx, choch_idx - 1):
                c0, c2 = ltf_scan[i], ltf_scan[i+2]
                if c0["h"] < c2["l"]:
                    ltf_fvg_top = c2["l"] # Jaring di atap LTF FVG
                    break

            # Jika tidak ada FVG, cari candle Order Block kecil (Candle merah terakhir)
            if not ltf_fvg_top:
                for i in range(choch_idx - 1, min_idx - 1, -1):
                    if ltf_scan[i]["c"] < ltf_scan[i]["o"]:
                        ltf_fvg_top = ltf_scan[i]["h"]
                        break
            
            # Fallback terakhir: Ambil area diskon 50% dari dorongan BOS
            if not ltf_fvg_top:
                ltf_fvg_top = min_c["l"] + (last_swing_high["h"] - min_c["l"]) * 0.5

            self.entry = ltf_fvg_top
            self.sl = min_c["l"] - (min_c["l"] * SL_BUFFER_PCT / 100)
            if self.sl >= self.entry: return False 

            self.tp = get_target(c_conf, self.direction)
            if not self.tp or self.tp <= self.entry: self.tp = self.entry + ((self.entry - self.sl) * 2.0)

            risk = self.entry - self.sl
            reward = self.tp - self.entry
            if risk > 0 and reward / risk >= MIN_RR:
                self.state = "WAIT_ENTRY"
                self.last_signal_time = time.time()
                self.place_limit_and_sl()
                return True

        elif self.direction == "SELL":
            # 1. Cari titik tertinggi absolut
            max_c = max(ltf_scan, key=lambda x: x["h"])
            max_idx = ltf_scan.index(max_c)

            if max_c["h"] < poi["fvg_b"] or max_c["h"] > poi["ob_t"]: return False

            # 2. Cari True Swing Low terakhir SEBELUM titik tertinggi
            swing_lows = []
            for i in range(1, max_idx):
                if ltf_scan[i]["l"] < ltf_scan[i-1]["l"] and ltf_scan[i]["l"] < ltf_scan[i+1]["l"]:
                    swing_lows.append(ltf_scan[i])
            if not swing_lows: return False
            last_swing_low = swing_lows[-1]

            # 3. Cari True BOS/ChoCh (Body Close menembus Swing Low)
            choch_idx = -1
            for i in range(max_idx + 1, len(ltf_scan)):
                if ltf_scan[i]["c"] < last_swing_low["l"]:
                    choch_idx = i
                    break
            if choch_idx == -1: return False 
            if choch_idx < len(ltf_scan) - 6: return False

            # 4. Cari LTF FVG Bearish
            ltf_fvg_bottom = None
            for i in range(max_idx, choch_idx - 1):
                c0, c2 = ltf_scan[i], ltf_scan[i+2]
                if c0["l"] > c2["h"]:
                    ltf_fvg_bottom = c2["h"] # Jaring di lantai LTF FVG
                    break

            if not ltf_fvg_bottom:
                for i in range(choch_idx - 1, max_idx - 1, -1):
                    if ltf_scan[i]["c"] > ltf_scan[i]["o"]:
                        ltf_fvg_bottom = ltf_scan[i]["l"]
                        break
                        
            if not ltf_fvg_bottom:
                ltf_fvg_bottom = max_c["h"] - (max_c["h"] - last_swing_low["l"]) * 0.5

            self.entry = ltf_fvg_bottom
            self.sl = max_c["h"] + (max_c["h"] * SL_BUFFER_PCT / 100)
            if self.sl <= self.entry: return False

            self.tp = get_target(c_conf, self.direction)
            if not self.tp or self.tp >= self.entry: self.tp = self.entry - ((self.sl - self.entry) * 2.0)

            risk = self.sl - self.entry
            reward = self.entry - self.tp
            if risk > 0 and reward / risk >= MIN_RR:
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
        
        c_p = klines_data[self.symbol][self.tf_poi]
        c_c = klines_data[self.symbol][self.tf_conf]
        c_t = klines_data[self.symbol][self.tf_trig]
        price = live_prices[self.symbol]
        
        if not c_p or price == 0: return
        if time.time() - self.last_signal_time < self.cooldown and self.state == "IDLE": return
            
        if self.setup_time and time.time() - self.setup_time > 3600:
            if self.state in ["WAIT_C1", "WAIT_C2", "WAIT_C3", "WAIT_OB_TOUCH"]:
                if self.active_poi: self.ignored_pois.append(self.active_poi["t"])
                self.reset(); return

        if self.state == "WAIT_ENTRY" and time.time() - self.last_signal_time > 7200:
            send_telegram(f"⏳ *{self.symbol}* [{self.mode}] Batal: Jaring Limit kadaluarsa (2 Jam tidak dijemput).")
            self.cancel_pending_orders()
            if self.active_poi: self.ignored_pois.append(self.active_poi["t"])
            self.reset()
            return

        if self.state == "IDLE":
            pois = get_unmitigated_poi(c_p)
            for poi in reversed(pois):
                if poi["t"] in self.ignored_pois: continue
                
                in_fvg = poi["fvg_b"] <= price <= poi["fvg_t"]
                in_ob = poi["ob_b"] <= price <= poi["ob_t"]
                
                if in_fvg or in_ob:
                    self.direction = poi["dir"]
                    self.active_poi = poi
                    self.active_zone = "FVG" if in_fvg else "OB"
                    self.state = "WAIT_C1"
                    self.setup_time = time.time()
                    return
                    
        elif self.state == "WAIT_OB_TOUCH":
            poi = self.active_poi
            if self.direction == "BUY":
                if price < poi["ob_b"]: self.reset(); return
                if price <= poi["ob_t"]: 
                    self.active_zone = "OB"
                    self.state = "WAIT_C1"
                    self.setup_time = time.time()
            elif self.direction == "SELL":
                if price > poi["ob_t"]: self.reset(); return
                if price >= poi["ob_b"]: 
                    self.active_zone = "OB"
                    self.state = "WAIT_C1"
                    self.setup_time = time.time()

        elif self.state in ["WAIT_C1", "WAIT_C2", "WAIT_C3"]:
            poi = self.active_poi
            if self.direction == "BUY":
                if price < poi["ob_b"]: self.reset(); return 
                if self.active_zone == "FVG" and price < poi["fvg_b"]:
                    self.active_zone = "OB"
                    self.state = "WAIT_C1"
                    self.setup_time = time.time()
                    self.fc1 = None; self.fc2 = None
                    return
            elif self.direction == "SELL":
                if price > poi["ob_t"]: self.reset(); return 
                if self.active_zone == "FVG" and price > poi["fvg_t"]:
                    self.active_zone = "OB"
                    self.state = "WAIT_C1"
                    self.setup_time = time.time()
                    self.fc1 = None; self.fc2 = None
                    return

            if len(c_c) < 1: return
            prev = c_c[-1] 
            if prev["t"] == self.last_processed_conf_t: return
            self.last_processed_conf_t = prev["t"]

            if self.state == "WAIT_C1":
                self.fc1 = prev
                if self.check_and_trigger(f"C1 ({self.active_zone})", c_t, c_c, 15): return
                self.state = "WAIT_C2"
                
            elif self.state == "WAIT_C2":
                self.fc2 = prev
                is_sweep = False
                if self.direction == "BUY" and prev["l"] < self.fc1["l"]: is_sweep = True
                if self.direction == "SELL" and prev["h"] > self.fc1["h"]: is_sweep = True
                if is_sweep:
                    if self.check_and_trigger(f"C2 Sweep ({self.active_zone})", c_t, c_c, 30): return
                self.state = "WAIT_C3"
                
            elif self.state == "WAIT_C3":
                if self.check_and_trigger(f"C3 Final ({self.active_zone})", c_t, c_c, 45): return
                if self.active_zone == "FVG":
                    self.state = "WAIT_OB_TOUCH"
                    self.fc1 = None; self.fc2 = None
                else: 
                    self.ignored_pois.append(self.active_poi["t"])
                    self.reset() 
                
        elif self.state == "WAIT_ENTRY":
            last_k = c_t[-1] if len(c_t) > 0 else None
            hit_tp_sl_buy = self.direction == "BUY" and (price >= self.tp or price <= self.sl or (last_k and (last_k["h"] >= self.tp or last_k["l"] <= self.sl)))
            hit_tp_sl_sell = self.direction == "SELL" and (price <= self.tp or price >= self.sl or (last_k and (last_k["l"] <= self.tp or last_k["h"] >= self.sl)))
            
            if hit_tp_sl_buy or hit_tp_sl_sell:
                send_telegram(f"❌ *{self.symbol}* [{self.mode}] Batal: Harga lari ke TP/SL duluan (Tertinggal kereta).")
                self.cancel_pending_orders()
                self.ignored_pois.append(self.active_poi["t"])
                self.reset()

    def place_limit_and_sl(self):
        def run():
            try:
                p = precisions.get(self.symbol)
                q_str = round_v((MARGIN_USDT * LEVERAGE) / self.entry, p["step"])
                q_float = float(q_str)
                
                if q_float < p["min_qty"] or (q_float * self.entry) < p["min_notional"]:
                    self.reset(); return
                    
                post_api({"symbol": self.symbol, "leverage": LEVERAGE}, "/fapi/v1/leverage")
                with state_lock:
                    active_signals[self.symbol] = {"mode": self.mode, "tp": self.tp, "sl": self.sl, "dir": self.direction, "qty": q_float}
                    
                limit_params = {"symbol": self.symbol, "side": self.direction, "type": "LIMIT", "quantity": q_str, "price": round_v(self.entry, p["tick"]), "timeInForce": "GTC"}
                res = post_api(limit_params, "/fapi/v1/order")
                
                if "orderId" in res:
                    self.pending_order_id = res["orderId"]
                    opp = "SELL" if self.direction == "BUY" else "BUY"
                    
                    sl_params = {
                        "symbol": self.symbol, 
                        "side": opp, 
                        "algoType": "CONDITIONAL", 
                        "type": "STOP_MARKET", 
                        "triggerPrice": round_v(self.sl, p["tick"]), 
                        "quantity": q_str, 
                        "reduceOnly": "true"
                    }
                    sl_res = post_api(sl_params, "/fapi/v1/algoOrder")
                    
                    if "code" in sl_res: send_telegram(f"⚠️ ERROR BINANCE (SL {self.symbol}): {sl_res.get('msg')}")
                    else: self.pending_sl_algo_id = sl_res.get("algoId") or sl_res.get("orderId")
                    
                    ep_str = round_v(self.entry, p["tick"])
                    sl_str = round_v(self.sl, p["tick"])
                    tp_str = round_v(self.tp, p["tick"])
                    msg = f"⏳ *{self.symbol}* LIMIT + SL ({self.mode})\n📍 Dir: {self.direction}\n💰 Entry: `{ep_str}`\n🛑 SL: `{sl_str}`\n🎯 TP: `{tp_str}`"
                    send_telegram(msg)
                else: 
                    send_telegram(f"⚠️ ERROR BINANCE (LIMIT {self.symbol}): {res.get('msg')}")
                    with state_lock: active_signals.pop(self.symbol, None)
                    self.reset()
            except Exception as e:
                self.reset()
        threading.Thread(target=run, daemon=True).start()

    def cancel_pending_orders(self):
        def run():
            if self.pending_order_id: post_api({"symbol": self.symbol, "orderId": self.pending_order_id}, "/fapi/v1/order", method="DELETE")
            if self.pending_sl_algo_id: post_api({"symbol": self.symbol, "algoId": self.pending_sl_algo_id}, "/fapi/v1/algoOrder", method="DELETE")
        threading.Thread(target=run, daemon=True).start()

# ==============================================================================
# 📱 TELEGRAM CMD & UTILS 
# ==============================================================================
def telegram_cmd():
    global total_pnl, total_wins, total_losses, current_month_str
    global API_KEY, SECRET_KEY, MARGIN_USDT, LEVERAGE, SL_BUFFER_PCT, MIN_RR
    
    t = os.getenv("TELEGRAM_TOKEN")
    if not t: return
        
    lid = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{t}/getUpdates"
            r = requests.get(url, params={"offset": lid, "timeout": 10}).json()
            
            for i in r.get("result", []):
                lid = i["update_id"] + 1
                txt = i.get("message", {}).get("text", "")
                if not txt: continue
                logger.info(f"📥 Pesan Telegram Diterima: {txt}")
                txt = txt.strip().lower()
                def rep(msg): send_telegram(msg)
                parts = txt.split()
                cmd = parts[0]

                if cmd == "/setapi" and len(parts) > 1:
                    API_KEY = parts[1]; update_env_file("BINANCE_API_KEY", API_KEY); rep("✅ API KEY Berhasil diperbarui!")
                elif cmd == "/setsecret" and len(parts) > 1:
                    SECRET_KEY = parts[1]; update_env_file("BINANCE_SECRET_KEY", SECRET_KEY); rep("✅ SECRET KEY Berhasil diperbarui!")
                elif cmd == "/margin" and len(parts) > 1:
                    try: MARGIN_USDT = float(parts[1]); update_env_file("MARGIN_USDT", MARGIN_USDT); rep(f"✅ Margin diubah ke: *{MARGIN_USDT} USDT*")
                    except: rep("⚠️ Format angka salah.")
                elif cmd == "/leverage" and len(parts) > 1:
                    try: LEVERAGE = int(parts[1]); update_env_file("LEVERAGE", LEVERAGE); rep(f"✅ Leverage diubah ke: *{LEVERAGE}x*")
                    except: rep("⚠️ Format angka salah.")
                elif cmd == "/buffer" and len(parts) > 1:
                    try: SL_BUFFER_PCT = float(parts[1]); update_env_file("SL_BUFFER_PCT", SL_BUFFER_PCT); rep(f"✅ SL Buffer diubah ke: *{SL_BUFFER_PCT}%*")
                    except: rep("⚠️ Format angka salah.")
                elif cmd == "/minrr" and len(parts) > 1:
                    try: MIN_RR = float(parts[1]); update_env_file("MIN_RR", MIN_RR); rep(f"✅ Min RR diubah ke: *{MIN_RR}*")
                    except: rep("⚠️ Format angka salah.")

                elif txt.startswith("/pnl"):
                    total_trades = total_wins + total_losses
                    wr = (total_wins / total_trades * 100) if total_trades > 0 else 0
                    mode_str = "DOUBLE (1H & 4H)" if config["ENABLE_1H"] and config["ENABLE_4H"] else "1H BIAS" if config["ENABLE_1H"] else "4H BIAS"
                    msg = f"📊 *PnL {current_month_str}*\n💰 Total: `{total_pnl:.4f} USDT`\n📈 Winrate: {wr:.1f}%\n✅ Wins: {total_wins} | ❌ Loss: {total_losses}\n⚙️ Mode: {mode_str}"
                    rep(msg)
                elif txt in ["/mode 1h", "/mode 4h", "/mode double"]:
                    config["ENABLE_1H"] = txt in ["/mode 1h", "/mode double"]; config["ENABLE_4H"] = txt in ["/mode 4h", "/mode double"]
                    for e in engines: e.cancel_pending_orders(); e.reset()
                    rep(f"✅ Mode diubah ke: {txt.upper()}")
                elif txt.startswith("/status"):
                    lines = ["📊 *STATUS BOT*\n", f"⚙️ Modal: {MARGIN_USDT} USDT | Lev: {LEVERAGE}x | Buffer: {SL_BUFFER_PCT}%\n", "━━━━━━━━━━━━━━━━━━━", "📈 *POSISI FLOATING*", "━━━━━━━━━━━━━━━━━━━\n"]
                    if positions:
                        for s, p in positions.items():
                            cur = live_prices.get(s, 0.0)
                            if cur == 0.0: cur = p['ep'] 
                            pnl = (cur - p['ep']) * p['qty'] if p['side'] == "BUY" else (p['ep'] - cur) * p['qty']
                            margin = (p['qty'] * p['ep']) / LEVERAGE
                            pnl_pct = (pnl / margin) * 100 if margin > 0 else 0
                            sign = "+" if pnl > 0 else ""
                            emo = "🟩" if pnl > 0 else "🟥"
                            pr = precisions.get(s, {}); tick = pr.get("tick", 4) if pr else 4
                            tp_val, sl_val = None, None
                            orders = post_api({"symbol": s}, "/fapi/v1/openOrders", method="GET")
                            if isinstance(orders, list):
                                for o in orders:
                                    sp = float(o.get("stopPrice", 0))
                                    if o.get("origType", o.get("type", "")) in ["TAKE_PROFIT_MARKET", "TAKE_PROFIT"] and sp > 0: tp_val = sp
                                    elif o.get("origType", o.get("type", "")) in ["STOP_MARKET", "STOP"] and sp > 0: sl_val = sp
                            algo_orders = post_api({"symbol": s}, "/fapi/v1/openAlgoOrders", method="GET")
                            if isinstance(algo_orders, list):
                                for o in algo_orders:
                                    sp = float(o.get("triggerPrice", o.get("stopPrice", 0)))
                                    if o.get("origType", o.get("type", o.get("orderType", ""))) in ["TAKE_PROFIT_MARKET", "TAKE_PROFIT"] and sp > 0: tp_val = sp
                                    elif o.get("origType", o.get("type", o.get("orderType", ""))) in ["STOP_MARKET", "STOP"] and sp > 0: sl_val = sp
                            tp_str = round_v(tp_val, tick) if tp_val else "-"
                            sl_str = round_v(sl_val, tick) if sl_val else "-"
                            rr_str = "-"
                            if tp_val and sl_val:
                                ep = p['ep']
                                risk = (ep - sl_val) if p['side'] == "BUY" else (sl_val - ep)
                                reward = (tp_val - ep) if p['side'] == "BUY" else (ep - tp_val)
                                if risk > 0: rr_str = f"1 : {reward / risk:.2f}"
                            lines.append(f"{emo} *{s}* ({p['side']})\n   Entry : `{p['ep']}`\n   TP    : `{tp_str}`\n   SL    : `{sl_str}`\n   PnL   : `{sign}{pnl:.2f} USDT` (`{sign}{pnl_pct:.2f}%`)\n   RR    : `{rr_str}`\n")
                    else: lines.append("💤 Tidak ada posisi aktif.\n")
                    lines.append("━━━━━━━━━━━━━━━━━━━\n🔍 *ANALISA AKTIF*\n━━━━━━━━━━━━━━━━━━━\n")
                    analyzing = [e for e in engines if e.state in ["WAIT_C1", "WAIT_C2", "WAIT_C3", "WAIT_ENTRY", "WAIT_OB_TOUCH"]]
                    if analyzing:
                        for e in analyzing:
                            mode_clean = "1H" if "1H" in e.mode else "4H"
                            state_clean = e.state.replace('_', ' ') + (f" ({e.active_zone})" if getattr(e, 'active_zone', '') else "")
                            pr = precisions.get(e.symbol, {}); tick = pr.get("tick", 4) if pr else 4
                            tp_str = round_v(e.tp, tick) if e.tp else "-"
                            sl_str = round_v(e.sl, tick) if e.sl else "-"
                            rr_str = "-"
                            if e.state == "WAIT_ENTRY" and e.entry and e.tp and e.sl:
                                risk = (e.entry - e.sl) if e.direction == "BUY" else (e.sl - e.entry)
                                reward = (e.tp - e.entry) if e.direction == "BUY" else (e.entry - e.tp)
                                if risk > 0: rr_str = f"1 : {reward / risk:.2f}"
                            lines.append(f"• *{e.symbol}* ({mode_clean})\n  Bias  : `{state_clean}`")
                            if e.state == "WAIT_ENTRY" and e.entry: lines.append(f"  Entry : `{round_v(e.entry, tick)}`")
                            lines.append(f"  TP    : `{tp_str}`\n  SL    : `{sl_str}`")
                            if e.state == "WAIT_ENTRY": lines.append(f"  RR    : `{rr_str}`")
                            lines.append("")
                    else: lines.append("💤 Semua koin sedang IDLE.\n")
                    rep("\n".join(lines).strip())
                elif txt.startswith("/close"):
                    target = txt.replace("/close", "").strip().upper()
                    if target != "ALL" and not target.endswith("USDT"): target += "USDT"
                    to_close = [s for s in positions] if target == "ALL" else ([target] if target in positions else [])
                    for s in to_close:
                        p, pr = positions[s], precisions.get(s)
                        post_api({"symbol": s, "side": "SELL" if p["side"] == "BUY" else "BUY", "type": "MARKET", "quantity": round_v(p["qty"], pr["step"]) if pr else str(p["qty"]), "reduceOnly": "true"}, "/fapi/v1/order")
                        post_api({"symbol": s}, "/fapi/v1/allOpenOrders", method="DELETE")
                        post_api({"symbol": s}, "/fapi/v1/algoOpenOrders", method="DELETE")
                        rep(f"🛑 {s} ditutup paksa via Market.")
                elif txt.startswith("/bep"):
                    target = txt.replace("/bep", "").strip().upper()
                    if target != "ALL" and not target.endswith("USDT"): target += "USDT"
                    to_bep = [s for s in positions] if target == "ALL" else ([target] if target in positions else [])
                    for s in to_bep:
                        p, pr = positions[s], precisions.get(s)
                        for api_ep in ["/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"]:
                            orders = post_api({"symbol": s}, api_ep, method="GET")
                            if isinstance(orders, list):
                                for o in orders:
                                    if o.get("origType", o.get("type", o.get("orderType", ""))) in ["STOP_MARKET", "STOP"]:
                                        if api_ep == "/fapi/v1/openOrders": post_api({"symbol": s, "orderId": o.get("orderId")}, "/fapi/v1/order", method="DELETE")
                                        else: post_api({"symbol": s, "algoId": o.get("algoId")}, "/fapi/v1/algoOrder", method="DELETE")
                        sl_p = {"symbol": s, "side": "SELL" if p["side"] == "BUY" else "BUY", "type": "STOP_MARKET", "stopPrice": round_v(p["ep"], pr["tick"]), "closePosition": "true"}
                        res_sl = post_api(sl_p, "/fapi/v1/order")
                        if "code" in res_sl:
                            sl_p.pop("stopPrice"); sl_p.update({"algoType": "CONDITIONAL", "triggerPrice": round_v(p["ep"], pr["tick"])})
                            post_api(sl_p, "/fapi/v1/algoOrder")
                        rep(f"🛡️ {s} Stop Loss dipindah ke Entry (BEP) | TP Tetap Aman.")
                elif txt.startswith("/help"):
                    rep("*📖 COMMANDS:*\n`/setapi <api>`\n`/setsecret <secret>`\n`/margin <angka>`\n`/leverage <angka>`\n`/buffer <angka>`\n`/minrr <angka>`\n`/status` — Status bot\n`/pnl` — PnL bulan ini\n`/mode 1h/4h/double` — Ubah Mode\n`/close koin/all` — Tutup Posisi\n`/bep koin/all` — SL ke Entry")
        except Exception as e: time.sleep(5)

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
                    pnl = float(row[3]); total_pnl += pnl
                    if pnl > 0: total_wins += 1
                    else: total_losses += 1
    except Exception: pass

# ==============================================================================
# ANTI MATI SURI
# ==============================================================================
def keep_alive_listenkey():
    while True:
        time.sleep(30 * 60)
        try: requests.put(BASE_URL + "/fapi/v1/listenKey", headers={"X-MBX-APIKEY": API_KEY}, timeout=10)
        except Exception: pass

# ==============================================================================
# WS HANDLER & STARTUP
# ==============================================================================
def on_market_msg(ws, m):
    try:
        d = json.loads(m)
        if "data" not in d: return
        k, s, tf = d["data"]["k"], d["data"]["s"], d["data"]["k"]["i"]
        if tf == "1m": live_prices[s] = float(k["c"])
        if k["x"]:
            klines_data[s][tf].append({"t": k["t"], "o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"])})
            klines_data[s][tf] = klines_data[s][tf][-80:]
        for e in engines:
            if e.symbol == s: e.tick()
    except Exception: pass

def on_user_msg(ws, m):
    global total_pnl, total_wins, total_losses
    try:
        d = json.loads(m)
        if d.get("e") == "ORDER_TRADE_UPDATE":
            o, s = d["o"], d["o"]["s"]
            if o["X"] == "FILLED" and o.get("o") == "LIMIT" and s in active_signals:
                sig, pr = active_signals[s], precisions.get(s, {})
                opp = "SELL" if sig["dir"] == "BUY" else "BUY"
                for e in engines:
                    if e.symbol == s and e.pending_sl_algo_id:
                        post_api({"symbol": s, "orderId": e.pending_sl_algo_id}, "/fapi/v1/order", method="DELETE")
                        post_api({"symbol": s, "algoId": e.pending_sl_algo_id}, "/fapi/v1/algoOrder", method="DELETE")
                sl_p = {"symbol": s, "side": opp, "type": "STOP_MARKET", "stopPrice": round_v(sig["sl"], pr["tick"]), "closePosition": "true"}
                res_sl = post_api(sl_p, "/fapi/v1/order")
                if "code" in res_sl:
                    sl_p.pop("stopPrice"); sl_p.update({"algoType": "CONDITIONAL", "triggerPrice": round_v(sig["sl"], pr["tick"])})
                    post_api(sl_p, "/fapi/v1/algoOrder")
                tp_p = {"symbol": s, "side": opp, "type": "TAKE_PROFIT_MARKET", "stopPrice": round_v(sig["tp"], pr["tick"]), "closePosition": "true"}
                res_tp = post_api(tp_p, "/fapi/v1/order")
                if "code" in res_tp:
                    tp_p.pop("stopPrice"); tp_p.update({"algoType": "CONDITIONAL", "triggerPrice": round_v(sig["tp"], pr["tick"])})
                    post_api(tp_p, "/fapi/v1/algoOrder")
                send_telegram(f"🚀 *{s}* LIMIT FILLED!\nTP: `{round_v(sig['tp'], pr['tick'])}` | SL: `{round_v(sig['sl'], pr['tick'])}` (Auto-Attached)")
            if o["X"] == "FILLED" and float(o.get("rp", 0)) != 0:
                rp = float(o["rp"])
                total_pnl += rp
                if rp > 0: total_wins += 1
                else: total_losses += 1
                order_type, orig_type = o.get("o"), o.get("ot", o.get("o"))
                if order_type in ["STOP_MARKET", "STOP"] or orig_type in ["STOP_MARKET", "STOP"]: reason = "Hit Stop Loss 🛑"
                elif order_type in ["TAKE_PROFIT_MARKET", "TAKE_PROFIT"] or orig_type in ["TAKE_PROFIT_MARKET", "TAKE_PROFIT"]: reason = "Hit Take Profit 🎯"
                else: reason = "Hit Take Profit 🎯 (Algo Trigger)" if rp > 0 else "Hit Stop Loss 🛑 (Wick Sweep/Jarum)"
                mode = active_signals.get(s, {}).get("mode", "UNKNOWN")
                log_trade(s, o["S"], rp, mode)
                send_telegram(f"{'✅' if rp > 0 else '❌'} *{s}* CLOSED!\nReason: {reason}\nPnL: `{rp:+.4f} USDT`")
        if d.get("e") == "ACCOUNT_UPDATE":
            for p in d["a"]["P"]:
                pa, s = float(p["pa"]), p["s"]
                with state_lock:
                    if pa == 0:
                        positions.pop(s, None); active_signals.pop(s, None)
                        post_api({"symbol": s}, "/fapi/v1/allOpenOrders", method="DELETE"); post_api({"symbol": s}, "/fapi/v1/algoOpenOrders", method="DELETE")
                        for e in engines:
                            if e.symbol == s: e.reset()
                    else:
                        positions[s] = {"side": "BUY" if pa > 0 else "SELL", "qty": abs(pa), "ep": float(p["ep"])}
                        for e in engines:
                            if e.symbol == s:
                                if e.active_poi: e.ignored_pois.append(e.active_poi["t"])
                                e.reset()
    except Exception: pass

def start_ws_with_reconnect(url, on_msg):
    def run():
        while True:
            try: websocket.WebSocketApp(url, on_message=on_msg).run_forever(ping_interval=60, ping_timeout=30)
            except Exception: pass
            time.sleep(5)
    threading.Thread(target=run, daemon=True).start()

def start_user_ws():
    threading.Thread(target=keep_alive_listenkey, daemon=True).start()
    while True:
        try:
            lk = requests.post(BASE_URL + "/fapi/v1/listenKey", headers={"X-MBX-APIKEY": API_KEY}).json().get("listenKey")
            if lk: websocket.WebSocketApp(f"{WS_BASE}/ws/{lk}", on_message=on_user_msg).run_forever(ping_interval=60, ping_timeout=30)
        except Exception: pass
        time.sleep(5)

def start():
    load_precisions()
    load_monthly_pnl()
    try:
        r = post_api({}, "/fapi/v2/positionRisk", method="GET")
        if isinstance(r, list):
            for p in r:
                amt, s = float(p["positionAmt"]), p["symbol"]
                if amt != 0 and s in symbols: positions[s] = {"side": "BUY" if amt > 0 else "SELL", "qty": abs(amt), "ep": float(p["entryPrice"])}
    except Exception: pass
    for s in symbols:
        for tf in ["4h", "1h", "15m", "5m", "1m"]:
            try:
                r = requests.get(BASE_URL + "/fapi/v1/klines", params={"symbol": s, "interval": tf, "limit": 80}).json()
                klines_data[s][tf] = [{"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])} for k in r[:-1]]
            except Exception: pass
    threading.Thread(target=telegram_cmd, daemon=True).start()
    streams = "/".join([f"{s.lower()}@kline_{tf}" for s in symbols for tf in ["1m", "5m", "15m", "1h", "4h"]])
    start_ws_with_reconnect(f"{WS_BASE}/stream?streams={streams}", on_market_msg)
    threading.Thread(target=start_user_ws, daemon=True).start()

engines = [Engine(s, m) for s in symbols for m in ["1H_BIAS", "4H_BIAS"]]

if __name__ == "__main__":
    start()
    print("🔥 BOT v9.0 (THE TRUE SMC EXECUTION) ACTIVE...")
    while True:
        time.sleep(1)
