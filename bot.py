import os, time, json, hmac, hashlib, requests, websocket, threading, math, csv
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# --- CONFIG ---
API_KEY = os.getenv("BINANCE_API_KEY")
SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")
MODE = os.getenv("MODE", "TESTNET")
BASE_URL = "https://testnet.binancefuture.com" if MODE == "TESTNET" else "https://fapi.binance.com"
WS_BASE = "wss://fstream.binance.com" if MODE != "TESTNET" else "wss://stream.binancefuture.com"
MARGIN_USDT = float(os.getenv("MARGIN_USDT", 1.0))
LEVERAGE = int(os.getenv("LEVERAGE", 10))
MIN_RR = float(os.getenv("MIN_RR", 1.5))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", 5))

symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "SUIUSDT", "AVAXUSDT", "BNBUSDT", "TRXUSDT"]

bots = {}
positions = {}
trade_count = 0
total_pnl, total_wins, total_losses = 0.0, 0, 0

# --- CSV LOGGING SYSTEM ---
CSV_FILE = "riwayat_trading.csv"

def log_trade_to_csv(symbol, side, pnl, strategy_mode):
    file_exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, mode='a', newline='') as file:
        writer = csv.writer(file)
        if not file_exists:
            # Header untuk Excel
            writer.writerow(['Waktu', 'Simbol', 'Posisi', 'PnL (USDT)', 'Strategy_Mode', 'Status'])
        
        status = "WIN" if pnl > 0 else "LOSS"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow([now, symbol, side, round(pnl, 4), strategy_mode, status])

# --- UTILS ---
def sync_time():
    try: return requests.get(BASE_URL + "/fapi/v1/time").json()["serverTime"] - int(time.time() * 1000)
    except: return 0

time_offset = sync_time()
def ts(): return int(time.time() * 1000 + time_offset)
def sign(p): return hmac.new(SECRET_KEY.encode(), "&".join([f"{k}={v}" for k, v in p.items()]).encode(), hashlib.sha256).hexdigest()

def send_telegram(msg):
    t, c = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if t and c:
        try: requests.post(f"https://api.telegram.org/bot{t}/sendMessage", json={"chat_id": c, "text": msg}, timeout=5)
        except: pass

precisions = {}
def load_precisions():
    try:
        info = requests.get(BASE_URL + "/fapi/v1/exchangeInfo").json()
        for s in info["symbols"]:
            if s["symbol"] in symbols:
                t = s["filters"][0]["tickSize"]; st = s["filters"][1]["stepSize"]
                precisions[s["symbol"]] = {"tick": max(0, int(round(-math.log10(float(t))))), "step": max(0, int(round(-math.log10(float(st)))))}
    except: pass

def round_v(v, p): return f"{round(v, p):.{p}f}" if p > 0 else str(int(round(v)))

# --- SMC POI DETECTOR ---
def get_poi(c, depth=15):
    fvgs, obs = [], []
    for i in range(len(c)-depth, len(c)-2):
        # FVG
        if c[i]["h"] < c[i+2]["l"]: fvgs.append(("BUY", c[i]["h"], c[i+2]["l"]))
        elif c[i]["l"] > c[i+2]["h"]: fvgs.append(("SELL", c[i+2]["h"], c[i]["l"]))
        # Order Block (OB)
        if c[i]["c"] < c[i]["o"] and c[i+1]["c"] > c[i+1]["o"] and (c[i+1]["c"]-c[i+1]["o"]) > abs(c[i]["c"]-c[i]["o"])*1.5:
            obs.append(("BUY", c[i]["l"], c[i]["h"]))
        elif c[i]["c"] > c[i]["o"] and c[i+1]["c"] < c[i+1]["o"] and (c[i+1]["o"]-c[i+1]["c"]) > abs(c[i]["c"]-c[i]["o"])*1.5:
            obs.append(("SELL", c[i]["l"], c[i]["h"]))
    return fvgs, obs

def get_liquidity_target(c, direction, depth=15):
    swings = [x["h"] if direction == "BUY" else x["l"] for x in c[-depth:]]
    return max(swings) if direction == "BUY" else min(swings)

