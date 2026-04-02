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
        logger.warning(f"Time sync gagal: {e}")
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
                logger.warning(f"Telegram send gagal: {e}")
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
    if p > 0:
        return f"{round(v, p):.{p}f}"
    else:
        return str(int(round(v)))

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
        logger.error(f"Log trade error: {e}")

def post_api(params, endpoint, method="POST"):
    params["timestamp"] = ts()
    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(SECRET_KEY.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    payload = f"{query_string}&signature={signature}"
    url = f"{BASE_URL}{endpoint}"
    
    try:
        if method == "POST":
            response = requests.post(url, headers=headers, data=payload, timeout=10)
        elif method == "DELETE":
            response = requests.delete(f"{url}?{payload}", headers=headers, timeout=10)
        elif method == "GET":
            response = requests.get(f"{url}?{payload}", headers=headers, timeout=10)
        else:
            return {"error": "Unknown method"}
            
        return response.json()
    except Exception as e:
        logger.error(f"API call error {method} {endpoint}: {e}")
        return {"error": str(e)}

# ==============================================================================
# LOGIKA SMC — FVG DINAMIS & ORDER BLOCK
# ==============================================================================
def get_unmitigated_poi(candles, depth=40, min_size_pct=0.1):
    if len(candles) < depth + 4:
        return []
        
    pois = []
    start_idx = max(0, len(candles) - depth)

    for i in range(start_idx, len(candles) - 3):
        c0 = candles[i]
        c1 = candles[i+1]
        c2 = candles[i+2]

        # 🟢 BULLISH FVG
        if c0["h"] < c2["l"]:
            gap_size_pct = (c2["l"] - c0["h"]) / c0["h"] * 100
            if gap_size_pct >= min_size_pct:
                gap_bottom = c0["h"]
                gap_top = c2["l"]
                is_valid = True
                
                for j in range(i+3, len(candles)):
                    if candles[j]["l"] < gap_top:
                        gap_top = candles[j]["l"] 
                    if gap_top <= gap_bottom:
                        is_valid = False
                        break
                        
                if is_valid:
                    pois.append(("BUY", gap_bottom, gap_top, "FVG"))

        # 🔴 BEARISH FVG
        elif c0["l"] > c2["h"]:
            gap_size_pct = (c0["l"] - c2["h"]) / c2["h"] * 100
            if gap_size_pct >= min_size_pct:
                gap_top = c0["l"]
                gap_bottom = c2["h"]
                is_valid = True
                
                for j in range(i+3, len(candles)):
                    if candles[j]["h"] > gap_bottom:
                        gap_bottom = candles[j]["h"] 
                    if gap_bottom >= gap_top:
                        is_valid = False
                        break
                        
                if is_valid:
                    pois.append(("SELL", gap_bottom, gap_top, "FVG"))

        # 🟦 ORDER BLOCKS
        # Bullish OB
        if c0["c"] < c0["o"] and c1["c"] > c1["o"] and (c1["c"] - c1["o"]) > abs(c0["c"] - c0["o"]) * 1.5:
            if not any(candles[j]["l"] <= c0["l"] for j in range(i+2, len(candles))):
                pois.append(("BUY", c0["l"], c0["h"], "OB"))
                
        # Bearish OB
        elif c0["c"] > c0["o"] and c1["c"] < c1["o"] and (c1["o"] - c1["c"]) > abs(c0["c"] - c0["o"]) * 1.5:
            if not any(candles[j]["h"] >= c0["h"] for j in range(i+2, len(candles))):
                pois.append(("SELL", c0["l"], c0["h"], "OB"))
                
    return pois

def is_price_in_zone(price, poi):
    return poi[1] <= price <= poi[2]

def get_target(candles, direction, depth=20):
    recent = candles[-depth:]
    if len(recent) < 3:
        return None
        
    if direction == "BUY":
        highs = [c["h"] for i, c in enumerate(recent[1:-1]) if c["h"] > recent[i]["h"] and c["h"] > recent[i+2]["h"]]
        return max(highs) if highs else max(c["h"] for c in recent)
    else:
        lows = [c["l"] for i, c in enumerate(recent[1:-1]) if c["l"] < recent[i]["l"] and c["l"] < recent[i+2]["l"]]
        return min(lows) if lows else min(c["l"] for c in recent)

# ==============================================================================
# ENGINE (X-RAY SNIPER)
# ==============================================================================
class Engine:
    def __init__(self, symbol, mode):
        self.symbol = symbol
        self.mode = mode
        self.reset()
        
        if mode == "1H_BIAS":
            self.tf_poi = "1h"
            self.tf_conf = "15m"
            self.tf_trig = "1m"
        else:
            self.tf_poi = "4h"
            self.tf_conf = "1h"
            self.tf_trig = "5m"
            
        self.last_signal_time = 0
        self.cooldown = 300

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
        ltf_scan = c_trig[-36:] 
        if len(ltf_scan) < 6:
            return False
            
        for i in range(len(ltf_scan)-1, 4, -1):
            curr = ltf_scan[i]
            body = abs(curr["c"] - curr["o"])
            wick = curr["h"] - curr["l"]
            
            if wick > 0 and body / wick > 0.6:
                prev_5 = ltf_scan[i-5:i]
                highs = [max(x["o"], x["c"]) for x in prev_5]
                lows = [min(x["o"], x["c"]) for x in prev_5]
                
                # BUY SETUP
                if self.direction == "BUY" and curr["c"] > max(highs):
                    self.entry = curr["c"] - (body * 0.25)
                    sl_low = min(c["l"] for c in ltf_scan[i-5:i+1])
                    self.sl = sl_low - (sl_low * SL_BUFFER_PCT / 100)
                    self.tp = get_target(c_conf, self.direction)
                    
                    if self.tp and abs(self.tp - self.entry) / abs(self.entry - self.sl) >= MIN_RR:
                        logger.info(f"{self.symbol} [{self.mode}] {log_prefix} -> X-RAY Found!")
                        self.state = "WAIT_ENTRY"
                        self.last_signal_time = time.time()
                        self.place_limit_and_sl()
                        return True
                        
                # SELL SETUP
                elif self.direction == "SELL" and curr["c"] < min(lows):
                    self.entry = curr["c"] + (body * 0.25)
                    sl_high = max(c["h"] for c in ltf_scan[i-5:i+1])
                    self.sl = sl_high + (sl_high * SL_BUFFER_PCT / 100)
                    self.tp = get_target(c_conf, self.direction)
                    
                    if self.tp and abs(self.entry - self.tp) / abs(self.sl - self.entry) >= MIN_RR:
                        logger.info(f"{self.symbol} [{self.mode}] {log_prefix} -> X-RAY Found!")
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
            
        if self.symbol in positions:
            return
        
        c_p = klines_data[self.symbol][self.tf_poi]
        c_c = klines_data[self.symbol][self.tf_conf]
        c_t = klines_data[self.symbol][self.tf_trig]
        price = live_prices[self.symbol]
        
        if not c_p or price == 0:
            return
            
        if time.time() - self.last_signal_time < self.cooldown and self.state == "IDLE":
            return
            
        if self.setup_time and time.time() - self.setup_time > 1800:
            if self.state in ["WAIT_C1", "WAIT_C2", "WAIT_C3"]:
                self.reset()
                return

        # STATE: IDLE
        if self.state == "IDLE":
            pois = get_unmitigated_poi(c_p)
            for poi in reversed(pois):
                if is_price_in_zone(price, poi):
                    self.direction = poi[0]
                    self.active_poi = poi
                    self.state = "WAIT_C1"
                    self.setup_time = time.time()
                    return
                    
        # STATE: WAIT_C1, WAIT_C2, WAIT_C3
        elif self.state in ["WAIT_C1", "WAIT_C2", "WAIT_C3"]:
            if not self.active_poi:
                self.reset()
                return
                
            margin = (self.active_poi[2] - self.active_poi[1]) * 0.5
            if self.direction == "BUY" and price < self.active_poi[1] - margin:
                self.reset()
                return
            elif self.direction == "SELL" and price > self.active_poi[2] + margin:
                self.reset()
                return

            if len(c_c) < 1:
                return
                
            prev = c_c[-1] 
            if prev["t"] == self.last_processed_conf_t:
                return
            self.last_processed_conf_t = prev["t"]

            # C1 Rejection
            if self.state == "WAIT_C1":
                valid_buy = self.direction == "BUY" and prev["l"] <= self.active_poi[2] and prev["c"] > self.active_poi[2]
                valid_sell = self.direction == "SELL" and prev["h"] >= self.active_poi[1] and prev["c"] < self.active_poi[1]
                
                if valid_buy or valid_sell:
                    if self.check_and_trigger("C1 Rejection", c_t, c_c):
                        return
                        
                self.fc1 = prev
                self.state = "WAIT_C2"
                
            # C2 Sweep
            elif self.state == "WAIT_C2":
                valid_buy = self.direction == "BUY" and prev["l"] < self.fc1["l"] and prev["c"] > self.fc1["l"]
                valid_sell = self.direction == "SELL" and prev["h"] > self.fc1["h"] and prev["c"] < self.fc1["h"]
                
                if valid_buy or valid_sell:
                    if self.check_and_trigger("C2 Sweep", c_t, c_c):
                        return
                        
                self.fc2 = prev
                self.state = "WAIT_C3"
                
            # C3 Engulfing
            elif self.state == "WAIT_C3":
                valid_buy = self.direction == "BUY" and prev["c"] > max(self.fc2["o"], self.fc2["c"])
                valid_sell = self.direction == "SELL" and prev["c"] < min(self.fc2["o"], self.fc2["c"])
                
                if valid_buy or valid_sell:
                    if self.check_and_trigger("C3 Engulfing", c_t, c_c):
                        return
                self.reset()
                
        # STATE: WAIT_ENTRY
        elif self.state == "WAIT_ENTRY":
            hit_tp_sl_buy = self.direction == "BUY" and (price >= self.tp or price <= self.sl)
            hit_tp_sl_sell = self.direction == "SELL" and (price <= self.tp or price >= self.sl)
            
            if hit_tp_sl_buy or hit_tp_sl_sell:
                send_telegram(f"❌ {self.symbol} [{self.mode}] Batal: Harga lari ke TP/SL duluan.")
                self.cancel_pending_orders()
                self.reset()

    def place_limit_and_sl(self):
        def run():
            try:
                p = precisions.get(self.symbol)
                q_str = round_v((MARGIN_USDT * LEVERAGE) / self.entry, p["step"])
                q_float = float(q_str)
                
                if q_float < p["min_qty"] or (q_float * self.entry) < p["min_notional"]:
                    self.reset()
                    return
                    
                post_api({"symbol": self.symbol, "leverage": LEVERAGE}, "/fapi/v1/leverage")
                
                with state_lock:
                    active_signals[self.symbol] = {
                        "mode": self.mode,
                        "tp": self.tp,
                        "dir": self.direction,
                        "qty": q_float
                    }
                    
                limit_params = {
                    "symbol": self.symbol,
                    "side": self.direction,
                    "type": "LIMIT",
                    "quantity": q_str,
                    "price": round_v(self.entry, p["tick"]),
                    "timeInForce": "GTC"
                }
                res = post_api(limit_params, "/fapi/v1/order")
                
                if "orderId" in res:
                    self.pending_order_id = res["orderId"]
                    opp = "SELL" if self.direction == "BUY" else "BUY"
                    
                    sl_params = {
                        "algoType": "CONDITIONAL",
                        "symbol": self.symbol,
                        "side": opp,
                        "type": "STOP_MARKET",
                        "triggerPrice": round_v(self.sl, p["tick"]),
                        "quantity": q_str,
                        "reduceOnly": "true"
                    }
                    sl_res = post_api(sl_params, "/fapi/v1/algoOrder")
                    
                    self.pending_sl_algo_id = sl_res.get("algoId") or sl_res.get("orderId")
                    
                    msg = f"⏳ *{self.symbol}* LIMIT + SL ({self.mode})\n"
                    msg += f"📍 Dir: {self.direction}\n"
                    msg += f"💰 Entry: `{self.entry:.4f}`\n"
                    msg += f"🛑 SL: `{self.sl:.4f}`\n"
                    msg += f"🎯 TP: `{self.tp:.4f}`"
                    send_telegram(msg)
                else: 
                    with state_lock:
                        active_signals.pop(self.symbol, None)
                    self.reset()
            except Exception as e:
                logger.error(f"Place order error: {e}")
                self.reset()
                
        threading.Thread(target=run, daemon=True).start()

    def cancel_pending_orders(self):
        def run():
            if self.pending_order_id:
                post_api({"symbol": self.symbol, "orderId": self.pending_order_id}, "/fapi/v1/order", method="DELETE")
            if self.pending_sl_algo_id:
                post_api({"symbol": self.symbol, "algoId": self.pending_sl_algo_id}, "/fapi/v1/algoOrder", method="DELETE")
        threading.Thread(target=run, daemon=True).start()

# ==============================================================================
# 📱 TELEGRAM CMD & UTILS
# ==============================================================================
def telegram_cmd():
    global total_pnl, total_wins, total_losses, current_month_str
    t = os.getenv("TELEGRAM_TOKEN")
    if not t:
        return
        
    lid = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{t}/getUpdates"
            r = requests.get(url, params={"offset": lid, "timeout": 10}).json()
            
            for i in r.get("result", []):
                lid = i["update_id"] + 1
                txt = i.get("message", {}).get("text", "")
                if not txt:
                    continue
                txt = txt.strip().lower()
                
                def rep(msg):
                    send_telegram(msg)

                # /pnl
                if txt.startswith("/pnl"):
                    total_trades = total_wins + total_losses
                    wr = (total_wins / total_trades * 100) if total_trades > 0 else 0
                    mode_str = "DOUBLE (1H & 4H)" if config["ENABLE_1H"] and config["ENABLE_4H"] else "1H BIAS" if config["ENABLE_1H"] else "4H BIAS"
                    msg = f"📊 *PnL {current_month_str}*\n💰 Total: `{total_pnl:.4f} USDT`\n📈 Winrate: {wr:.1f}%\n✅ Wins: {total_wins} | ❌ Loss: {total_losses}\n⚙️ Mode: {mode_str}"
                    rep(msg)
                
                # /mode
                elif txt in ["/mode 1h", "/mode 4h", "/mode double"]:
                    config["ENABLE_1H"] = txt in ["/mode 1h", "/mode double"]
                    config["ENABLE_4H"] = txt in ["/mode 4h", "/mode double"]
                    for e in engines:
                        e.cancel_pending_orders()
                        e.reset()
                    rep(f"✅ Mode diubah ke: {txt.upper()}")

                # /status
                elif txt.startswith("/status"):
                    lines = ["📊 *Status Bot Saat Ini*"]
                    
                    pending = [e for e in engines if e.state == "WAIT_ENTRY"]
                    if pending:
                        lines.append("\n⏳ *Limit Belum Kejemput:*")
                        for e in pending:
                            lines.append(f"• {e.symbol} ({e.direction}) | Entry: `{e.entry:.4f}`")
                        
                    if positions:
                        lines.append("\n📈 *Posisi Floating Aktif:*")
                        for s, p in positions.items():
                            cur = live_prices.get(s, p['ep'])
                            pnl = (cur - p['ep']) * p['qty'] if p['side'] == "BUY" else (p['ep'] - cur) * p['qty']
                            margin = (p['qty'] * p['ep']) / LEVERAGE
                            pnl_pct = (pnl / margin) * 100 if margin > 0 else 0
                            sign = "+" if pnl > 0 else ""
                            emo = "🟩" if pnl > 0 else "🟥"
                            lines.append(f"{emo} {s} ({p['side']}) | Entry: `{p['ep']}` | PnL: `{pnl:.2f} USDT` | `{sign}{pnl_pct:.2f}%`")
                    else:
                        lines.append("\n📈 *Posisi Floating:* Tidak ada")

                    analyzing = [e for e in engines if e.state in ["WAIT_C1", "WAIT_C2", "WAIT_C3"]]
                    if analyzing:
                        lines.append("\n🔍 *Sedang Analisa (Setup Ditemukan):*")
                        for e in analyzing:
                            lines.append(f"• {e.symbol} [{e.mode}] ➔ {e.state}")

                    if not pending and not positions and not analyzing:
                        lines = ["📊 *Status Bot Saat Ini*\n\n💤 Semua koin sedang IDLE (Mencari zona)."]
                        
                    rep("\n".join(lines))

                # /close
                elif txt.startswith("/close"):
                    target = txt.replace("/close", "").strip().upper()
                    if target != "ALL" and not target.endswith("USDT"):
                        target += "USDT"
                    
                    if target == "ALL":
                        to_close = [s for s in positions]
                    else:
                        to_close = [target] if target in positions else []
                        
                    if not to_close:
                        rep("⚠️ Koin tidak ditemukan atau tidak ada posisi.")
                        continue
                    
                    for s in to_close:
                        p = positions[s]
                        pr = precisions.get(s)
                        qty_str = round_v(p["qty"], pr["step"]) if pr else str(p["qty"])
                        side = "SELL" if p["side"] == "BUY" else "BUY"
                        
                        post_api({"symbol": s, "side": side, "type": "MARKET", "quantity": qty_str, "reduceOnly": "true"}, "/fapi/v1/order")
                        post_api({"symbol": s}, "/fapi/v1/allOpenOrders", method="DELETE")
                        post_api({"symbol": s}, "/fapi/v1/algoOpenOrders", method="DELETE")
                        rep(f"🛑 {s} ditutup paksa via Market.")

                # /bep
                elif txt.startswith("/bep"):
                    target = txt.replace("/bep", "").strip().upper()
                    if target != "ALL" and not target.endswith("USDT"):
                        target += "USDT"
                    
                    if target == "ALL":
                        to_bep = [s for s in positions]
                    else:
                        to_bep = [target] if target in positions else []
                        
                    if not to_bep:
                        rep("⚠️ Koin tidak ditemukan.")
                        continue
                    
                    for s in to_bep:
                        p = positions[s]
                        pr = precisions.get(s)
                        
                        open_algo = post_api({"symbol": s}, "/fapi/v1/openAlgoOrders", method="GET")
                        if isinstance(open_algo, list):
                            for order in open_algo:
                                if order.get("type") == "STOP_MARKET" or order.get("origType") == "STOP_MARKET":
                                    post_api({"symbol": s, "algoId": order.get("algoId")}, "/fapi/v1/algoOrder", method="DELETE")
                                    
                        opp = "SELL" if p["side"] == "BUY" else "BUY"
                        qty_str = round_v(p["qty"], pr["step"])
                        
                        params = {
                            "algoType": "CONDITIONAL",
                            "symbol": s,
                            "side": opp,
                            "type": "STOP_MARKET",
                            "triggerPrice": round_v(p["ep"], pr["tick"]),
                            "quantity": qty_str,
                            "reduceOnly": "true"
                        }
                        post_api(params, "/fapi/v1/algoOrder")
                        rep(f"🛡️ {s} Stop Loss dipindah ke Entry (BEP) | TP Tetap Aman.")
                        
                # /help
                elif txt.startswith("/help"):
                    msg = "*📖 Commands:*\n"
                    msg += "/status — Status bot\n"
                    msg += "/pnl — PnL bulan ini\n"
                    msg += "/mode <1h/4h/double> — Ubah Mode\n"
                    msg += "/close <koin/all> — Tutup Posisi\n"
                    msg += "/bep <koin/all> — SL ke Entry"
                    rep(msg)
                    
        except Exception as e:
            time.sleep(5)

def load_monthly_pnl():
    global total_pnl, total_wins, total_losses, current_month_str
    current_month_str = datetime.now().strftime("%Y-%m")
    if not os.path.isfile(CSV_FILE):
        return
        
    try:
        with open(CSV_FILE, mode='r') as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) >= 6 and row[0].startswith(current_month_str):
                    pnl = float(row[3])
                    total_pnl += pnl
                    if pnl > 0:
                        total_wins += 1
                    else:
                        total_losses += 1
    except Exception as e:
        logger.error(f"Gagal load PnL: {e}")

