import os, time, json, hmac, hashlib, requests, websocket, threading, math, csv, urllib.parse, logging
from collections import deque
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SMC_Bot")

# --- CONFIG ---
API_KEY    = os.getenv("BINANCE_API_KEY")
SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")
MODE       = os.getenv("MODE", "TESTNET")
BASE_URL   = "https://testnet.binancefuture.com" if MODE == "TESTNET" else "https://fapi.binance.com"
WS_BASE    = "wss://stream.binancefuture.com"    if MODE == "TESTNET" else "wss://fstream.binance.com"
MARGIN_USDT   = float(os.getenv("MARGIN_USDT",   1.0))
LEVERAGE      = int(os.getenv("LEVERAGE",         10))
MIN_RR        = float(os.getenv("MIN_RR",         1.5))
SL_BUFFER_PCT = float(os.getenv("SL_BUFFER_PCT",  0.05))

symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "SUIUSDT", "AVAXUSDT", "BNBUSDT", "TRXUSDT"]

MAX_CANDLES  = 80
MAX_IGNORED  = 100
SWEEP_LOOKBACK  = 40   # Cari sweep max 40 candle ke belakang
SWING_LOOKBACK  = 30   # Cari swing sebelum sweep max 30 candle
BOS_STALE_LIMIT = 8    # BOS lebih dari 8 candle lalu dianggap basi
MIN_BODY_PCT    = 0.02  # Body BOS minimal 0.02% agar ada momentum
WICK_RATIO      = 1.5   # Wick sweep minimal 1.5x body

# --- GLOBAL STATE ---
klines_data = {s: {tf: deque(maxlen=MAX_CANDLES) for tf in ["1m","5m","15m","1h","4h"]} for s in symbols}
live_prices = {s: 0.0 for s in symbols}
config      = {"ENABLE_1H": True, "ENABLE_4H": True}
positions   = {}
active_signals = {}
CSV_FILE    = "riwayat_trading.csv"

total_pnl    = 0.0
total_wins   = 0
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
                f.write(f"{key}={value}\n"); found = True
            else:
                f.write(line)
        if not found:
            f.write(f"{key}={value}\n")

# --- UTILS ---
def sync_time():
    try:
        r = requests.get(BASE_URL + "/fapi/v1/time", timeout=5).json()
        return r["serverTime"] - int(time.time() * 1000)
    except:
        return 0

time_offset = sync_time()

def ts():
    return int(time.time() * 1000 + time_offset)

def send_telegram(msg):
    def run():
        token   = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if token and chat_id:
            try:
                res = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                    timeout=10
                )
                if res.status_code != 200:
                    logger.warning(f"[TELEGRAM] {res.text}")
            except Exception as e:
                logger.warning(f"[TELEGRAM KONEKSI] {e}")
    threading.Thread(target=run, daemon=True).start()

precisions = {}

def load_precisions():
    try:
        info = requests.get(BASE_URL + "/fapi/v1/exchangeInfo", timeout=10).json()
        for s in info["symbols"]:
            if s["symbol"] in symbols:
                filters      = {f["filterType"]: f for f in s["filters"]}
                tick_size    = float(filters["PRICE_FILTER"]["tickSize"])
                step_size    = float(filters["LOT_SIZE"]["stepSize"])
                min_qty      = float(filters["LOT_SIZE"]["minQty"])
                min_notional = float(filters.get("MIN_NOTIONAL", {}).get("notional", 5))
                precisions[s["symbol"]] = {
                    "tick": max(0, int(round(-math.log10(tick_size)))),
                    "step": max(0, int(round(-math.log10(step_size)))),
                    "min_qty": min_qty,
                    "min_notional": min_notional
                }
    except Exception as e:
        logger.error(f"load_precisions error: {e}")

def round_v(v, p):
    return f"{round(v, p):.{p}f}" if p > 0 else str(int(round(v)))

def log_trade(symbol, side, pnl, mode):
    try:
        exists = os.path.isfile(CSV_FILE)
        with open(CSV_FILE, "a", newline="") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["Waktu","Simbol","Posisi","PnL (USDT)","Mode","Status"])
            w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        symbol, side, round(pnl, 4), mode,
                        "WIN" if pnl > 0 else "LOSS"])
    except:
        pass