# --- BOT CLASS (DUAL ENGINE) ---
class Bot:
    def __init__(self, s):
        self.symbol = s; self.reset()
        self.c4h, self.c1h, self.c15, self.c5m, self.c1m = [], [], [], [], []
        self.live_price = 0.0
        self.last_active_mode = "N/A" # Menyimpan mode terakhir untuk logging CSV

    def reset(self):
        self.mode = None 
        self.state = "IDLE"; self.direction = None; self.poi_zones = []
        self.failed_c2 = None; self.entry_zone = None; self.sl_target = None; self.tp_target = None

    def update_kline(self, tf, k):
        if tf == "1m": 
            self.live_price = float(k["c"])
            if self.state == "WAIT_ENTRY": self.logic_entry()
            if k["x"]: self.c1m.append(k); self.c1m = self.c1m[-50:]
        elif tf == "5m" and k["x"]: self.c5m.append(k); self.c5m = self.c5m[-50:]
        elif tf == "15m" and k["x"]: self.c15.append(k); self.c15 = self.c15[-50:]
        elif tf == "1h" and k["x"]: self.c1h.append(k); self.c1h = self.c1h[-50:]
        elif tf == "4h" and k["x"]: self.c4h.append(k); self.c4h = self.c4h[-50:]
        
        if k["x"] and self.state not in ["WAIT_ENTRY", "EXECUTING"]: self.logic_structure()

    def logic_structure(self):
        if self.symbol in positions or len(self.c4h) < 10: return

        # --- PHASE 1: IDLE (PILIH JALUR POI) ---
        if self.state == "IDLE":
            # Cek Bias 4H Dulu (Prioritas Tinggi)
            fvgs4, obs4 = get_poi(self.c4h)
            last4 = self.c4h[-1]
            for t, l, h in fvgs4 + obs4:
                if (t == "SELL" and last4["h"] >= l) or (t == "BUY" and last4["l"] <= h):
                    self.mode = "4H_BIAS"; self.last_active_mode = "4H_BIAS"; self.direction = t; self.poi_zones = fvgs4 + obs4; self.state = "WAIT_CONF"; return

            # Jika 4H tidak ada, cek Bias 1H
            fvgs1, obs1 = get_poi(self.c1h)
            last1 = self.c1h[-1]
            for t, l, h in fvgs1 + obs1:
                if (t == "SELL" and last1["h"] >= l) or (t == "BUY" and last1["l"] <= h):
                    self.mode = "1H_BIAS"; self.last_active_mode = "1H_BIAS"; self.direction = t; self.poi_zones = fvgs1 + obs1; self.state = "WAIT_CONF"; return

        # --- PHASE 2: CONFIRMATION (1H atau 15M) ---
        elif self.state in ["WAIT_CONF", "WAIT_C3"]:
            # Check Invalidation (Harga keluar dari POI HTF)
            lows = [z[1] for z in self.poi_zones if z[0] == self.direction]
            highs = [z[2] for z in self.poi_zones if z[0] == self.direction]
            if (self.direction == "BUY" and self.live_price < min(lows)) or (self.direction == "SELL" and self.live_price > max(highs)):
                self.reset(); return

            # Tentukan TF Konfirmasi berdasarkan Mode
            conf_klines = self.c1h if self.mode == "4H_BIAS" else self.c15
            if len(conf_klines) < 2: return
            c_p, c_c = conf_klines[-2], conf_klines[-1]

            if self.state == "WAIT_CONF":
                if (self.direction == "BUY" and c_c["l"] < c_p["l"] and c_c["c"] > c_p["l"]) or \
                   (self.direction == "SELL" and c_c["h"] > c_p["h"] and c_c["c"] < c_p["h"]):
                    self.state = "WAIT_TRIGGER"
                elif (self.direction == "BUY" and c_c["l"] < c_p["l"]) or (self.direction == "SELL" and c_c["h"] > c_p["h"]):
                    self.failed_c2 = c_c; self.state = "WAIT_C3"
            
            elif self.state == "WAIT_C3" and c_c["t"] > self.failed_c2["t"]:
                if (self.direction == "BUY" and c_c["c"] > max(self.failed_c2["o"], self.failed_c2["c"])) or \
                   (self.direction == "SELL" and c_c["c"] < min(self.failed_c2["o"], self.failed_c2["c"])):
                    self.state = "WAIT_TRIGGER"
                else: self.state = "WAIT_CONF"

        # --- PHASE 3: TRIGGER (5M atau 1M) ---
        elif self.state == "WAIT_TRIGGER":
            trig_klines = self.c5m if self.mode == "4H_BIAS" else self.c1m
            if len(trig_klines) < 5: return
            
            # Displacement (CISD)
            c = trig_klines[-2]
            if (c["h"]-c["l"]) > 0 and abs(c["c"]-c["o"])/(c["h"]-c["l"]) > 0.7:
                # BOS Check (Body-to-Body)
                r = trig_klines[-5:]; hb = [max(x["o"],x["c"]) for x in r[:-1]]; lb = [min(x["o"],x["c"]) for x in r[:-1]]
                if (self.direction == "BUY" and r[-1]["c"] > max(hb)) or (self.direction == "SELL" and r[-1]["c"] < min(lb)):
                    self.entry_zone = (r[-1]["o"] + r[-1]["c"]) / 2
                    self.sl_target = r[-1]["h"] if self.direction == "SELL" else r[-1]["l"]
                    # Target IRL (Liquidity 15M untuk 1H Bias, Liquidity 1H untuk 4H Bias)
                    self.tp_target = get_liquidity_target(self.c15 if self.mode == "1H_BIAS" else self.c1h, self.direction)
                    
                    self.state = "WAIT_ENTRY"
                    p = precisions[self.symbol]["tick"]
                    
                    # --- KALKULASI RR UNTUK TELEGRAM ---
                    jarak_sl = abs(self.entry_zone - self.sl_target)
                    jarak_tp = abs(self.tp_target - self.entry_zone)
                    estimasi_rr = round(jarak_tp / jarak_sl, 2) if jarak_sl > 0 else 0
                    
                    send_telegram(f"🔔 {self.symbol} {self.direction} ({self.mode})\nentry : {round_v(self.entry_zone, p)}\nSL: {round_v(self.sl_target, p)}\nTP: {round_v(self.tp_target, p)}\nRR : 1:{estimasi_rr}\nstatus : waiting entry")

    def logic_entry(self):
        if not self.tp_target: return
        # Invalidation TP Hit
        if (self.direction == "SELL" and self.live_price <= self.tp_target) or (self.direction == "BUY" and self.live_price >= self.tp_target):
            self.reset(); return
        
        # Pullback Entry
        if (self.direction == "SELL" and self.live_price >= self.entry_zone) or (self.direction == "BUY" and self.live_price <= self.entry_zone):
            rr = abs(self.tp_target - self.live_price) / abs(self.live_price - self.sl_target) if abs(self.live_price-self.sl_target)>0 else 0
            if rr < MIN_RR: self.reset(); return
            self.state = "EXECUTING"; qty = (MARGIN_USDT * LEVERAGE) / self.live_price
            self.execute_binance(qty)

    def execute_binance(self, qty):
        headers = {"X-MBX-APIKEY": API_KEY}; p = precisions[self.symbol]
        f_qty, f_sl, f_tp = round_v(qty, p["step"]), round_v(self.sl_target, p["tick"]), round_v(self.tp_target, p["tick"])
        orders = [{"symbol": self.symbol, "side": self.direction, "type": "MARKET", "quantity": f_qty},
                  {"symbol": self.symbol, "side": "SELL" if self.direction == "BUY" else "BUY", "type": "STOP_MARKET", "stopPrice": f_sl, "closePosition": "true"},
                  {"symbol": self.symbol, "side": "SELL" if self.direction == "BUY" else "BUY", "type": "TAKE_PROFIT_MARKET", "stopPrice": f_tp, "closePosition": "true"}]
        params = {"batchOrders": json.dumps(orders), "timestamp": ts()}; params["signature"] = sign(params)
        try:
            r = requests.post(f"{BASE_URL}/fapi/v1/batchOrders", headers=headers, data=params).json()
            if isinstance(r, list) and not [x for x in r if "code" in x]:
                global trade_count; trade_count += 1
                send_telegram(f"🚀 {self.symbol} {self.direction} Executed ({self.mode})\nentry : {round_v(self.live_price, p['tick'])}\nSL: {f_sl}\nTP: {f_tp}")
        except: pass
        self.reset()