# ==============================================================================
# ANTI MATI SURI (LISTEN KEY KEEPALIVE)
# ==============================================================================
def keep_alive_listenkey():
    while True:
        time.sleep(30 * 60) # Berjalan setiap 30 Menit
        try:
            url = BASE_URL + "/fapi/v1/listenKey"
            headers = {"X-MBX-APIKEY": API_KEY}
            requests.put(url, headers=headers, timeout=10)
            logger.info("ListenKey diperpanjang otomatis.")
        except Exception as e:
            logger.warning(f"Gagal perpanjang ListenKey: {e}")

# ==============================================================================
# WS HANDLER & STARTUP
# ==============================================================================
def on_market_msg(ws, m):
    try:
        d = json.loads(m)
        if "data" not in d:
            return
            
        k = d["data"]["k"]
        s = d["data"]["s"]
        tf = d["data"]["k"]["i"]
        
        if tf == "1m":
            live_prices[s] = float(k["c"])
            
        if k["x"]:
            candle = {
                "t": k["t"],
                "o": float(k["o"]),
                "h": float(k["h"]),
                "l": float(k["l"]),
                "c": float(k["c"])
            }
            klines_data[s][tf].append(candle)
            klines_data[s][tf] = klines_data[s][tf][-80:]
            
        for e in engines:
            if e.symbol == s:
                e.tick()
    except Exception as e:
        pass