def post_api(params, endpoint, method="POST"):
    params["timestamp"] = ts()
    qs  = urllib.parse.urlencode(params)
    sig = hmac.new(SECRET_KEY.encode(), qs.encode(), hashlib.sha256).hexdigest()
    hdr = {"X-MBX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    pl  = f"{qs}&signature={sig}"
    url = f"{BASE_URL}{endpoint}"
    try:
        if method == "POST":   r = requests.post(url, headers=hdr, data=pl, timeout=10)
        elif method == "DELETE": r = requests.delete(f"{url}?{pl}", headers=hdr, timeout=10)
        elif method == "GET":  r = requests.get(f"{url}?{pl}", headers=hdr, timeout=10)
        else: return {"error": "Unknown method"}
        return r.json()
    except Exception as e:
        return {"error": str(e)}

# ==============================================================================
# SMC HELPERS
# ==============================================================================
def get_unmitigated_poi(candles, depth=20, min_size_pct=0.1):
    if len(candles) < depth + 4: return []
    pois = []
    start = max(0, len(candles) - depth)
    for i in range(start, len(candles) - 3):
        c0, c1, c2 = candles[i], candles[i+1], candles[i+2]
        if c0["h"] < c2["l"] and c1["c"] > c1["o"]:
            if (c2["l"] - c0["h"]) / c0["h"] * 100 >= min_size_pct:
                fvg_b, fvg_t = c0["h"], c2["l"]
                valid = all(candles[j]["l"] > fvg_t for j in range(i+3, len(candles)-1))
                if valid:
                    pois.append({"dir":"BUY","fvg_b":fvg_b,"fvg_t":fvg_t,
                                 "ob_b":c0["l"],"ob_t":c0["h"],"t":c0["t"]})
        elif c0["l"] > c2["h"] and c1["c"] < c1["o"]:
            if (c0["l"] - c2["h"]) / c2["h"] * 100 >= min_size_pct:
                fvg_t, fvg_b = c0["l"], c2["h"]
                valid = all(candles[j]["h"] < fvg_b for j in range(i+3, len(candles)-1))
                if valid:
                    pois.append({"dir":"SELL","fvg_b":fvg_b,"fvg_t":fvg_t,
                                 "ob_b":c0["l"],"ob_t":c0["h"],"t":c0["t"]})
    return pois

def get_target(candles, direction, depth=20):
    recent = candles[-depth:]
    if len(recent) < 3: return None
    if direction == "BUY":
        highs = [c["h"] for i, c in enumerate(recent[1:-1])
                 if c["h"] > recent[i]["h"] and c["h"] > recent[i+2]["h"]]
        return highs[-1] if highs else None
    else:
        lows = [c["l"] for i, c in enumerate(recent[1:-1])
                if c["l"] < recent[i]["l"] and c["l"] < recent[i+2]["l"]]
        return lows[-1] if lows else None

# ==============================================================================
# ENGINE (V9.3 — SMC LOGIC FIXED)
# ==============================================================================
class Engine:
    def __init__(self, symbol, mode):
        self.symbol = symbol
        self.mode   = mode
        self.ignored_pois = []
        self.lock   = threading.Lock()
        self.reset()
        if mode == "1H_BIAS": self.tf_poi, self.tf_conf, self.tf_trig = "1h",  "15m", "1m"
        else:                  self.tf_poi, self.tf_conf, self.tf_trig = "4h",  "1h",  "5m"
        self.last_signal_time = 0
        self.cooldown = 300

    def reset(self):
        self.state               = "IDLE"
        self.direction           = None
        self.active_poi          = None
        self.active_zone         = None
        self.fc1                 = None
        self.fc2                 = None
        self.entry               = None
        self.sl                  = None
        self.tp                  = None
        self.pending_order_id    = None
        self.pending_sl_algo_id  = None
        self.setup_time          = None
        self.last_processed_conf_t = 0

    def safe_reset_from_outside(self):
        """Dipanggil dari thread luar — acquire self.lock sendiri."""
        with self.lock:
            if self.active_poi:
                self._append_ignored(self.active_poi["t"])
            self.reset()

    def _append_ignored(self, poi_t):
        self.ignored_pois.append(poi_t)
        self.ignored_pois = self.ignored_pois[-MAX_IGNORED:]

    # ==========================================================================
    # CHECK & TRIGGER — SMC V9.3 (FIXED)
    # ==========================================================================
    def check_and_trigger(self, log_prefix, c_trig, c_conf, scan_depth):
        """
        Alur SMC yang benar:
        1. Sweep / Liquidity Grab  → candle dengan wick panjang MASUK ke zona POI
        2. Swing High/Low terkini  → level struktur SEBELUM sweep (target BOS)
        3. BOS / ChoCh             → candle bullish/bearish yang CLOSE tembus swing
        4. LTF FVG / OB            → dicari MUNDUR dari BOS ke sweep (paling fresh)
        5. Entry / SL / TP         → kalkulasi RR
        """
        ltf = c_trig[-MAX_CANDLES:]
        n   = len(ltf)
        if n < 15: return False
        poi = self.active_poi

        # ------------------------------------------------------------------
        # ████  BUY  ████
        # ------------------------------------------------------------------
        if self.direction == "BUY":

            # ── STEP 1: Sweep ──────────────────────────────────────────────
            # Cari candle yang low-nya masuk zona POI (ob_b ≤ low ≤ fvg_t)
            # dengan wick bawah ≥ WICK_RATIO × body → tanda rejection kuat.
            # Dicari dari candle terbaru ke belakang (SWEEP_LOOKBACK candle).
            sweep_idx, sweep_c = None, None
            for i in range(n - 1, max(n - SWEEP_LOOKBACK, 1), -1):
                c    = ltf[i]
                body = abs(c["c"] - c["o"])
                wick = min(c["o"], c["c"]) - c["l"]   # wick bawah

                in_zone  = poi["ob_b"] <= c["l"] <= poi["fvg_t"]
                rejected = (wick >= body * WICK_RATIO) or \
                           (c["c"] > c["o"] and wick > 0)   # bullish + ada wick

                if in_zone and rejected:
                    sweep_idx, sweep_c = i, c
                    break

            if sweep_idx is None:
                return False

            # ── STEP 2: Swing High sebelum sweep ───────────────────────────
            # Cari swing high terakhir dalam SWING_LOOKBACK candle sebelum sweep.
            # Ini adalah level yang akan "dibreak" oleh BOS.
            s_start = max(0, sweep_idx - SWING_LOOKBACK)
            swing_highs = [
                ltf[i] for i in range(s_start + 1, sweep_idx)
                if ltf[i]["h"] > ltf[i-1]["h"] and ltf[i]["h"] > ltf[i+1]["h"]
            ]
            if not swing_highs:
                return False
            last_swing_high = swing_highs[-1]

            # ── STEP 3: BOS / ChoCh ────────────────────────────────────────
            # Candle setelah sweep yang:
            #   a. Close > swing high (tembus struktur)
            #   b. Bullish (close > open)
            #   c. Body minimal MIN_BODY_PCT% → ada momentum, bukan doji
            #   d. Tidak basi (dalam BOS_STALE_LIMIT candle terakhir)
            choch_idx = -1
            for i in range(sweep_idx + 1, n):
                c        = ltf[i]
                body_pct = (c["c"] - c["o"]) / c["o"] * 100 if c["o"] > 0 else 0
                if c["c"] > last_swing_high["h"] and c["c"] > c["o"] and body_pct >= MIN_BODY_PCT:
                    choch_idx = i
                    break

            if choch_idx == -1:
                return False
            if choch_idx < n - BOS_STALE_LIMIT:
                return False    # BOS sudah terlalu lama, skip

            # ── STEP 4: LTF FVG Bullish ────────────────────────────────────
            # Cari FVG dari choch MUNDUR ke sweep (kanan → kiri).
            # Ini memastikan FVG yang diambil adalah yang paling fresh/dekat BOS.
            # FVG Bullish: gap antara high[i] dan low[i+2] (c0.h < c2.l)
            ltf_fvg_b, ltf_fvg_t = None, None
            for i in range(choch_idx - 2, sweep_idx - 1, -1):
                if i < 0 or i + 2 >= n: continue
                c0, c2 = ltf[i], ltf[i+2]
                if c0["h"] < c2["l"]:
                    ltf_fvg_b = c0["h"]   # Batas bawah FVG
                    ltf_fvg_t = c2["l"]   # Batas atas FVG = entry (price pullback ke sini)
                    break

            # Fallback: Order Block — candle bearish terakhir sebelum impulse BOS
            # (Tidak ada FVG → harga bergerak terlalu kuat tanpa gap)
            if ltf_fvg_t is None:
                for i in range(choch_idx - 1, sweep_idx - 1, -1):
                    if i < 0: continue
                    c = ltf[i]
                    if c["c"] < c["o"]:    # candle merah = OB bullish
                        ltf_fvg_t = c["h"]
                        ltf_fvg_b = c["l"]
                        break

            # Tidak ada FVG maupun OB → sinyal tidak valid, tolak
            if ltf_fvg_t is None:
                return False

            # ── STEP 5: Entry / SL / TP / RR ──────────────────────────────
            self.entry = ltf_fvg_t                                          # limit order di batas atas FVG
            self.sl    = sweep_c["l"] - (sweep_c["l"] * SL_BUFFER_PCT / 100)  # di bawah sweep low

            if self.sl >= self.entry:
                return False

            # TP = swing high terdekat di konfirmasi TF
            # Jika tidak ada → tolak (tidak pakai arbitrary 2x RR fallback)
            self.tp = get_target(c_conf, self.direction)
            if not self.tp or self.tp <= self.entry:
                return False

            risk   = self.entry - self.sl
            reward = self.tp    - self.entry
            if risk > 0 and reward / risk >= MIN_RR:
                self.state            = "PLACING_ORDER"
                self.last_signal_time = time.time()
                self.place_limit_and_sl()
                return True

        # ------------------------------------------------------------------
        # ████  SELL  ████
        # ------------------------------------------------------------------
        elif self.direction == "SELL":

            # ── STEP 1: Sweep ──────────────────────────────────────────────
            # High candle masuk zona POI (fvg_b ≤ high ≤ ob_t)
            # dengan wick atas ≥ WICK_RATIO × body.
            sweep_idx, sweep_c = None, None
            for i in range(n - 1, max(n - SWEEP_LOOKBACK, 1), -1):
                c    = ltf[i]
                body = abs(c["c"] - c["o"])
                wick = c["h"] - max(c["o"], c["c"])    # wick atas

                in_zone  = poi["fvg_b"] <= c["h"] <= poi["ob_t"]
                rejected = (wick >= body * WICK_RATIO) or \
                           (c["c"] < c["o"] and wick > 0)   # bearish + ada wick

                if in_zone and rejected:
                    sweep_idx, sweep_c = i, c
                    break

            if sweep_idx is None:
                return False

            # ── STEP 2: Swing Low sebelum sweep ────────────────────────────
            s_start = max(0, sweep_idx - SWING_LOOKBACK)
            swing_lows = [
                ltf[i] for i in range(s_start + 1, sweep_idx)
                if ltf[i]["l"] < ltf[i-1]["l"] and ltf[i]["l"] < ltf[i+1]["l"]
            ]
            if not swing_lows:
                return False
            last_swing_low = swing_lows[-1]

            # ── STEP 3: BOS / ChoCh ────────────────────────────────────────
            # Candle setelah sweep yang:
            #   a. Close < swing low (tembus struktur ke bawah)
            #   b. Bearish (close < open)
            #   c. Body minimal MIN_BODY_PCT%
            #   d. Dalam BOS_STALE_LIMIT candle terakhir
            choch_idx = -1
            for i in range(sweep_idx + 1, n):
                c        = ltf[i]
                body_pct = (c["o"] - c["c"]) / c["o"] * 100 if c["o"] > 0 else 0
                if c["c"] < last_swing_low["l"] and c["c"] < c["o"] and body_pct >= MIN_BODY_PCT:
                    choch_idx = i
                    break

            if choch_idx == -1:
                return False
            if choch_idx < n - BOS_STALE_LIMIT:
                return False

            # ── STEP 4: LTF FVG Bearish ────────────────────────────────────
            # FVG Bearish: gap antara low[i] dan high[i+2] (c0.l > c2.h)
            # Dicari MUNDUR dari choch ke sweep.
            ltf_fvg_b, ltf_fvg_t = None, None
            for i in range(choch_idx - 2, sweep_idx - 1, -1):
                if i < 0 or i + 2 >= n: continue
                c0, c2 = ltf[i], ltf[i+2]
                if c0["l"] > c2["h"]:
                    ltf_fvg_t = c0["l"]   # Batas atas FVG
                    ltf_fvg_b = c2["h"]   # Batas bawah FVG = entry (price pullback ke sini)
                    break

            # Fallback: OB Bearish — candle bullish terakhir sebelum impulse BOS
            if ltf_fvg_b is None:
                for i in range(choch_idx - 1, sweep_idx - 1, -1):
                    if i < 0: continue
                    c = ltf[i]
                    if c["c"] > c["o"]:    # candle hijau = OB bearish
                        ltf_fvg_b = c["l"]
                        ltf_fvg_t = c["h"]
                        break

            if ltf_fvg_b is None:
                return False

            # ── STEP 5: Entry / SL / TP / RR ──────────────────────────────
            self.entry = ltf_fvg_b                                          # limit sell di batas bawah FVG
            self.sl    = sweep_c["h"] + (sweep_c["h"] * SL_BUFFER_PCT / 100)  # di atas sweep high

            if self.sl <= self.entry:
                return False

            self.tp = get_target(c_conf, self.direction)
            if not self.tp or self.tp >= self.entry:
                return False

            risk   = self.sl    - self.entry
            reward = self.entry - self.tp
            if risk > 0 and reward / risk >= MIN_RR:
                self.state            = "PLACING_ORDER"
                self.last_signal_time = time.time()
                self.place_limit_and_sl()
                return True

        return False

    # ==========================================================================
    # TICK
    # ==========================================================================
    def tick(self):
        with self.lock:
            if self.state == "PLACING_ORDER": return

            if (self.mode == "1H_BIAS" and not config["ENABLE_1H"]) or \
               (self.mode == "4H_BIAS" and not config["ENABLE_4H"]):
                if self.state != "IDLE":
                    self.cancel_pending_orders()
                    self.reset()
                return

            if self.symbol in positions: return

            # Snapshot deque → list sekali, aman dari write bersamaan
            c_p = list(klines_data[self.symbol][self.tf_poi])
            c_c = list(klines_data[self.symbol][self.tf_conf])
            c_t = list(klines_data[self.symbol][self.tf_trig])
            price = live_prices[self.symbol]

            if not c_p or price == 0: return
            if time.time() - self.last_signal_time < self.cooldown and self.state == "IDLE": return

            # Setup timeout 1 jam
            if self.setup_time and time.time() - self.setup_time > 3600:
                if self.state in ["WAIT_C1","WAIT_C2","WAIT_C3","WAIT_OB_TOUCH"]:
                    if self.active_poi: self._append_ignored(self.active_poi["t"])
                    self.reset(); return

            # Limit order timeout 2 jam
            if self.state == "WAIT_ENTRY" and time.time() - self.last_signal_time > 7200:
                send_telegram(f"⏳ <b>{self.symbol}</b> [{self.mode}] Batal: Limit kadaluarsa (2 Jam).")
                self.cancel_pending_orders()
                if self.active_poi: self._append_ignored(self.active_poi["t"])
                self.reset(); return

            if self.state == "IDLE":
                for poi in reversed(get_unmitigated_poi(c_p)):
                    if poi["t"] in self.ignored_pois: continue
                    in_fvg = poi["fvg_b"] <= price <= poi["fvg_t"]
                    in_ob  = poi["ob_b"]  <= price <= poi["ob_t"]
                    if in_fvg or in_ob:
                        self.direction  = poi["dir"]
                        self.active_poi = poi
                        self.active_zone = "FVG" if in_fvg else "OB"
                        self.state      = "WAIT_C1"
                        self.setup_time = time.time()
                        return

            elif self.state == "WAIT_OB_TOUCH":
                poi = self.active_poi
                if self.direction == "BUY":
                    if price < poi["ob_b"]: self.reset(); return
                    if price <= poi["ob_t"]:
                        self.active_zone = "OB"; self.state = "WAIT_C1"; self.setup_time = time.time()
                elif self.direction == "SELL":
                    if price > poi["ob_t"]: self.reset(); return
                    if price >= poi["ob_b"]:
                        self.active_zone = "OB"; self.state = "WAIT_C1"; self.setup_time = time.time()

            elif self.state in ["WAIT_C1","WAIT_C2","WAIT_C3"]:
                poi = self.active_poi
                if self.direction == "BUY":
                    if price < poi["ob_b"]: self.reset(); return
                    if self.active_zone == "FVG" and price < poi["fvg_b"]:
                        self.active_zone = "OB"; self.state = "WAIT_C1"
                        self.setup_time = time.time(); self.fc1 = None; self.fc2 = None; return
                elif self.direction == "SELL":
                    if price > poi["ob_t"]: self.reset(); return
                    if self.active_zone == "FVG" and price > poi["fvg_t"]:
                        self.active_zone = "OB"; self.state = "WAIT_C1"
                        self.setup_time = time.time(); self.fc1 = None; self.fc2 = None; return

                if not c_c: return
                prev = c_c[-1]
                if prev["t"] == self.last_processed_conf_t: return
                self.last_processed_conf_t = prev["t"]

                if self.state == "WAIT_C1":
                    self.fc1 = prev
                    if self.check_and_trigger("C1", c_t, c_c, 15): return
                    self.state = "WAIT_C2"
                elif self.state == "WAIT_C2":
                    self.fc2 = prev
                    sweep = (self.direction == "BUY"  and prev["l"] < self.fc1["l"]) or \
                            (self.direction == "SELL" and prev["h"] > self.fc1["h"])
                    if sweep and self.check_and_trigger("C2 Sweep", c_t, c_c, 30): return
                    self.state = "WAIT_C3"
                elif self.state == "WAIT_C3":
                    if self.check_and_trigger("C3 Final", c_t, c_c, 45): return
                    if self.active_zone == "FVG":
                        self.state = "WAIT_OB_TOUCH"; self.fc1 = None; self.fc2 = None
                    else:
                        self._append_ignored(self.active_poi["t"]); self.reset()

            elif self.state == "WAIT_ENTRY":
                last_k   = c_t[-1] if c_t else None
                hit_buy  = self.direction == "BUY"  and (
                    price >= self.tp or price <= self.sl or
                    (last_k and (last_k["h"] >= self.tp or last_k["l"] <= self.sl)))
                hit_sell = self.direction == "SELL" and (
                    price <= self.tp or price >= self.sl or
                    (last_k and (last_k["l"] <= self.tp or last_k["h"] >= self.sl)))
                if hit_buy or hit_sell:
                    send_telegram(f"❌ <b>{self.symbol}</b> [{self.mode}] Batal: Harga lari ke TP/SL duluan.")
                    self.cancel_pending_orders()
                    self._append_ignored(self.active_poi["t"])
                    self.reset()

    # ==========================================================================
    # ORDER PLACEMENT
    # ==========================================================================
    def place_limit_and_sl(self):
        def run():
            try:
                p     = precisions.get(self.symbol)
                q_str = round_v((MARGIN_USDT * LEVERAGE) / self.entry, p["step"])
                q_f   = float(q_str)

                if q_f < p["min_qty"] or (q_f * self.entry) < p["min_notional"]:
                    with self.lock: self.reset()
                    return

                post_api({"symbol": self.symbol, "leverage": LEVERAGE}, "/fapi/v1/leverage")

                with state_lock:
                    active_signals[self.symbol] = {
                        "mode": self.mode, "tp": self.tp,
                        "sl": self.sl, "dir": self.direction, "qty": q_f
                    }

                res = post_api({
                    "symbol": self.symbol, "side": self.direction, "type": "LIMIT",
                    "quantity": q_str, "price": round_v(self.entry, p["tick"]),
                    "timeInForce": "GTC"
                }, "/fapi/v1/order")

                if "orderId" in res:
                    with self.lock:
                        self.pending_order_id = res["orderId"]
                        self.state = "WAIT_ENTRY"

                    opp = "SELL" if self.direction == "BUY" else "BUY"
                    sl_res = post_api({
                        "symbol": self.symbol, "side": opp,
                        "algoType": "CONDITIONAL", "type": "STOP_MARKET",
                        "triggerPrice": round_v(self.sl, p["tick"]),
                        "quantity": q_str, "reduceOnly": "true"
                    }, "/fapi/v1/algoOrder")

                    if "code" in sl_res:
                        send_telegram(f"⚠️ ERROR BINANCE (SL {self.symbol}): {sl_res.get('msg')}")
                    else:
                        self.pending_sl_algo_id = sl_res.get("algoId") or sl_res.get("orderId")

                    send_telegram(
                        f"⏳ <b>{self.symbol}</b> LIMIT + SL ({self.mode})\n"
                        f"📍 Dir: {self.direction}\n"
                        f"💰 Entry: <code>{round_v(self.entry, p['tick'])}</code>\n"
                        f"🛑 SL:    <code>{round_v(self.sl,    p['tick'])}</code>\n"
                        f"🎯 TP:    <code>{round_v(self.tp,    p['tick'])}</code>"
                    )
                else:
                    send_telegram(f"⚠️ ERROR BINANCE (LIMIT {self.symbol}): {res.get('msg')}")
                    with state_lock: active_signals.pop(self.symbol, None)
                    with self.lock:  self.reset()

            except Exception as e:
                logger.error(f"place_limit_and_sl {self.symbol}: {e}")
                with state_lock: active_signals.pop(self.symbol, None)
                with self.lock:  self.reset()

        threading.Thread(target=run, daemon=True).start()

    def cancel_pending_orders(self):
        oid, aid, sym = self.pending_order_id, self.pending_sl_algo_id, self.symbol
        def run():
            if oid: post_api({"symbol": sym, "orderId": oid}, "/fapi/v1/order",    method="DELETE")
            if aid: post_api({"symbol": sym, "algoId":  aid}, "/fapi/v1/algoOrder", method="DELETE")
            with state_lock: active_signals.pop(sym, None)
        threading.Thread(target=run, daemon=True).start()

# ==============================================================================
# TELEGRAM CMD
# ==============================================================================
def telegram_cmd():
    global total_pnl, total_wins, total_losses, current_month_str
    global API_KEY, SECRET_KEY, MARGIN_USDT, LEVERAGE, SL_BUFFER_PCT, MIN_RR

    t = os.getenv("TELEGRAM_TOKEN")
    if not t: return
    lid = 0
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{t}/getUpdates",
                             params={"offset": lid, "timeout": 10}).json()
            for i in r.get("result", []):
                lid  = i["update_id"] + 1
                txt  = i.get("message", {}).get("text", "")
                if not txt: continue
                logger.info(f"📥 Telegram: {txt}")
                txt   = txt.strip().lower()
                parts = txt.split()
                cmd   = parts[0]
                rep   = send_telegram

                if cmd == "/setapi" and len(parts) > 1:
                    API_KEY = parts[1]; update_env_file("BINANCE_API_KEY", API_KEY)
                    rep("✅ API KEY diperbarui!")
                elif cmd == "/setsecret" and len(parts) > 1:
                    SECRET_KEY = parts[1]; update_env_file("BINANCE_SECRET_KEY", SECRET_KEY)
                    rep("✅ SECRET KEY diperbarui!")
                elif cmd == "/margin" and len(parts) > 1:
                    try: MARGIN_USDT = float(parts[1]); update_env_file("MARGIN_USDT", MARGIN_USDT); rep(f"✅ Margin: <b>{MARGIN_USDT} USDT</b>")
                    except: rep("⚠️ Format angka salah.")
                elif cmd == "/leverage" and len(parts) > 1:
                    try: LEVERAGE = int(parts[1]); update_env_file("LEVERAGE", LEVERAGE); rep(f"✅ Leverage: <b>{LEVERAGE}x</b>")
                    except: rep("⚠️ Format angka salah.")
                elif cmd == "/buffer" and len(parts) > 1:
                    try: SL_BUFFER_PCT = float(parts[1]); update_env_file("SL_BUFFER_PCT", SL_BUFFER_PCT); rep(f"✅ SL Buffer: <b>{SL_BUFFER_PCT}%</b>")
                    except: rep("⚠️ Format angka salah.")
                elif cmd == "/minrr" and len(parts) > 1:
                    try: MIN_RR = float(parts[1]); update_env_file("MIN_RR", MIN_RR); rep(f"✅ Min RR: <b>{MIN_RR}</b>")
                    except: rep("⚠️ Format angka salah.")
                elif txt.startswith("/pnl"):
                    tot = total_wins + total_losses
                    wr  = (total_wins / tot * 100) if tot > 0 else 0
                    mode_str = "DOUBLE" if config["ENABLE_1H"] and config["ENABLE_4H"] \
                               else "1H BIAS" if config["ENABLE_1H"] else "4H BIAS"
                    rep(f"📊 <b>PnL {current_month_str}</b>\n"
                        f"💰 Total: <code>{total_pnl:.4f} USDT</code>\n"
                        f"📈 Winrate: {wr:.1f}% | ✅ {total_wins} | ❌ {total_losses}\n"
                        f"⚙️ Mode: {mode_str}")
                elif txt in ["/mode 1h", "/mode 4h", "/mode double"]:
                    config["ENABLE_1H"] = txt in ["/mode 1h", "/mode double"]
                    config["ENABLE_4H"] = txt in ["/mode 4h", "/mode double"]
                    for e in engines: e.cancel_pending_orders(); e.safe_reset_from_outside()
                    rep(f"✅ Mode: {txt.upper()}")
                elif txt.startswith("/status"):
                    lines = ["📊 <b>STATUS BOT</b>\n",
                             f"⚙️ Modal: {MARGIN_USDT} USDT | Lev: {LEVERAGE}x | Buffer: {SL_BUFFER_PCT}%\n",
                             "━━━━━━━━━━━━━━━━━━━\n📈 <b>POSISI FLOATING</b>\n━━━━━━━━━━━━━━━━━━━\n"]
                    with state_lock: pos_snap = dict(positions)
                    if pos_snap:
                        for s, p in pos_snap.items():
                            cur = live_prices.get(s, 0.0) or p["ep"]
                            pnl = (cur - p["ep"]) * p["qty"] if p["side"] == "BUY" else (p["ep"] - cur) * p["qty"]
                            mg  = (p["qty"] * p["ep"]) / LEVERAGE
                            pct = (pnl / mg * 100) if mg > 0 else 0
                            sgn = "+" if pnl > 0 else ""
                            pr  = precisions.get(s, {}); tick = pr.get("tick", 4) if pr else 4
                            tp_v, sl_v = None, None
                            for api_ep in ["/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"]:
                                orders = post_api({"symbol": s}, api_ep, method="GET")
                                if isinstance(orders, list):
                                    for o in orders:
                                        sp = float(o.get("triggerPrice", o.get("stopPrice", 0)))
                                        ot = o.get("origType", o.get("type", o.get("orderType","")))
                                        if ot in ["TAKE_PROFIT_MARKET","TAKE_PROFIT"] and sp > 0: tp_v = sp
                                        elif ot in ["STOP_MARKET","STOP"] and sp > 0: sl_v = sp
                            rr_str = "-"
                            if tp_v and sl_v:
                                risk   = (p["ep"] - sl_v) if p["side"] == "BUY" else (sl_v - p["ep"])
                                reward = (tp_v - p["ep"]) if p["side"] == "BUY" else (p["ep"] - tp_v)
                                if risk > 0: rr_str = f"1:{reward/risk:.2f}"
                            lines.append(
                                f"{'🟩' if pnl>0 else '🟥'} <b>{s}</b> ({p['side']})\n"
                                f"   Entry: <code>{p['ep']}</code> | TP: <code>{round_v(tp_v,tick) if tp_v else '-'}</code> | SL: <code>{round_v(sl_v,tick) if sl_v else '-'}</code>\n"
                                f"   PnL: <code>{sgn}{pnl:.2f} USDT ({sgn}{pct:.2f}%)</code> | RR: <code>{rr_str}</code>\n"
                            )
                    else: lines.append("💤 Tidak ada posisi aktif.\n")
                    lines.append("━━━━━━━━━━━━━━━━━━━\n🔍 <b>ANALISA AKTIF</b>\n━━━━━━━━━━━━━━━━━━━\n")
                    active_e = [e for e in engines if e.state not in ["IDLE"]]
                    if active_e:
                        for e in active_e:
                            tf  = "1H" if "1H" in e.mode else "4H"
                            st  = e.state.replace("_"," ") + (f" ({e.active_zone})" if e.active_zone else "")
                            pr  = precisions.get(e.symbol,{}); tick = pr.get("tick",4) if pr else 4
                            rr_str = "-"
                            if e.state in ["WAIT_ENTRY","PLACING_ORDER"] and e.entry and e.tp and e.sl:
                                risk   = (e.entry-e.sl) if e.direction=="BUY" else (e.sl-e.entry)
                                reward = (e.tp-e.entry) if e.direction=="BUY" else (e.entry-e.tp)
                                if risk > 0: rr_str = f"1:{reward/risk:.2f}"
                            ln = f"• <b>{e.symbol}</b> ({tf}) | <code>{st}</code>"
                            if e.entry: ln += f"\n  Entry: <code>{round_v(e.entry,tick)}</code>"
                            if e.tp:    ln += f" | TP: <code>{round_v(e.tp,tick)}</code>"
                            if e.sl:    ln += f" | SL: <code>{round_v(e.sl,tick)}</code>"
                            ln += f" | RR: <code>{rr_str}</code>"
                            lines.append(ln + "\n")
                    else: lines.append("💤 Semua koin IDLE.\n")
                    rep("\n".join(lines).strip())

                elif txt.startswith("/close"):
                    tgt = txt.replace("/close","").strip().upper()
                    if tgt != "ALL" and not tgt.endswith("USDT"): tgt += "USDT"
                    with state_lock: to_close = list(positions.keys()) if tgt=="ALL" else ([tgt] if tgt in positions else [])
                    for s in to_close:
                        with state_lock: p = positions.get(s)
                        if not p: continue
                        pr = precisions.get(s)
                        post_api({"symbol":s,"side":"SELL" if p["side"]=="BUY" else "BUY",
                                  "type":"MARKET","quantity":round_v(p["qty"],pr["step"]) if pr else str(p["qty"]),
                                  "reduceOnly":"true"}, "/fapi/v1/order")
                        post_api({"symbol":s}, "/fapi/v1/allOpenOrders",  method="DELETE")
                        post_api({"symbol":s}, "/fapi/v1/algoOpenOrders", method="DELETE")
                        rep(f"🛑 {s} ditutup paksa.")

                elif txt.startswith("/bep"):
                    tgt = txt.replace("/bep","").strip().upper()
                    if tgt != "ALL" and not tgt.endswith("USDT"): tgt += "USDT"
                    with state_lock: to_bep = list(positions.keys()) if tgt=="ALL" else ([tgt] if tgt in positions else [])
                    for s in to_bep:
                        with state_lock: p = positions.get(s)
                        if not p: continue
                        pr = precisions.get(s)
                        for api_ep in ["/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"]:
                            orders = post_api({"symbol":s}, api_ep, method="GET")
                            if isinstance(orders, list):
                                for o in orders:
                                    ot = o.get("origType", o.get("type", o.get("orderType","")))
                                    if ot in ["STOP_MARKET","STOP"]:
                                        if api_ep == "/fapi/v1/openOrders":
                                            post_api({"symbol":s,"orderId":o.get("orderId")}, "/fapi/v1/order", method="DELETE")
                                        else:
                                            post_api({"symbol":s,"algoId":o.get("algoId")}, "/fapi/v1/algoOrder", method="DELETE")
                        sl_p = {"symbol":s,"side":"SELL" if p["side"]=="BUY" else "BUY",
                                "type":"STOP_MARKET","stopPrice":round_v(p["ep"],pr["tick"]),"closePosition":"true"}
                        res_sl = post_api(sl_p, "/fapi/v1/order")
                        if "code" in res_sl:
                            sl_p.pop("stopPrice"); sl_p.update({"algoType":"CONDITIONAL","triggerPrice":round_v(p["ep"],pr["tick"])})
                            post_api(sl_p, "/fapi/v1/algoOrder")
                        rep(f"🛡️ {s} SL dipindah ke BEP.")

                elif txt.startswith("/help"):
                    rep("<b>📖 COMMANDS:</b>\n"
                        "<code>/margin /leverage /buffer /minrr</code> — Parameter\n"
                        "<code>/setapi /setsecret</code> — API Keys\n"
                        "<code>/status</code> — Status bot\n"
                        "<code>/pnl</code> — PnL bulan ini\n"
                        "<code>/mode 1h|4h|double</code> — Mode\n"
                        "<code>/close koin|all</code> — Tutup posisi\n"
                        "<code>/bep koin|all</code> — SL ke entry")
        except Exception as e:
            logger.warning(f"telegram_cmd: {e}"); time.sleep(5)

