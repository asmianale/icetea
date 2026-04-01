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
                requests.post(f"https://api.telegram.org/bot{t}/sendMessage",
                              json={"chat_id": c, "text": msg, "parse_mode": "Markdown"}, timeout=10)
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
    """
    Deteksi FVG dan Order Block (OB) yang BELUM ter-mitigasi.
    Returns: list of (direction, zone_bottom, zone_top, type)
    """
    if len(candles) < depth + 3: return []
    pois = []
    start_idx = max(0, len(candles) - depth)

    for i in range(start_idx, len(candles) - 2):
        c0, c1, c2 = candles[i], candles[i+1], candles[i+2]

        # --- Bullish FVG ---
        if c0["h"] < c2["l"]:
            if (c2["l"] - c0["h"]) / c0["h"] * 100 >= min_size_pct:
                if not any(candles[j]["l"] <= c2["l"] for j in range(i+3, len(candles))):
                    pois.append(("BUY", c0["h"], c2["l"], "FVG"))

        # --- Bearish FVG ---
        elif c0["l"] > c2["h"]:
            if (c0["l"] - c2["h"]) / c2["h"] * 100 >= min_size_pct:
                if not any(candles[j]["h"] >= c2["h"] for j in range(i+3, len(candles))):
                    pois.append(("SELL", c2["h"], c0["l"], "FVG"))

        # --- Bullish Order Block ---
        if c0["c"] < c0["o"] and c1["c"] > c1["o"] and (c1["c"] - c1["o"]) > abs(c0["c"] - c0["o"]) * 1.5:
            if not any(candles[j]["l"] <= c0["h"] for j in range(i+2, len(candles))):
                pois.append(("BUY", c0["l"], c0["h"], "OB"))

        # --- Bearish Order Block ---
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
# ENGINE CLASS
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
        self.fc2 = None
        self.entry = None
        self.sl = None
        self.tp = None
        self.pending_order_id = None
        self.pending_sl_id = None
        self.setup_time = None

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
            if self.state in ["WAIT_CONF", "WAIT_C3", "WAIT_TRIG"]:
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
                    self.state = "WAIT_CONF"
                    self.setup_time = time.time()
                    logger.info(f"{self.symbol} [{self.mode}] {poi[3]} ditemukan: {poi[0]} zona [{poi[1]:.4f} - {poi[2]:.4f}], harga: {price}")
                    return

        # === STATE: WAIT_CONF / WAIT_C3 ===
        elif self.state in ["WAIT_CONF", "WAIT_C3"]:
            if not self.active_poi:
                self.reset(); return
            
            _, zone_low, zone_high, _ = self.active_poi
            margin = (zone_high - zone_low) * 0.5
            
            if self.direction == "BUY" and price < zone_low - margin:
                self.reset(); return
            elif self.direction == "SELL" and price > zone_high + margin:
                self.reset(); return

            if len(c_conf) < 3: return

            prev_candle = c_conf[-2]
            prev2_candle = c_conf[-3]

            if self.state == "WAIT_CONF":
                # --- 1. JALUR CEPAT: C1 REJECTION ---
                if self.direction == "BUY" and prev_candle["l"] <= zone_high and prev_candle["c"] > zone_high:
                    self.state = "WAIT_TRIG"
                    logger.info(f"{self.symbol} [{self.mode}] C1 Reject (Wick di zona, Close di atas)! Lanjut ke CISD LTF.")
                    return
                elif self.direction == "SELL" and prev_candle["h"] >= zone_low and prev_candle["c"] < zone_low:
                    self.state = "WAIT_TRIG"
                    logger.info(f"{self.symbol} [{self.mode}] C1 Reject (Wick di zona, Close di bawah)! Lanjut ke CISD LTF.")
                    return

                # --- 2. JALUR STANDAR: C2 SWEEP ---
                if self.direction == "BUY":
                    if prev_candle["l"] < prev2_candle["l"] and prev_candle["c"] > prev2_candle["l"]:
                        self.state = "WAIT_TRIG"
                        logger.info(f"{self.symbol} [{self.mode}] C2 Sweep & Reject! Lanjut ke CISD LTF.")
                    elif prev_candle["l"] < prev2_candle["l"]:
                        self.fc2 = prev_candle
                        self.state = "WAIT_C3"
                elif self.direction == "SELL":
                    if prev_candle["h"] > prev2_candle["h"] and prev_candle["c"] < prev2_candle["h"]:
                        self.state = "WAIT_TRIG"
                        logger.info(f"{self.symbol} [{self.mode}] C2 Sweep & Reject! Lanjut ke CISD LTF.")
                    elif prev_candle["h"] > prev2_candle["h"]:
                        self.fc2 = prev_candle
                        self.state = "WAIT_C3"

            elif self.state == "WAIT_C3":
                # --- 3. JALUR FALLBACK: C3 ENGULFING ---
                if self.fc2 and prev_candle["t"] > self.fc2["t"]:
                    if self.direction == "BUY":
                        fc2_body_high = max(self.fc2["o"], self.fc2["c"])
                        if prev_candle["c"] > fc2_body_high:
                            self.state = "WAIT_TRIG"
                            logger.info(f"{self.symbol} [{self.mode}] C3 Engulfing Bullish! Lanjut ke CISD LTF.")
                        else:
                            self.state = "WAIT_CONF"
                            self.fc2 = None
                    elif self.direction == "SELL":
                        fc2_body_low = min(self.fc2["o"], self.fc2["c"])
                        if prev_candle["c"] < fc2_body_low:
                            self.state = "WAIT_TRIG"
                            logger.info(f"{self.symbol} [{self.mode}] C3 Engulfing Bearish! Lanjut ke CISD LTF.")
                        else:
                            self.state = "WAIT_CONF"
                            self.fc2 = None

        # === STATE: WAIT_TRIG (CISD + BOS + 25% PULLBACK ENTRY) ===
        elif self.state == "WAIT_TRIG":
            if len(c_trig) < 6: return

            trigger_candle = c_trig[-2]
            body = abs(trigger_candle["c"] - trigger_candle["o"])
            wick = trigger_candle["h"] - trigger_candle["l"]

            # Syarat 1: Displacement (Momentum Dominan > 60%)
            if wick > 0 and body / wick > 0.6:
                recent_5 = c_trig[-6:-1]
                recent_bodies_high = [max(x["o"], x["c"]) for x in recent_5[:-1]]
                recent_bodies_low = [min(x["o"], x["c"]) for x in recent_5[:-1]]

                triggered = False
                # Syarat 2: Micro-BOS (Valid Body Break)
                if self.direction == "BUY" and recent_bodies_high and trigger_candle["c"] > max(recent_bodies_high):
                    triggered = True
                elif self.direction == "SELL" and recent_bodies_low and trigger_candle["c"] < min(recent_bodies_low):
                    triggered = True

                if triggered:
                    # Menghitung ukuran murni dari badan (body) candle pembawa CISD/BOS
                    body_size = abs(trigger_candle["c"] - trigger_candle["o"])

                    # ENTRY: Pullback 25% dari titik penutupan (Close)
                    if self.direction == "BUY":
                        self.entry = trigger_candle["c"] - (body_size * 0.25)
                        recent_low = min(c["l"] for c in c_trig[-5:])
                        self.sl = recent_low - (recent_low * SL_BUFFER_PCT / 100)
                    else: # SELL
                        self.entry = trigger_candle["c"] + (body_size * 0.25)
                        recent_high = max(c["h"] for c in c_trig[-5:])
                        self.sl = recent_high + (recent_high * SL_BUFFER_PCT / 100)

                    self.tp = get_target(c_conf, self.direction)
                    if self.tp is None: self.reset(); return

                    if self.direction == "BUY" and (self.tp <= self.entry or self.sl >= self.entry): self.reset(); return
                    if self.direction == "SELL" and (self.tp >= self.entry or self.sl <= self.entry): self.reset(); return

                    risk = abs(self.entry - self.sl)
                    reward = abs(self.tp - self.entry)
                    if risk == 0 or reward / risk < MIN_RR: self.reset(); return

                    logger.info(f"{self.symbol} [{self.mode}] CISD+BOS TRIGGERED! | Pullback Entry: {self.entry:.4f} SL: {self.sl:.4f} TP: {self.tp:.4f} RR: {reward/risk:.2f}")
                    self.state = "WAIT_ENTRY"
                    self.last_signal_time = time.time()
                    self.place_limit_and_sl()

        # === STATE: WAIT_ENTRY ===
        elif self.state == "WAIT_ENTRY":
            if self.entry is None or self.tp is None:
                self.reset(); return

            if (self.direction == "BUY" and (price >= self.tp or price <= self.sl)) or \
               (self.direction == "SELL" and (price <= self.tp or price >= self.sl)):
                send_telegram(f"❌ {self.symbol} [{self.mode}] Setup batal: Harga menyentuh target target/SL sebelum limit terisi.")
                self.cancel_pending_orders()
                self.reset()

    def place_limit_and_sl(self):
        def run():
            try:
                pr = precisions.get(self.symbol)
                if not pr: self.reset(); return

                qty_raw = (MARGIN_USDT * LEVERAGE) / self.entry
                qty = float(round_v(qty_raw, pr["step"]))

                if qty < pr["min_qty"] or (qty * self.entry) < pr["min_notional"]:
                    self.reset(); return

                fe = round_v(self.entry, pr["tick"])
                fsl = round_v(self.sl, pr["tick"])
                ftp = round_v(self.tp, pr["tick"])
                fq = round_v(qty_raw, pr["step"])
                opp_side = "SELL" if self.direction == "BUY" else "BUY"

                post_api({"symbol": self.symbol, "leverage": LEVERAGE}, "/fapi/v1/leverage")

                res_limit = post_api({"symbol": self.symbol, "side": self.direction, "type": "LIMIT", "quantity": fq, "price": fe, "timeInForce": "GTC"}, "/fapi/v1/order")
                
                if "orderId" in res_limit:
                    self.pending_order_id = res_limit["orderId"]
                    
                    # Protect account immediately with reduceOnly Stop Loss
                    res_sl = post_api({"symbol": self.symbol, "side": opp_side, "type": "STOP_MARKET", "stopPrice": fsl, "quantity": fq, "reduceOnly": "true"}, "/fapi/v1/order")
                    
                    if "orderId" in res_sl:
                        self.pending_sl_id = res_sl["orderId"]
                    else:
                        send_telegram(f"⚠️ {self.symbol} Limit OK tapi SL gagal: {res_sl.get('msg', res_sl)}")

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
                if self.pending_sl_id:
                    post_api({"symbol": self.symbol, "orderId": self.pending_sl_id}, "/fapi/v1/order", method="DELETE")
            except Exception as e: pass
        threading.Thread(target=run, daemon=True).start()
        self.pending_order_id = None
        self.pending_sl_id = None


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
            
            # Fire TP immediately when Limit fills
            if stat == "FILLED" and o.get("ot") == "LIMIT" and s in active_signals:
                sig, pr = active_signals[s], precisions.get(s, {})
                opp = "SELL" if sig["dir"] == "BUY" else "BUY"
                post_api({"symbol": s, "side": opp, "type": "TAKE_PROFIT_MARKET", "stopPrice": round_v(sig["tp"], pr["tick"]), "closePosition": "true"}, "/fapi/v1/algoOrder")
                send_telegram(f"🚀 *{s}* LIMIT FILLED!\nTP dipasang otomatis di `{round_v(sig['tp'], pr['tick'])}`")

            # Track PnL
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
                        positions[sym] = {"side": "BUY" if pa > 0 else "SELL", "qty": abs(pa)}
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
                msg = i.get("message", {}).get("text", "").strip().lower()
                chat_id = i.get("message", {}).get("chat", {}).get("id")
                if not chat_id: continue

                def reply(text): requests.post(f"https://api.telegram.org/bot{t}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)

                if msg == "/pnl":
                    wr = (total_wins / (total_wins + total_losses) * 100) if (total_wins + total_losses) > 0 else 0
                    mode_str = "DOUBLE (1H & 4H)" if config["ENABLE_1H"] and config["ENABLE_4H"] else "1H BIAS" if config["ENABLE_1H"] else "4H BIAS"
                    reply(f"📊 *PnL Bulan Ini* ({current_month_str})\n💰 Total: `{round(total_pnl, 4)} USDT`\n📈 Winrate: {round(wr, 1)}%\n✅ Wins: {total_wins} | ❌ Loss: {total_losses}\n📌 Active: {len(positions)}\n⚙️ Mode: {mode_str}")
                elif msg in ["/mode 1h", "/mode 4h", "/mode double"]:
                    config["ENABLE_1H"] = msg in ["/mode 1h", "/mode double"]
                    config["ENABLE_4H"] = msg in ["/mode 4h", "/mode double"]
                    for e in engines: e.cancel_pending_orders(); e.reset()
                    reply(f"✅ Mode diubah ke: {msg.upper()}")
                elif msg == "/status":
                    lines = [f"📊 *Bot Status*\n"]
                    for e in engines:
                        if e.state != "IDLE":
                            lines.append(f"• {e.symbol} [{e.mode}]: {e.state} ({e.direction})")
                    if len(positions) > 0:
                        lines.append(f"\n📌 *Posisi Aktif:*")
                        for sym, pos in positions.items():
                            lines.append(f"• {sym}: {pos['side']} qty={pos['qty']}")
                    if len(lines) == 1:
                        lines.append("Semua engine IDLE, tidak ada posisi aktif")
                    reply("\n".join(lines))
                elif msg == "/help":
                    reply("*📖 Commands:*\n/pnl — Lihat PnL bulan ini\n/mode 1h — Hanya 1H bias\n/mode 4h — Hanya 4H bias\n/mode double — Kedua mode aktif\n/status — Status engine & posisi\n/help — Menu ini")
        except: time.sleep(5)

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
    for s in symbols:
        for tf in ["4h", "1h", "15m", "5m"]:
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
    print("🔥 BOT v5.3 (THE ULTIMATE PULLBACK SNIPER) ACTIVE...")
    while True: time.sleep(1)