def on_user_msg(ws, m):
    global total_pnl, total_wins, total_losses
    try:
        d = json.loads(m)
        if d.get("e") == "ORDER_TRADE_UPDATE":
            o = d["o"]
            s = d["o"]["s"]
            
            # PASANG TP OTOMATIS SAAT ENTRY LIMIT TERISI
            if o["X"] == "FILLED" and o.get("o") == "LIMIT" and s in active_signals:
                sig = active_signals[s]
                pr = precisions.get(s, {})
                opp = "SELL" if sig["dir"] == "BUY" else "BUY"
                
                params = {
                    "algoType": "CONDITIONAL",
                    "symbol": s,
                    "side": opp,
                    "type": "TAKE_PROFIT_MARKET",
                    "triggerPrice": round_v(sig["tp"], pr["tick"]),
                    "closePosition": "true"
                }
                post_api(params, "/fapi/v1/algoOrder")
                send_telegram(f"🚀 *{s}* LIMIT FILLED!\nTP dipasang otomatis di `{round_v(sig['tp'], pr['tick'])}`")

            # NOTIFIKASI SL / TP HIT
            if o["X"] == "FILLED" and float(o.get("rp", 0)) != 0:
                rp = float(o["rp"])
                total_pnl += rp
                if rp > 0:
                    total_wins += 1
                else:
                    total_losses += 1
                
                reason = "Manual Close 🔘"
                order_type = o.get("o")
                
                if order_type == "STOP_MARKET":
                    reason = "Hit Stop Loss 🛑"
                elif order_type == "TAKE_PROFIT_MARKET":
                    reason = "Hit Take Profit 🎯"
                
                mode = active_signals.get(s, {}).get("mode", "UNKNOWN")
                log_trade(s, o["S"], rp, mode)
                
                emo = "✅" if rp > 0 else "❌"
                send_telegram(f"{emo} *{s}* CLOSED!\nReason: {reason}\nPnL: `{rp:+.4f} USDT`")

        if d.get("e") == "ACCOUNT_UPDATE":
            for p in d["a"]["P"]:
                pa = float(p["pa"])
                s = p["s"]
                
                with state_lock:
                    if pa == 0:
                        positions.pop(s, None)
                        active_signals.pop(s, None)
                        
                        # Sapu bersih seluruh sisa Target & SL
                        post_api({"symbol": s}, "/fapi/v1/allOpenOrders", method="DELETE")
                        post_api({"symbol": s}, "/fapi/v1/algoOpenOrders", method="DELETE")
                        
                        for e in engines:
                            if e.symbol == s:
                                e.reset()
                    else:
                        positions[s] = {
                            "side": "BUY" if pa > 0 else "SELL",
                            "qty": abs(pa),
                            "ep": float(p["ep"])
                        }
    except Exception as e:
        logger.error(f"User ws error: {e}")