def load_monthly_pnl():
    global total_pnl, total_wins, total_losses, current_month_str
    current_month_str = datetime.now().strftime("%Y-%m")
    if not os.path.isfile(CSV_FILE): return
    try:
        with open(CSV_FILE, "r") as f:
            for row in csv.reader(f):
                if len(row) >= 6 and row[0].startswith(current_month_str):
                    pnl = float(row[3]); total_pnl += pnl
                    if pnl > 0: total_wins += 1
                    else: total_losses += 1
    except: pass

# ==============================================================================
# WEBSOCKET
# ==============================================================================
def keep_alive_listenkey():
    while True:
        time.sleep(1800)
        try: requests.put(BASE_URL + "/fapi/v1/listenKey", headers={"X-MBX-APIKEY": API_KEY}, timeout=10)
        except: pass

def on_market_msg(ws, m):
    try:
        d = json.loads(m)
        if "data" not in d: return
        k = d["data"]["k"]; s, tf = d["data"]["s"], k["i"]
        if tf == "1m": live_prices[s] = float(k["c"])
        if k["x"]:
            klines_data[s][tf].append({
                "t": k["t"], "o": float(k["o"]), "h": float(k["h"]),
                "l": float(k["l"]), "c": float(k["c"])
            })
        for e in engines:
            if e.symbol == s: e.tick()
    except: pass

