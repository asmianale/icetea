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
# SMC LOGIC — FVG DETECTION & TARGET
# ==============================================================================

def get_unmitigated_fvg(candles, depth=40, min_size_pct=0.1):
    """
    Deteksi Fair Value Gap (FVG) yang BELUM ter-mitigasi.
    
    FVG Bullish (sinyal BUY):
      - candle[i] HIGH < candle[i+2] LOW  → ada gap ke atas
      - Gap = zona antara candle[i].high (bawah gap) dan candle[i+2].low (atas gap)
      - Belum mitigasi jika TIDAK ADA candle setelahnya yang LOW <= candle[i+2].low
        (artinya harga belum turun kembali masuk ke gap)
    
    FVG Bearish (sinyal SELL):
      - candle[i] LOW > candle[i+2] HIGH  → ada gap ke bawah
      - Gap = zona antara candle[i+2].high (atas gap) dan candle[i].low (bawah gap)
      - Belum mitigasi jika TIDAK ADA candle setelahnya yang HIGH >= candle[i+2].high
        (artinya harga belum naik kembali masuk ke gap)
    
    Returns: list of (direction, gap_bottom, gap_top)
      - BUY:  (\"BUY\",  candle[i].high,   candle[i+2].low)   → gap_bottom, gap_top
      - SELL: (\"SELL\", candle[i+2].high,  candle[i].low)     → gap_top,    gap_bottom
      
    Konsistensi: selalu (direction, zone_low, zone_high) supaya mudah dipakai
    """
    if len(candles) < depth + 3:
        return []

    fvgs = []
    start_idx = max(0, len(candles) - depth)

    for i in range(start_idx, len(candles) - 2):
        c0 = candles[i]
        # c1 = candles[i + 1]  # candle tengah (yang membuat gap)
        c2 = candles[i + 2]

        # --- Bullish FVG: gap ke atas ---
        if c0["h"] < c2["l"]:
            gap_bottom = c0["h"]
            gap_top = c2["l"]
            gap_size_pct = (gap_top - gap_bottom) / gap_bottom * 100
            if gap_size_pct >= min_size_pct:
                # Cek mitigasi: apakah ada candle SETELAH gap yang masuk zona
                mitigated = False
                for j in range(i + 3, len(candles)):
                    if candles[j]["l"] <= gap_top:  # harga turun ke zona gap
                        mitigated = True
                        break
                if not mitigated:
                    fvgs.append(("BUY", gap_bottom, gap_top))

        # --- Bearish FVG: gap ke bawah ---
        elif c0["l"] > c2["h"]:
            gap_top = c0["l"]
            gap_bottom = c2["h"]
            gap_size_pct = (gap_top - gap_bottom) / gap_bottom * 100
            if gap_size_pct >= min_size_pct:
                # Cek mitigasi: apakah ada candle SETELAH gap yang masuk zona
                mitigated = False
                for j in range(i + 3, len(candles)):
                    if candles[j]["h"] >= gap_bottom:  # harga naik ke zona gap
                        mitigated = True
                        break
                if not mitigated:
                    fvgs.append(("SELL", gap_bottom, gap_top))

    return fvgs


def is_price_in_fvg(price, fvg_tuple):
    """Cek apakah harga saat ini berada di dalam zona FVG"""
    direction, zone_low, zone_high = fvg_tuple
    return zone_low <= price <= zone_high


def get_target(candles, direction, depth=20):
    """
    Cari target TP dari swing high/low terakhir.
    - BUY:  target = swing high tertinggi dari N candle terakhir
    - SELL: target = swing low terendah dari N candle terakhir
    """
    if len(candles) < 3:
        return None

    recent = candles[-depth:] if len(candles) >= depth else candles
    
    if direction == "BUY":
        # Cari swing highs (local maxima)
        swing_highs = []
        for i in range(1, len(recent) - 1):
            if recent[i]["h"] > recent[i-1]["h"] and recent[i]["h"] > recent[i+1]["h"]:
                swing_highs.append(recent[i]["h"])
        if swing_highs:
            return max(swing_highs)
        return max(c["h"] for c in recent)
    else:
        # Cari swing lows (local minima)
        swing_lows = []
        for i in range(1, len(recent) - 1):
            if recent[i]["l"] < recent[i-1]["l"] and recent[i]["l"] < recent[i+1]["l"]:
                swing_lows.append(recent[i]["l"])
        if swing_lows:
            return min(swing_lows)
        return min(c["l"] for c in recent)