def start_ws_with_reconnect(url, on_msg):
    def run():
        while True:
            try:
                ws = websocket.WebSocketApp(url, on_message=on_msg)
                ws.run_forever(ping_interval=60, ping_timeout=30)
            except Exception as e:
                logger.warning(f"WS Disconnected: {e}")
            time.sleep(5)
    threading.Thread(target=run, daemon=True).start()

# --- MESIN IMMORTAL USER WS ---
def start_user_ws():
    # Nyalakan mesin perpanjangan nyawa 30 menit
    threading.Thread(target=keep_alive_listenkey, daemon=True).start()
    
    while True:
        try:
            # 1. Selalu minta KUNCI BARU setiap kali (re)connect
            url = f"{BASE_URL}/fapi/v1/listenKey"
            headers = {"X-MBX-APIKEY": API_KEY}
            lk = requests.post(url, headers=headers).json().get("listenKey")
            
            if lk:
                logger.info("Menyambungkan User WS (Akses Akun)...")
                ws = websocket.WebSocketApp(f"{WS_BASE}/ws/{lk}", on_message=on_user_msg)
                # Jika kena tendang 24 jam, ini akan berhenti dan masuk ke Exception
                ws.run_forever(ping_interval=60, ping_timeout=30)
        except Exception as e:
            logger.warning(f"User WS Terputus (Mungkin Reset 24 Jam): {e}")
        
        # Jika putus, istirahat 5 detik, lalu putar balik ke atas minta kunci baru!
        time.sleep(5)