# --- ENGINE ---
def on_market_msg(ws, msg):
    d = json.loads(msg)
    if "data" in d:
        k, s, tf = d["data"]["k"], d["data"]["s"], d["data"]["k"]["i"]
        fk = {"t": k["t"], "o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"]), "x": k["x"]}
        if s in bots: bots[s].update_kline(tf, fk)

def telegram_polling():
    t = os.getenv("TELEGRAM_TOKEN"); last_id = 0
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{t}/getUpdates?offset={last_id}&timeout=10").json()
            if r.get("ok"):
                for i in r["result"]:
                    last_id = i["update_id"] + 1
                    if i.get("message", {}).get("text") == "/pnl":
                        wr = (total_wins/(total_wins+total_losses)*100) if (total_wins+total_losses)>0 else 0
                        msg = f"📊 PnL: {round(total_pnl, 4)} USDT\nWinrate: {round(wr, 1)}%\nWins: {total_wins} | Loss: {total_losses}\nActive: {len(positions)}"
                        requests.post(f"https://api.telegram.org/bot{t}/sendMessage", json={"chat_id": i["message"]["chat"]["id"], "text": msg})
        except: time.sleep(5)

def start():
    load_precisions()
    for s in symbols:
        bots[s] = Bot(s)
        for tf in ["4h", "1h"]:
            try:
                res = requests.get(f"{BASE_URL}/fapi/v1/klines", params={"symbol": s, "interval": tf, "limit": 50}).json()
                for k in res: bots[s].update_kline(tf, {"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]), "x": True})
            except: pass
    
    threading.Thread(target=telegram_polling, daemon=True).start()
    streams = "/".join([f"{s.lower()}@kline_{tf}" for s in symbols for tf in ["1m", "5m", "15m", "1h", "4h"]])
    threading.Thread(target=lambda: websocket.WebSocketApp(f"{WS_BASE}/stream?streams={streams}", on_message=on_market_msg).run_forever(ping_interval=60)).start()
    
    lk = requests.post(BASE_URL + "/fapi/v1/listenKey", headers={"X-MBX-APIKEY": API_KEY}).json().get("listenKey")
    def on_user(ws, m):
        global total_pnl, total_wins, total_losses
        d = json.loads(m)
        if d.get("e") == "ORDER_TRADE_UPDATE" and d["o"]["X"] == "FILLED":
            rp = float(d["o"].get("rp", 0))
            if rp != 0: 
                total_pnl += rp; total_wins += 1 if rp > 0 else 0; total_losses += 1 if rp < 0 else 0
                s = d["o"]["s"]
                side = d["o"]["S"]
                mode = bots[s].last_active_mode if s in bots else "N/A"
                # SIMPAN KE CSV & NOTIFIKASI
                log_trade_to_csv(s, side, rp, mode)
                send_telegram(f"💰 {s} Closed. PnL: {round(rp, 4)} USDT")
        if d.get("e") == "ACCOUNT_UPDATE":
            for p in d["a"]["P"]:
                if float(p["pa"]) == 0: positions.pop(p["s"], None)
                else: positions[p["s"]] = {"side": "BUY" if float(p["pa"]) > 0 else "SELL", "qty": abs(float(p["pa"]))}
    threading.Thread(target=lambda: websocket.WebSocketApp(f"{WS_BASE}/ws/{lk}", on_message=on_user).run_forever(ping_interval=60)).start()

if __name__ == "__main__":
    start()
    print("🔥 BOT DUAL-ENGINE + CSV LOGGING ACTIVE...")
    while True: time.sleep(1)