def detect_choch_or_bos(candles, direction, lookback=10):
    """
    Deteksi Change of Character (CHoCH) / Break of Structure (BOS)
    untuk konfirmasi tambahan.
    
    BUY setup: Cari break of structure ke atas (higher high setelah lower low)
    SELL setup: Cari break of structure ke bawah (lower low setelah higher high)
    """
    if len(candles) < lookback:
        return False

    recent = candles[-lookback:]
    
    if direction == "BUY":
        # Cari lower low diikuti higher high
        lowest = min(c["l"] for c in recent[:lookback//2])
        recent_highs = [c["h"] for c in recent[lookback//2:]]
        prev_high = max(c["h"] for c in recent[:lookback//2])
        if recent_highs and max(recent_highs) > prev_high:
            return True
    else:
        # Cari higher high diikuti lower low
        highest = max(c["h"] for c in recent[:lookback//2])
        recent_lows = [c["l"] for c in recent[lookback//2:]]
        prev_low = min(c["l"] for c in recent[:lookback//2])
        if recent_lows and min(recent_lows) < prev_low:
            return True

    return False


# ==============================================================================
# ENGINE CLASS — MULTI-TIMEFRAME ENTRY LOGIC
# ==============================================================================

class Engine:
    """
    State Machine:
      IDLE       → Scanning FVG di POI timeframe
      WAIT_CONF  → Harga di zona FVG, menunggu konfirmasi di confirmation TF
      WAIT_C3    → Candle 2 menunjukkan rejection, tunggu candle 3 konfirmasi
      WAIT_TRIG  → Konfirmasi ada, menunggu trigger entry di trigger TF
      WAIT_ENTRY → Limit order sudah dipasang, menunggu fill
    """

    def __init__(self, symbol, mode):
        self.symbol = symbol
        self.mode = mode
        self.reset()
        if mode == "1H_BIAS":
            self.tf_poi, self.tf_conf, self.tf_trig = "1h", "15m", "1m"
        else:
            self.tf_poi, self.tf_conf, self.tf_trig = "4h", "1h", "5m"
        self.last_signal_time = 0
        self.cooldown_seconds = 300  # 5 menit cooldown antara signal

    def reset(self):
        self.state = "IDLE"
        self.direction = None
        self.active_fvg = None  # FVG yang sedang aktif: (dir, low, high)
        self.fc2 = None  # Candle konfirmasi kedua
        self.entry = None
        self.sl = None
        self.tp = None
        self.pending_order_id = None
        self.pending_sl_id = None
        self.setup_time = None

    def tick(self):
        # Cek apakah mode aktif
        if (self.mode == "1H_BIAS" and not config["ENABLE_1H"]) or \
           (self.mode == "4H_BIAS" and not config["ENABLE_4H"]):
            if self.state != "IDLE":
                self.cancel_pending_orders()
                self.reset()
            return

        # Jangan proses jika sudah ada posisi terbuka untuk simbol ini
        if self.symbol in positions:
            return

        c_poi = klines_data[self.symbol][self.tf_poi]
        c_conf = klines_data[self.symbol][self.tf_conf]
        c_trig = klines_data[self.symbol][self.tf_trig]
        price = live_prices[self.symbol]

        if len(c_poi) < 10 or price == 0:
            return

        # Cooldown check
        if time.time() - self.last_signal_time < self.cooldown_seconds and self.state == "IDLE":
            return

        # Timeout setup yang terlalu lama (30 menit)
        if self.setup_time and time.time() - self.setup_time > 1800:
            if self.state in ["WAIT_CONF", "WAIT_C3", "WAIT_TRIG"]:
                logger.info(f"{self.symbol} [{self.mode}] Setup timeout, reset")
                self.reset()
                return

        # === STATE: IDLE — Cari FVG yang belum ter-mitigasi ===
        if self.state == "IDLE":
            fvgs = get_unmitigated_fvg(c_poi)
            if not fvgs:
                return

            # Cek apakah harga saat ini berada di salah satu zona FVG
            for fvg in reversed(fvgs):  # prioritaskan FVG terbaru
                if is_price_in_fvg(price, fvg):
                    self.direction = fvg[0]
                    self.active_fvg = fvg
                    self.state = "WAIT_CONF"
                    self.setup_time = time.time()
                    logger.info(f"{self.symbol} [{self.mode}] FVG ditemukan: {fvg[0]} "
                                f"zona [{fvg[1]:.4f} - {fvg[2]:.4f}], harga: {price}")
                    return

        # === STATE: WAIT_CONF / WAIT_C3 — Konfirmasi dari TF konfirmasi ===
        elif self.state in ["WAIT_CONF", "WAIT_C3"]:
            # Validasi: harga masih di zona FVG?
            if self.active_fvg:
                _, zone_low, zone_high = self.active_fvg
                margin = (zone_high - zone_low) * 0.5  # toleransi 50% dari ukuran gap
                if self.direction == "BUY" and price < zone_low - margin:
                    logger.info(f"{self.symbol} [{self.mode}] Harga keluar zona BUY, reset")
                    self.reset()
                    return
                elif self.direction == "SELL" and price > zone_high + margin:
                    logger.info(f"{self.symbol} [{self.mode}] Harga keluar zona SELL, reset")
                    self.reset()
                    return

            if len(c_conf) < 3:
                return

            prev_candle = c_conf[-2]  # candle yang sudah close
            curr_candle = c_conf[-1]  # candle berjalan (belum close, tapi cek yang sudah close)

            if self.state == "WAIT_CONF":
                # Konfirmasi pola 1: Engulfing / Rejection langsung
                # BUY: candle bikin lower low tapi CLOSE di atas low sebelumnya (bullish rejection)
                # SELL: candle bikin higher high tapi CLOSE di bawah high sebelumnya (bearish rejection)
                if self.direction == "BUY":
                    if prev_candle["l"] < c_conf[-3]["l"] and prev_candle["c"] > c_conf[-3]["l"]:
                        # Bullish rejection: sweep low, close di atas
                        self.state = "WAIT_TRIG"
                        logger.info(f"{self.symbol} [{self.mode}] BUY konfirmasi (rejection)")
                    elif prev_candle["l"] < c_conf[-3]["l"]:
                        # Baru sweep low, belum konfirmasi — tunggu candle berikutnya
                        self.fc2 = prev_candle
                        self.state = "WAIT_C3"
                elif self.direction == "SELL":
                    if prev_candle["h"] > c_conf[-3]["h"] and prev_candle["c"] < c_conf[-3]["h"]:
                        # Bearish rejection: sweep high, close di bawah
                        self.state = "WAIT_TRIG"
                        logger.info(f"{self.symbol} [{self.mode}] SELL konfirmasi (rejection)")
                    elif prev_candle["h"] > c_conf[-3]["h"]:
                        # Baru sweep high, belum konfirmasi
                        self.fc2 = prev_candle
                        self.state = "WAIT_C3"

            elif self.state == "WAIT_C3":
                # Tunggu candle ketiga yang mengkonfirmasi reversal
                if self.fc2 and prev_candle["t"] > self.fc2["t"]:
                    if self.direction == "BUY":
                        # Candle 3 close di atas body candle 2 (bullish)
                        fc2_body_high = max(self.fc2["o"], self.fc2["c"])
                        if prev_candle["c"] > fc2_body_high:
                            self.state = "WAIT_TRIG"
                            logger.info(f"{self.symbol} [{self.mode}] BUY konfirmasi (C3 bullish)")
                        else:
                            # Gagal konfirmasi, kembali
                            self.state = "WAIT_CONF"
                            self.fc2 = None
                    elif self.direction == "SELL":
                        fc2_body_low = min(self.fc2["o"], self.fc2["c"])
                        if prev_candle["c"] < fc2_body_low:
                            self.state = "WAIT_TRIG"
                            logger.info(f"{self.symbol} [{self.mode}] SELL konfirmasi (C3 bearish)")
                        else:
                            self.state = "WAIT_CONF"
                            self.fc2 = None

        # === STATE: WAIT_TRIG — Tunggu trigger entry di TF trigger ===
        elif self.state == "WAIT_TRIG":
            if len(c_trig) < 6:
                return

            # Gunakan candle yang sudah CLOSE (c_trig[-2])
            trigger_candle = c_trig[-2]
            body = abs(trigger_candle["c"] - trigger_candle["o"])
            wick = trigger_candle["h"] - trigger_candle["l"]

            # Trigger: candle dengan body dominan (> 60% dari total range)
            if wick > 0 and body / wick > 0.6:
                recent_5 = c_trig[-6:-1]  # 5 candle terakhir yang sudah close
                recent_bodies_high = [max(x["o"], x["c"]) for x in recent_5[:-1]]
                recent_bodies_low = [min(x["o"], x["c"]) for x in recent_5[:-1]]

                triggered = False
                if self.direction == "BUY":
                    # Trigger candle close di atas body high dari 4 candle sebelumnya
                    if recent_bodies_high and trigger_candle["c"] > max(recent_bodies_high):
                        triggered = True
                elif self.direction == "SELL":
                    # Trigger candle close di bawah body low dari 4 candle sebelumnya
                    if recent_bodies_low and trigger_candle["c"] < min(recent_bodies_low):
                        triggered = True

                if triggered:
                    # === Hitung Entry, SL, TP ===
                    self.entry = price  # gunakan harga live, bukan rata-rata

                    # SL: di luar swing low/high terakhir + buffer
                    if self.direction == "BUY":
                        # SL di bawah low terbaru dari trigger candles
                        recent_low = min(c["l"] for c in c_trig[-5:])
                        buffer = recent_low * SL_BUFFER_PCT / 100
                        self.sl = recent_low - buffer
                    else:
                        # SL di atas high terbaru dari trigger candles
                        recent_high = max(c["h"] for c in c_trig[-5:])
                        buffer = recent_high * SL_BUFFER_PCT / 100
                        self.sl = recent_high + buffer

                    # TP dari swing structure di confirmation TF
                    self.tp = get_target(c_conf, self.direction)
                    if self.tp is None:
                        self.reset()
                        return

                    # Validasi: TP harus di sisi yang benar
                    if self.direction == "BUY" and self.tp <= self.entry:
                        logger.info(f"{self.symbol} [{self.mode}] TP <= entry untuk BUY, skip")
                        self.reset()
                        return
                    if self.direction == "SELL" and self.tp >= self.entry:
                        logger.info(f"{self.symbol} [{self.mode}] TP >= entry untuk SELL, skip")
                        self.reset()
                        return

                    # Validasi: SL harus di sisi yang benar
                    if self.direction == "BUY" and self.sl >= self.entry:
                        logger.info(f"{self.symbol} [{self.mode}] SL >= entry untuk BUY, skip")
                        self.reset()
                        return
                    if self.direction == "SELL" and self.sl <= self.entry:
                        logger.info(f"{self.symbol} [{self.mode}] SL <= entry untuk SELL, skip")
                        self.reset()
                        return

                    # Risk-Reward ratio check
                    risk = abs(self.entry - self.sl)
                    reward = abs(self.tp - self.entry)
                    if risk == 0 or reward / risk < MIN_RR:
                        logger.info(f"{self.symbol} [{self.mode}] RR {reward/risk:.2f} < {MIN_RR}, skip")
                        self.reset()
                        return

                    logger.info(f"{self.symbol} [{self.mode}] TRIGGERED {self.direction} | "
                                f"Entry: {self.entry:.4f} SL: {self.sl:.4f} TP: {self.tp:.4f} "
                                f"RR: {reward/risk:.2f}")

                    self.state = "WAIT_ENTRY"
                    self.last_signal_time = time.time()
                    self.place_limit_and_sl()

        # === STATE: WAIT_ENTRY — Order sudah dipasang, pantau ===
        elif self.state == "WAIT_ENTRY":
            if self.entry is None or self.tp is None:
                self.reset()
                return

            # Cancel jika harga sudah melewati TP sebelum limit terisi
            if self.direction == "BUY" and price >= self.tp:
                send_telegram(f"❌ {self.symbol} [{self.mode}] Setup batal: harga mencapai TP sebelum limit terisi")
                self.cancel_pending_orders()
                self.reset()
            elif self.direction == "SELL" and price <= self.tp:
                send_telegram(f"❌ {self.symbol} [{self.mode}] Setup batal: harga mencapai TP sebelum limit terisi")
                self.cancel_pending_orders()
                self.reset()

            # Cancel jika SL terkena sebelum limit terisi
            elif self.direction == "BUY" and price <= self.sl:
                send_telegram(f"❌ {self.symbol} [{self.mode}] Setup batal: harga mencapai SL sebelum limit terisi")
                self.cancel_pending_orders()
                self.reset()
            elif self.direction == "SELL" and price >= self.sl:
                send_telegram(f"❌ {self.symbol} [{self.mode}] Setup batal: harga mencapai SL sebelum limit terisi")
                self.cancel_pending_orders()
                self.reset()

    # --- Order Placement ---
    def place_limit_and_sl(self):
        def run():
            try:
                pr = precisions.get(self.symbol)
                if not pr:
                    logger.error(f"Precision tidak ditemukan untuk {self.symbol}")
                    self.reset()
                    return

                # Hitung quantity
                qty_raw = (MARGIN_USDT * LEVERAGE) / self.entry
                qty = float(round_v(qty_raw, pr["step"]))

                # Validasi minimum quantity
                if qty < pr["min_qty"]:
                    send_telegram(f"⚠️ {self.symbol} qty {qty} < min {pr['min_qty']}, skip")
                    self.reset()
                    return

                # Validasi minimum notional
                notional = qty * self.entry
                if notional < pr["min_notional"]:
                    send_telegram(f"⚠️ {self.symbol} notional {notional:.2f} < min {pr['min_notional']}, skip")
                    self.reset()
                    return

                fe = round_v(self.entry, pr["tick"])
                fsl = round_v(self.sl, pr["tick"])
                ftp = round_v(self.tp, pr["tick"])
                fq = round_v(qty_raw, pr["step"])
                opp_side = "SELL" if self.direction == "BUY" else "BUY"

                # Set leverage
                post_api({"symbol": self.symbol, "leverage": LEVERAGE}, "/fapi/v1/leverage")

                # Place limit order
                params_limit = {
                    "symbol": self.symbol,
                    "side": self.direction,
                    "type": "LIMIT",
                    "quantity": fq,
                    "price": fe,
                    "timeInForce": "GTC"
                }
                res_limit = post_api(params_limit, "/fapi/v1/order")

                if "orderId" in res_limit:
                    self.pending_order_id = res_limit["orderId"]

                    # Place SL order
                    params_sl = {
                        "symbol": self.symbol,
                        "side": opp_side,
                        "type": "STOP_MARKET",
                        "stopPrice": fsl,
                        "quantity": fq,
                        "reduceOnly": "true"
                    }
                    res_sl = post_api(params_sl, "/fapi/v1/order")

                    if "orderId" in res_sl:
                        self.pending_sl_id = res_sl["orderId"]
                    else:
                        send_telegram(f"⚠️ {self.symbol} Limit OK tapi SL gagal: {res_sl.get('msg', res_sl)}")

                    with state_lock:
                        active_signals[self.symbol] = {
                            "mode": self.mode,
                            "tp": self.tp,
                            "sl": self.sl,
                            "dir": self.direction,
                            "qty": fq,
                            "entry": self.entry,
                        }

                    risk = abs(self.entry - self.sl)
                    reward = abs(self.tp - self.entry)
                    rr = reward / risk if risk > 0 else 0

                    send_telegram(
                        f"⏳ *{self.symbol}* LIMIT + SL ({self.mode})\n"
                        f"📍 Dir: {self.direction}\n"
                        f"💰 Entry: `{fe}`\n"
                        f"🛑 SL: `{fsl}`\n"
                        f"🎯 TP: `{ftp}`\n"
                        f"📊 RR: {rr:.2f}\n"
                        f"📦 Qty: {fq}"
                    )
                else:
                    send_telegram(f"⚠️ {self.symbol} Limit gagal: {res_limit.get('msg', res_limit)}")
                    self.reset()

            except Exception as e:
                logger.error(f"place_limit_and_sl error {self.symbol}: {e}")
                send_telegram(f"⚠️ Error place order {self.symbol}: {e}")
                self.reset()

        threading.Thread(target=run, daemon=True).start()

    def cancel_pending_orders(self):
        """Cancel semua pending order untuk engine ini"""
        def run():
            try:
                if self.pending_order_id:
                    res = post_api({"symbol": self.symbol, "orderId": self.pending_order_id},
                                   "/fapi/v1/order", method="DELETE")
                    logger.info(f"Cancel limit {self.symbol}: {res.get('status', res.get('msg', ''))}")
                if self.pending_sl_id:
                    res = post_api({"symbol": self.symbol, "orderId": self.pending_sl_id},
                                   "/fapi/v1/order", method="DELETE")
                    logger.info(f"Cancel SL {self.symbol}: {res.get('status', res.get('msg', ''))}")
            except Exception as e:
                logger.error(f"Cancel order error {self.symbol}: {e}")

        threading.Thread(target=run, daemon=True).start()
        self.pending_order_id = None
        self.pending_sl_id = None


# ==============================================================================
# WEBSOCKET HANDLERS
# ==============================================================================

def on_market_msg(ws, msg):
    try:
        d = json.loads(msg)
        if "data" not in d:
            return
        k = d["data"]["k"]
        s = d["data"]["s"]
        tf = k["i"]

        # Update live price dari 1m kline
        if tf == "1m":
            live_prices[s] = float(k["c"])

        # Saat candle close, tambahkan ke data
        if k["x"]:
            candle = {
                "t": k["t"],
                "o": float(k["o"]),
                "h": float(k["h"]),
                "l": float(k["l"]),
                "c": float(k["c"]),
                "x": True
            }
            klines_data[s][tf].append(candle)
            klines_data[s][tf] = klines_data[s][tf][-80:]  # Keep more history

        # Tick engines setiap kali ada data baru
        for e in engines:
            if e.symbol == s:
                try:
                    e.tick()
                except Exception as ex:
                    logger.error(f"Engine tick error {e.symbol} [{e.mode}]: {ex}")

    except Exception as e:
        logger.error(f"on_market_msg error: {e}")


def on_user_msg(ws, m):
    global total_pnl, total_wins, total_losses, current_month_str

    try:
        d = json.loads(m)

        # === LIMIT ORDER FILLED → Pasang TP ===
        if d.get("e") == "ORDER_TRADE_UPDATE":
            order = d["o"]
            symbol = order["s"]
            status = order["X"]
            order_type = order.get("ot", order.get("o", ""))

            # Limit terisi → pasang TP
            if status == "FILLED" and order_type == "LIMIT":
                if symbol in active_signals:
                    sig = active_signals[symbol]
                    pr = precisions.get(symbol, {})
                    if not pr:
                        return

                    opp = "SELL" if sig["dir"] == "BUY" else "BUY"
                    ftp = round_v(sig["tp"], pr["tick"])

                    # Pasang TP sebagai TAKE_PROFIT_MARKET
                    params_tp = {
                        "symbol": symbol,
                        "side": opp,
                        "type": "TAKE_PROFIT_MARKET",
                        "stopPrice": ftp,
                        "closePosition": "true",
                    }
                    res_tp = post_api(params_tp, "/fapi/v1/order")

                    if "orderId" in res_tp:
                        send_telegram(f"🚀 *{symbol}* LIMIT FILLED!\nTP dipasang otomatis di `{ftp}`")
                    else:
                        send_telegram(f"⚠️ {symbol} Limit filled, tapi TP gagal: {res_tp.get('msg', res_tp)}")

            # Order yang menghasilkan realized PnL
            if status == "FILLED" and float(order.get("rp", 0)) != 0:
                rp = float(order["rp"])
                symbol = order["s"]

                # Reset monthly jika bulan berubah
                now_ym = datetime.now().strftime("%Y-%m")
                if now_ym != current_month_str:
                    total_pnl, total_wins, total_losses = 0.0, 0, 0
                    current_month_str = now_ym

                total_pnl += rp
                if rp > 0:
                    total_wins += 1
                else:
                    total_losses += 1

                mode = active_signals.get(symbol, {}).get("mode", "UNKNOWN")
                log_trade(symbol, order["S"], rp, mode)

                emoji = "✅" if rp > 0 else "❌"
                send_telegram(f"{emoji} *{symbol}* Closed | PnL: `{round(rp, 4)} USDT` ({mode})")

        # === ACCOUNT UPDATE → Track posisi ===
        if d.get("e") == "ACCOUNT_UPDATE":
            for p in d["a"]["P"]:
                sym = p["s"]
                pa = float(p["pa"])
                if pa == 0:
                    # Posisi ditutup
                    with state_lock:
                        positions.pop(sym, None)
                        active_signals.pop(sym, None)
                    # Reset engine untuk symbol ini
                    for e in engines:
                        if e.symbol == sym:
                            e.reset()
                else:
                    with state_lock:
                        positions[sym] = {
                            "side": "BUY" if pa > 0 else "SELL",
                            "qty": abs(pa)
                        }

    except Exception as e:
        logger.error(f"on_user_msg error: {e}")


# ==============================================================================
# TELEGRAM COMMANDS
# ==============================================================================

def telegram_cmd():
    global total_pnl, total_wins, total_losses, current_month_str
    t = os.getenv("TELEGRAM_TOKEN")
    if not t:
        logger.warning("TELEGRAM_TOKEN tidak diset, command handler dimatikan")
        return

    lid = 0
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{t}/getUpdates",
                             params={"offset": lid, "timeout": 10}, timeout=15).json()
            if not r.get("ok"):
                time.sleep(5)
                continue

            for i in r["result"]:
                lid = i["update_id"] + 1
                msg = i.get("message", {}).get("text", "").strip().lower()
                chat_id = i.get("message", {}).get("chat", {}).get("id")
                if not chat_id:
                    continue

                def reply(text):
                    requests.post(f"https://api.telegram.org/bot{t}/sendMessage",
                                  json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)

                if msg == "/pnl":
                    wr = (total_wins / (total_wins + total_losses) * 100) if (total_wins + total_losses) > 0 else 0
                    if config["ENABLE_1H"] and config["ENABLE_4H"]:
                        st_mode = "DOUBLE (1H & 4H)"
                    elif config["ENABLE_1H"]:
                        st_mode = "HANYA 1H BIAS"
                    elif config["ENABLE_4H"]:
                        st_mode = "HANYA 4H BIAS"
                    else:
                        st_mode = "SEMUA MATI"

                    reply(
                        f"📊 *PnL Bulan Ini* ({current_month_str})\n"
                        f"💰 Total: `{round(total_pnl, 4)} USDT`\n"
                        f"📈 Winrate: {round(wr, 1)}%\n"
                        f"✅ Wins: {total_wins} | ❌ Loss: {total_losses}\n"
                        f"📌 Active: {len(positions)}\n"
                        f"⚙️ Mode: {st_mode}"
                    )

                elif msg == "/mode 1h":
                    config["ENABLE_1H"] = True
                    config["ENABLE_4H"] = False
                    for e in engines:
                        if e.mode == "4H_BIAS":
                            e.cancel_pending_orders()
                            e.reset()
                    reply("✅ Mode diubah: *HANYA 1H Bias*")

                elif msg == "/mode 4h":
                    config["ENABLE_1H"] = False
                    config["ENABLE_4H"] = True
                    for e in engines:
                        if e.mode == "1H_BIAS":
                            e.cancel_pending_orders()
                            e.reset()
                    reply("✅ Mode diubah: *HANYA 4H Bias*")

                elif msg == "/mode double":
                    config["ENABLE_1H"] = True
                    config["ENABLE_4H"] = True
                    reply("✅ Mode diubah: *DOUBLE* (1H & 4H)")

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
                    reply(
                        "*📖 Commands:*\n"
                        "/pnl — Lihat PnL bulan ini\n"
                        "/mode 1h — Hanya 1H bias\n"
                        "/mode 4h — Hanya 4H bias\n"
                        "/mode double — Kedua mode aktif\n"
                        "/status — Status engine & posisi\n"
                        "/help — Menu ini"
                    )

        except Exception as e:
            logger.error(f"Telegram cmd error: {e}")
            time.sleep(5)


# ==============================================================================
# STARTUP & WEBSOCKET
# ==============================================================================

def keep_alive_listenkey():
    while True:
        time.sleep(30 * 60)  # 30 menit (bukan 45)
        try:
            requests.put(BASE_URL + "/fapi/v1/listenKey",
                         headers={"X-MBX-APIKEY": API_KEY}, timeout=10)
            logger.info("ListenKey refreshed")
        except Exception as e:
            logger.warning(f"ListenKey refresh gagal: {e}")


def load_monthly_pnl():
    global total_pnl, total_wins, total_losses, current_month_str
    total_pnl, total_wins, total_losses = 0.0, 0, 0
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
                    elif pnl < 0:
                        total_losses += 1
    except Exception as e:
        logger.error(f"Load monthly PnL error: {e}")


def start_ws_with_reconnect(url, on_message, name="WS"):
    """Wrapper untuk WebSocket dengan auto-reconnect"""
    def run():
        while True:
            try:
                logger.info(f"{name} connecting...")
                ws = websocket.WebSocketApp(
                    url,
                    on_message=on_message,
                    on_error=lambda ws, e: logger.error(f"{name} error: {e}"),
                    on_close=lambda ws, c, m: logger.warning(f"{name} closed: {c} {m}"),
                )
                ws.run_forever(ping_interval=60, ping_timeout=30)
            except Exception as e:
                logger.error(f"{name} exception: {e}")
            logger.info(f"{name} reconnecting in 5s...")
            time.sleep(5)
    threading.Thread(target=run, daemon=True).start()


def start():
    logger.info("🔧 Loading precisions...")
    load_precisions()

    logger.info("📊 Loading monthly PnL...")
    load_monthly_pnl()

    logger.info("📥 Loading historical klines...")
    for s in symbols:
        for tf in ["4h", "1h", "15m", "5m"]:
            try:
                res = requests.get(f"{BASE_URL}/fapi/v1/klines",
                                   params={"symbol": s, "interval": tf, "limit": 80}, timeout=10).json()
                klines_data[s][tf] = [
                    {
                        "t": k[0],
                        "o": float(k[1]),
                        "h": float(k[2]),
                        "l": float(k[3]),
                        "c": float(k[4]),
                        "x": (i < len(res) - 1)
                    }
                    for i, k in enumerate(res)
                ]
                logger.info(f"  {s} {tf}: {len(klines_data[s][tf])} candles")
            except Exception as e:
                logger.error(f"  {s} {tf} gagal: {e}")

    # Telegram command handler
    threading.Thread(target=telegram_cmd, daemon=True).start()

    # Market data WebSocket
    streams = "/".join([f"{s.lower()}@kline_{tf}" for s in symbols for tf in ["1m", "5m", "15m", "1h", "4h"]])
    start_ws_with_reconnect(f"{WS_BASE}/stream?streams={streams}", on_market_msg, "MarketWS")

    # User data WebSocket
    try:
        lk_resp = requests.post(BASE_URL + "/fapi/v1/listenKey",
                                headers={"X-MBX-APIKEY": API_KEY}, timeout=10).json()
        lk = lk_resp.get("listenKey")
        if lk:
            threading.Thread(target=keep_alive_listenkey, daemon=True).start()
            start_ws_with_reconnect(f"{WS_BASE}/ws/{lk}", on_user_msg, "UserWS")
        else:
            logger.error(f"ListenKey gagal: {lk_resp}")
    except Exception as e:
        logger.error(f"ListenKey error: {e}")

    logger.info("🔥 Bot started successfully!")


# ==============================================================================
# MAIN
# ==============================================================================

engines = [Engine(s, m) for s in symbols for m in ["1H_BIAS", "4H_BIAS"]]

if __name__ == "__main__":
    start()
    print("🔥 BOT v5.0 (SMC Enhanced) ACTIVE...")
    while True:
        time.sleep(1)