def start():
    load_precisions()
    load_monthly_pnl()
    
    try:
        r = post_api({}, "/fapi/v2/positionRisk", method="GET")
        if isinstance(r, list):
            for p in r:
                amt = float(p["positionAmt"])
                s = p["symbol"]
                if amt != 0 and s in symbols:
                    positions[s] = {
                        "side": "BUY" if amt > 0 else "SELL",
                        "qty": abs(amt),
                        "ep": float(p["entryPrice"])
                    }
    except Exception as e:
        logger.error(f"Gagal tarik posisi awal: {e}")

    for s in symbols:
        for tf in ["4h", "1h", "15m", "5m", "1m"]:
            try:
                url = f"{BASE_URL}/fapi/v1/klines"
                r = requests.get(url, params={"symbol": s, "interval": tf, "limit": 80}).json()
                klines_data[s][tf] = [{"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])} for k in r[:-1]]
            except Exception as e:
                logger.error(f"Gagal tarik kline {s} {tf}: {e}")

    threading.Thread(target=telegram_cmd, daemon=True).start()
    
    streams = "/".join([f"{s.lower()}@kline_{tf}" for s in symbols for tf in ["1m", "5m", "15m", "1h", "4h"]])
    start_ws_with_reconnect(f"{WS_BASE}/stream?streams={streams}", on_market_msg)
    
    # Jalankan Mesin Immortal untuk akun
    threading.Thread(target=start_user_ws, daemon=True).start()

engines = [Engine(s, m) for s in symbols for m in ["1H_BIAS", "4H_BIAS"]]

if __name__ == "__main__":
    start()
    print("🔥 BOT v6.4 (IMMORTAL & UNCOMPRESSED EDITION) ACTIVE...")
    while True:
        time.sleep(1)
