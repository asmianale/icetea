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
                requests.post(url, json=payload, timeout=10)
            except Exception as e:
                pass
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
# LOGIKA SMC — DUAL ZONE
# ==============================================================================
def get_unmitigated_poi(candles, depth=40, min_size_pct=0.1):
    if len(candles) < depth + 4: return []
    pois = []
    start_idx = max(0, len(candles) - depth)

    for i in range(start_idx, len(candles) - 3):
        c0 = candles[i]
        c1 = candles[i+1]
        c2 = candles[i+2]

        if c0["h"] < c2["l"]:
            if (c2["l"] - c0["h"]) / c0["h"] * 100 >= min_size_pct:
                fvg_b, fvg_t = c0["h"], c2["l"]
                ob_b, ob_t = c0["l"], c0["h"] 
                is_valid = True
                for j in range(i+3, len(candles)):
                    if candles[j]["l"] < ob_b: is_valid = False; break 
                if is_valid: pois.append({"dir": "BUY", "fvg_b": fvg_b, "fvg_t": fvg_t, "ob_b": ob_b, "ob_t": ob_t, "t": c0["t"]})

        elif c0["l"] > c2["h"]:
            if (c0["l"] - c2["h"]) / c2["h"] * 100 >= min_size_pct:
                fvg_t, fvg_b = c0["l"], c2["h"]
                ob_b, ob_t = c0["l"], c0["h"] 
                is_valid = True
                for j in range(i+3, len(candles)):
                    if candles[j]["h"] > ob_t: is_valid = False; break 
                if is_valid: pois.append({"dir": "SELL", "fvg_b": fvg_b, "fvg_t": fvg_t, "ob_b": ob_b, "ob_t": ob_t, "t": c0["t"]})

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
# ENGINE (RETROSPECTIVE X-RAY SNIPER)
# ==============================================================================
class Engine:
    def __init__(self, symbol, mode):
        self.symbol = symbol
        self.mode = mode
        self.ignored_pois = [] # --- [FIX 1]: List Blacklist POI Zombie ---
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
        ltf_scan = c_trig[-scan_depth:] 
        if len(ltf_scan) < 6: return False
        poi = self.active_poi
            
        for i in range(len(ltf_scan)-1, 4, -1):
            curr = ltf_scan[i]
            body = abs(curr["c"] - curr["o"])
            wick = curr["h"] - curr["l"]
            
            if wick > 0 and body / wick > 0.6:
                prev_5 = ltf_scan[i-5:i]
                highs = [max(x["o"], x["c"]) for x in prev_5]
                lows = [min(x["o"], x["c"]) for x in prev_5]
                
                if self.direction == "BUY" and curr["c"] > max(highs):
                    cisd_low = min(c["l"] for c in ltf_scan[i-5:i+1])
                    if cisd_low > poi["fvg_t"] or cisd_low < poi["ob_b"]: continue
                        
                    self.entry = curr["c"] - (body * 0.25)
                    self.sl = cisd_low - (cisd_low * SL_BUFFER_PCT / 100)
                    if self.sl >= self.entry: continue 
                    
                    self.tp = get_target(c_conf, self.direction)
                    if not self.tp or self.tp <= self.entry: self.tp = self.entry + ((self.entry - self.sl) * 2.0)
                    
                    risk = self.entry - self.sl
                    reward = self.tp - self.entry
                    
                    if risk > 0 and reward / risk >= MIN_RR:
                        self.state = "WAIT_ENTRY"
                        self.last_signal_time = time.time()
                        self.place_limit_and_sl()
                        return True
                        
                elif self.direction == "SELL" and curr["c"] < min(lows):
                    cisd_high = max(c["h"] for c in ltf_scan[i-5:i+1])
                    if cisd_high < poi["fvg_b"] or cisd_high > poi["ob_t"]: continue
                        
                    self.entry = curr["c"] + (body * 0.25)
                    self.sl = cisd_high + (cisd_high * SL_BUFFER_PCT / 100)
                    if self.sl <= self.entry: continue
                    
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
                if self.active_poi: self.ignored_pois.append(self.active_poi["t"]) # --- [FIX 1]: Blacklist jika expired
                self.reset(); return

        if self.state == "IDLE":
            pois = get_unmitigated_poi(c_p)
            for poi in reversed(pois):
                if poi["t"] in self.ignored_pois: continue # --- [FIX 1]: Abaikan POI yang sudah diblacklist
                
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
                    self.ignored_pois.append(self.active_poi["t"]) # --- [FIX 1]: Blacklist jika OB juga gagal total
                    self.reset() 
                
        elif self.state == "WAIT_ENTRY":
            hit_tp_sl_buy = self.direction == "BUY" and (price >= self.tp or price <= self.sl)
            hit_tp_sl_sell = self.direction == "SELL" and (price <= self.tp or price >= self.sl)
            
            if hit_tp_sl_buy or hit_tp_sl_sell:
                send_telegram(f"❌ {self.symbol} [{self.mode}] Batal: Harga lari ke TP/SL duluan.")
                self.cancel_pending_orders()
                self.ignored_pois.append(self.active_poi["t"]) # --- [FIX 1]: Blacklist jika batal ditinggal lari
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
                    active_signals[self.symbol] = {"mode": self.mode, "tp": self.tp, "dir": self.direction, "qty": q_float}
                    
                limit_params = {"symbol": self.symbol, "side": self.direction, "type": "LIMIT", "quantity": q_str, "price": round_v(self.entry, p["tick"]), "timeInForce": "GTC"}
                res = post_api(limit_params, "/fapi/v1/order")
                
                if "orderId" in res:
                    self.pending_order_id = res["orderId"]
                    opp = "SELL" if self.direction == "BUY" else "BUY"
                    
                    sl_params = {
                        "symbol": self.symbol, "side": opp, "type": "STOP_MARKET", 
                        "stopPrice": round_v(self.sl, p["tick"]), "quantity": q_str, "reduceOnly": "true"
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
                txt = txt.strip().lower()
                def rep(msg): send_telegram(msg)

                if txt.startswith("/pnl"):
                    total_trades = total_wins + total_losses
                    wr = (total_wins / total_trades * 100) if total_trades > 0 else 0
                    mode_str = "DOUBLE (1H & 4H)" if config["ENABLE_1H"] and config["ENABLE_4H"] else "1H BIAS" if config["ENABLE_1H"] else "4H BIAS"
                    msg = f"📊 *PnL {current_month_str}*\n💰 Total: `{total_pnl:.4f} USDT`\n📈 Winrate: {wr:.1f}%\n✅ Wins: {total_wins} | ❌ Loss: {total_losses}\n⚙️ Mode: {mode_str}"
                    rep(msg)
                
                elif txt in ["/mode 1h", "/mode 4h", "/mode double"]:
                    config["ENABLE_1H"] = txt in ["/mode 1h", "/mode double"]
                    config["ENABLE_4H"] = txt in ["/mode 4h", "/mode double"]
                    for e in engines: e.cancel_pending_orders(); e.reset()
                    rep(f"✅ Mode diubah ke: {txt.upper()}")

                elif txt.startswith("/status"):
                    lines = ["📊 *STATUS BOT*\n"]
                    lines.append("━━━━━━━━━━━━━━━━━━━")
                    lines.append("📈 *POSISI FLOATING*")
                    lines.append("━━━━━━━━━━━━━━━━━━━\n")

                    if positions:
                        for s, p in positions.items():
                            cur = live_prices.get(s, 0.0)
                            if cur == 0.0: cur = p['ep'] 
                            
                            pnl = (cur - p['ep']) * p['qty'] if p['side'] == "BUY" else (p['ep'] - cur) * p['qty']
                            margin = (p['qty'] * p['ep']) / LEVERAGE
                            pnl_pct = (pnl / margin) * 100 if margin > 0 else 0
                            sign = "+" if pnl > 0 else ""
                            emo = "🟩" if pnl > 0 else "🟥"

                            pr = precisions.get(s, {})
                            tick = pr.get("tick", 4) if pr else 4
                            tp_val, sl_val = None, None
                            
                            orders = post_api({"symbol": s}, "/fapi/v1/openOrders", method="GET")
                            if isinstance(orders, list):
                                for o in orders:
                                    o_type = o.get("type", "")
                                    sp = float(o.get("stopPrice", 0))
                                    if o_type in ["TAKE_PROFIT_MARKET", "TAKE_PROFIT"] and sp > 0: tp_val = sp
                                    elif o_type in ["STOP_MARKET", "STOP"] and sp > 0: sl_val = sp

                            algo_orders = post_api({"symbol": s}, "/fapi/v1/openAlgoOrders", method="GET")
                            if isinstance(algo_orders, list):
                                for o in algo_orders:
                                    o_type = o.get("type", o.get("orderType", ""))
                                    sp = float(o.get("stopPrice", 0))
                                    if o_type in ["TAKE_PROFIT_MARKET", "TAKE_PROFIT"] and sp > 0: tp_val = sp
                                    elif o_type in ["STOP_MARKET", "STOP"] and sp > 0: sl_val = sp

                            tp_str = round_v(tp_val, tick) if tp_val else "-"
                            sl_str = round_v(sl_val, tick) if sl_val else "-"

                            rr_str = "-"
                            if tp_val and sl_val:
                                ep = p['ep']
                                risk = (ep - sl_val) if p['side'] == "BUY" else (sl_val - ep)
                                reward = (tp_val - ep) if p['side'] == "BUY" else (ep - tp_val)
                                if risk > 0:
                                    rr = reward / risk
                                    rr_str = f"1 : {rr:.2f}"

                            lines.append(f"{emo} *{s}* ({p['side']})")
                            lines.append(f"   Entry : `{p['ep']}`")
                            lines.append(f"   TP    : `{tp_str}`")
                            lines.append(f"   SL    : `{sl_str}`")
                            lines.append(f"   PnL   : `{sign}{pnl:.2f} USDT` (`{sign}{pnl_pct:.2f}%`)")
                            lines.append(f"   RR    : `{rr_str}`\n")
                    else:
                        lines.append("💤 Tidak ada posisi aktif.\n")

                    lines.append("━━━━━━━━━━━━━━━━━━━")
                    lines.append("🔍 *ANALISA AKTIF*")
                    lines.append("━━━━━━━━━━━━━━━━━━━\n")

                    analyzing = [e for e in engines if e.state in ["WAIT_C1", "WAIT_C2", "WAIT_C3", "WAIT_ENTRY", "WAIT_OB_TOUCH"]]
                    if analyzing:
                        for e in analyzing:
                            mode_clean = "1H" if "1H" in e.mode else "4H"
                            state_clean = e.state.replace('_', ' ')
                            
                            zone_label = getattr(e, 'active_zone', '')
                            if zone_label: state_clean += f" ({zone_label})"
                            
                            pr = precisions.get(e.symbol, {})
                            tick = pr.get("tick", 4) if pr else 4
                            
                            tp_str = round_v(e.tp, tick) if e.tp else "-"
                            sl_str = round_v(e.sl, tick) if e.sl else "-"

                            rr_str = "-"
                            if e.state == "WAIT_ENTRY" and e.entry and e.tp and e.sl:
                                risk = (e.entry - e.sl) if e.direction == "BUY" else (e.sl - e.entry)
                                reward = (e.tp - e.entry) if e.direction == "BUY" else (e.entry - e.tp)
                                if risk > 0:
                                    rr = reward / risk
                                    rr_str = f"1 : {rr:.2f}"

                            lines.append(f"• *{e.symbol}* ({mode_clean})")
                            lines.append(f"  Bias  : `{state_clean}`")
                            
                            if e.state == "WAIT_ENTRY" and e.entry:
                                lines.append(f"  Entry : `{round_v(e.entry, tick)}`")
                                
                            lines.append(f"  TP    : `{tp_str}`")
                            lines.append(f"  SL    : `{sl_str}`")
                            if e.state == "WAIT_ENTRY":
                                lines.append(f"  RR    : `{rr_str}`")
                            lines.append("")
                    else:
                        lines.append("💤 Semua koin sedang IDLE.\n")

                    rep("\n".join(lines).strip())

                elif txt.startswith("/close"):
                    target = txt.replace("/close", "").strip().upper()
                    if target != "ALL" and not target.endswith("USDT"): target += "USDT"
                    to_close = [s for s in positions] if target == "ALL" else ([target] if target in positions else [])
                    for s in to_close:
                        p, pr = positions[s], precisions.get(s)
                        qty_str = round_v(p["qty"], pr["step"]) if pr else str(p["qty"])
                        side = "SELL" if p["side"] == "BUY" else "BUY"
                        post_api({"symbol": s, "side": side, "type": "MARKET", "quantity": qty_str, "reduceOnly": "true"}, "/fapi/v1/order")
                        post_api({"symbol": s}, "/fapi/v1/allOpenOrders", method="DELETE")
                        post_api({"symbol": s}, "/fapi/v1/algoOpenOrders", method="DELETE")
                        rep(f"🛑 {s} ditutup paksa via Market.")

                elif txt.startswith("/bep"):
                    target = txt.replace("/bep", "").strip().upper()
                    if target != "ALL" and not target.endswith("USDT"): target += "USDT"
                    to_bep = [s for s in positions] if target == "ALL" else ([target] if target in positions else [])
                    for s in to_bep:
                        p, pr = positions[s], precisions.get(s)
                        
                        algo_orders = post_api({"symbol": s}, "/fapi/v1/openAlgoOrders", method="GET")
                        if isinstance(algo_orders, list):
                            for order in algo_orders:
                                if order.get("type", order.get("orderType", "")) in ["STOP_MARKET", "STOP"]:
                                    post_api({"symbol": s, "algoId": order.get("algoId")}, "/fapi/v1/algoOrder", method="DELETE")
                        
                        opp = "SELL" if p["side"] == "BUY" else "BUY"
                        qty_str = round_v(p["qty"], pr["step"])
                        
                        post_api({
                            "symbol": s, "side": opp, "type": "STOP_MARKET", 
                            "stopPrice": round_v(p["ep"], pr["tick"]), "quantity": qty_str, "reduceOnly": "true"
                        }, "/fapi/v1/algoOrder")
                        rep(f"🛡️ {s} Stop Loss dipindah ke Entry (BEP) | TP Tetap Aman.")
                        
                elif txt.startswith("/help"):
                    msg = "*📖 Commands:*\n/status — Status bot\n/pnl — PnL bulan ini\n/mode <1h/4h/double> — Ubah Mode\n/close <koin/all> — Tutup Posisi\n/bep <koin/all> — SL ke Entry"
                    rep(msg)
        except Exception as e:
            time.sleep(5)

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
    except Exception as e:
        pass

# ==============================================================================
# ANTI MATI SURI
# ==============================================================================
def keep_alive_listenkey():
    while True:
        time.sleep(30 * 60)
        try:
            url = BASE_URL + "/fapi/v1/listenKey"
            headers = {"X-MBX-APIKEY": API_KEY}
            requests.put(url, headers=headers, timeout=10)
        except Exception as e:
            pass

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
    except Exception as e:
        pass

def on_user_msg(ws, m):
    global total_pnl, total_wins, total_losses
    try:
        d = json.loads(m)
        if d.get("e") == "ORDER_TRADE_UPDATE":
            o, s = d["o"], d["o"]["s"]
            
            if o["X"] == "FILLED" and o.get("o") == "LIMIT" and s in active_signals:
                sig, pr = active_signals[s], precisions.get(s, {})
                opp = "SELL" if sig["dir"] == "BUY" else "BUY"
                qty_str = round_v(sig["qty"], pr["step"])
                
                params = {
                    "symbol": s, "side": opp, "type": "TAKE_PROFIT_MARKET", 
                    "stopPrice": round_v(sig["tp"], pr["tick"]), "quantity": qty_str, "reduceOnly": "true"
                }
                res = post_api(params, "/fapi/v1/algoOrder")
                
                if "code" in res: send_telegram(f"⚠️ ERROR BINANCE (TP {s}): {res.get('msg')}")
                else: send_telegram(f"🚀 *{s}* LIMIT FILLED!\nTP dipasang otomatis di `{round_v(sig['tp'], pr['tick'])}`")

            if o["X"] == "FILLED" and float(o.get("rp", 0)) != 0:
                rp = float(o["rp"])
                total_pnl += rp
                if rp > 0: total_wins += 1
                else: total_losses += 1
                
                reason = "Manual Close 🔘"
                order_type = o.get("o")
                if order_type == "STOP_MARKET": reason = "Hit Stop Loss 🛑"
                elif order_type == "TAKE_PROFIT_MARKET": reason = "Hit Take Profit 🎯"
                
                mode = active_signals.get(s, {}).get("mode", "UNKNOWN")
                log_trade(s, o["S"], rp, mode)
                emo = "✅" if rp > 0 else "❌"
                send_telegram(f"{emo} *{s}* CLOSED!\nReason: {reason}\nPnL: `{rp:+.4f} USDT`")

        if d.get("e") == "ACCOUNT_UPDATE":
            for p in d["a"]["P"]:
                pa, s = float(p["pa"]), p["s"]
                with state_lock:
                    if pa == 0:
                        positions.pop(s, None); active_signals.pop(s, None)
                        post_api({"symbol": s}, "/fapi/v1/allOpenOrders", method="DELETE")
                        post_api({"symbol": s}, "/fapi/v1/algoOpenOrders", method="DELETE")
                        for e in engines:
                            if e.symbol == s: e.reset()
                    else:
                        positions[s] = {"side": "BUY" if pa > 0 else "SELL", "qty": abs(pa), "ep": float(p["ep"])}
                        for e in engines:
                            if e.symbol == s:
                                # --- [FIX 2]: Hapus Hantu Analisa saat posisi terbentuk & Blacklist POI yang dipakai ---
                                if e.active_poi: e.ignored_pois.append(e.active_poi["t"])
                                e.reset()
    except Exception as e:
        pass

def start_ws_with_reconnect(url, on_msg):
    def run():
        while True:
            try:
                ws = websocket.WebSocketApp(url, on_message=on_msg)
                ws.run_forever(ping_interval=60, ping_timeout=30)
            except Exception as e:
                pass
            time.sleep(5)
    threading.Thread(target=run, daemon=True).start()

def start_user_ws():
    threading.Thread(target=keep_alive_listenkey, daemon=True).start()
    while True:
        try:
            url = f"{BASE_URL}/fapi/v1/listenKey"
            headers = {"X-MBX-APIKEY": API_KEY}
            lk = requests.post(url, headers=headers).json().get("listenKey")
            if lk:
                ws = websocket.WebSocketApp(f"{WS_BASE}/ws/{lk}", on_message=on_user_msg)
                ws.run_forever(ping_interval=60, ping_timeout=30)
        except Exception as e:
            pass
        time.sleep(5)

def start():
    load_precisions()
    load_monthly_pnl()
    
    try:
        r = post_api({}, "/fapi/v2/positionRisk", method="GET")
        if isinstance(r, list):
            for p in r:
                amt, s = float(p["positionAmt"]), p["symbol"]
                if amt != 0 and s in symbols:
                    positions[s] = {"side": "BUY" if amt > 0 else "SELL", "qty": abs(amt), "ep": float(p["entryPrice"])}
    except Exception as e:
        pass

    for s in symbols:
        for tf in ["4h", "1h", "15m", "5m", "1m"]:
            try:
                url = f"{BASE_URL}/fapi/v1/klines"
                r = requests.get(url, params={"symbol": s, "interval": tf, "limit": 80}).json()
                klines_data[s][tf] = [{"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])} for k in r[:-1]]
            except Exception as e:
                pass

    threading.Thread(target=telegram_cmd, daemon=True).start()
    streams = "/".join([f"{s.lower()}@kline_{tf}" for s in symbols for tf in ["1m", "5m", "15m", "1h", "4h"]])
    start_ws_with_reconnect(f"{WS_BASE}/stream?streams={streams}", on_market_msg)
    threading.Thread(target=start_user_ws, daemon=True).start()

engines = [Engine(s, m) for s in symbols for m in ["1H_BIAS", "4H_BIAS"]]

if __name__ == "__main__":
    start()
    print("🔥 BOT v7.2 (THE LOGIC SWEEPER) ACTIVE...")
    while True:
        time.sleep(1)