def on_user_msg(ws, m):
    global total_pnl, total_wins, total_losses
    try:
        d = json.loads(m)
        if d.get("e") == "ORDER_TRADE_UPDATE":
            o = d["o"]; s = o["s"]
            if o["X"] == "FILLED" and o.get("o") == "LIMIT" and s in active_signals:
                sig = active_signals[s]; pr = precisions.get(s, {}); opp = "SELL" if sig["dir"] == "BUY" else "BUY"
                for e in engines:
                    if e.symbol == s and e.pending_sl_algo_id:
                        post_api({"symbol":s,"algoId":e.pending_sl_algo_id}, "/fapi/v1/algoOrder", method="DELETE")
                sl_p = {"symbol":s,"side":opp,"type":"STOP_MARKET","stopPrice":round_v(sig["sl"],pr.get("tick",4)),"closePosition":"true"}
                if "code" in post_api(sl_p, "/fapi/v1/order"):
                    sl_p.pop("stopPrice"); sl_p.update({"algoType":"CONDITIONAL","triggerPrice":round_v(sig["sl"],pr.get("tick",4))})
                    post_api(sl_p, "/fapi/v1/algoOrder")
                tp_p = {"symbol":s,"side":opp,"type":"TAKE_PROFIT_MARKET","stopPrice":round_v(sig["tp"],pr.get("tick",4)),"closePosition":"true"}
                if "code" in post_api(tp_p, "/fapi/v1/order"):
                    tp_p.pop("stopPrice"); tp_p.update({"algoType":"CONDITIONAL","triggerPrice":round_v(sig["tp"],pr.get("tick",4))})
                    post_api(tp_p, "/fapi/v1/algoOrder")
                send_telegram(f"🚀 <b>{s}</b> LIMIT FILLED! TP/SL auto-attached.")

            if o["X"] == "FILLED" and float(o.get("rp",0)) != 0:
                rp = float(o["rp"]); total_pnl += rp
                if rp > 0: total_wins += 1
                else: total_losses += 1
                ot = o.get("ot", o.get("o"))
                reason = "Hit TP 🎯" if ot in ["TAKE_PROFIT_MARKET","TAKE_PROFIT"] else "Hit SL 🛑"
                log_trade(s, o["S"], rp, active_signals.get(s, {}).get("mode","UNKNOWN"))
                send_telegram(f"{'✅' if rp>0 else '❌'} <b>{s}</b> CLOSED | {reason} | PnL: <code>{rp:+.4f} USDT</code>")

        if d.get("e") == "ACCOUNT_UPDATE":
            for p in d["a"]["P"]:
                pa = float(p["pa"]); s = p["s"]
                with state_lock:
                    if pa == 0: positions.pop(s, None); active_signals.pop(s, None)
                    else: positions[s] = {"side":"BUY" if pa>0 else "SELL","qty":abs(pa),"ep":float(p["ep"])}
                if pa == 0:
                    post_api({"symbol":s}, "/fapi/v1/allOpenOrders",  method="DELETE")
                    post_api({"symbol":s}, "/fapi/v1/algoOpenOrders", method="DELETE")
                for e in engines:
                    if e.symbol == s: e.safe_reset_from_outside()
    except Exception as ex:
        logger.warning(f"on_user_msg: {ex}")

def start_ws_with_reconnect(url, on_msg):
    def run():
        while True:
            try: websocket.WebSocketApp(url, on_message=on_msg).run_forever(ping_interval=60, ping_timeout=30)
            except: pass
            time.sleep(5)
    threading.Thread(target=run, daemon=True).start()

def start_user_ws():
    threading.Thread(target=keep_alive_listenkey, daemon=True).start()
    while True:
        try:
            lk = requests.post(BASE_URL + "/fapi/v1/listenKey",
                               headers={"X-MBX-APIKEY": API_KEY}).json().get("listenKey")
            if lk: websocket.WebSocketApp(f"{WS_BASE}/ws/{lk}", on_message=on_user_msg).run_forever(ping_interval=60, ping_timeout=30)
        except: pass
        time.sleep(5)

def start():
    load_precisions(); load_monthly_pnl()
    try:
        r = post_api({}, "/fapi/v2/positionRisk", method="GET")
        if isinstance(r, list):
            for p in r:
                amt = float(p["positionAmt"]); s = p["symbol"]
                if amt != 0 and s in symbols:
                    positions[s] = {"side":"BUY" if amt>0 else "SELL","qty":abs(amt),"ep":float(p["entryPrice"])}
    except: pass

    for s in symbols:
        for tf in ["4h","1h","15m","5m","1m"]:
            try:
                r = requests.get(BASE_URL + "/fapi/v1/klines",
                                 params={"symbol":s,"interval":tf,"limit":MAX_CANDLES+1}).json()
                klines_data[s][tf].extend([
                    {"t":k[0],"o":float(k[1]),"h":float(k[2]),"l":float(k[3]),"c":float(k[4])} for k in r[:-1]
                ])
            except: pass

    threading.Thread(target=telegram_cmd, daemon=True).start()
    streams = "/".join([f"{s.lower()}@kline_{tf}" for s in symbols for tf in ["1m","5m","15m","1h","4h"]])
    start_ws_with_reconnect(f"{WS_BASE}/stream?streams={streams}", on_market_msg)
    threading.Thread(target=start_user_ws, daemon=True).start()

engines = [Engine(s, m) for s in symbols for m in ["1H_BIAS", "4H_BIAS"]]

if __name__ == "__main__":
    start()
    print("🔥 BOT v9.3 (SMC LOGIC FIXED) ACTIVE...")
    while True:
        time.sleep(1)
