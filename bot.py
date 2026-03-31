import os, time, json, hmac, hashlib, requests, websocket, threading, math, csv, urllib.parse
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

symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "SUIUSDT", "AVAXUSDT", "BNBUSDT", "TRXUSDT"]

# --- GLOBAL STATE ---
klines_data = {s: {"1m": [], "5m": [], "15m": [], "1h": [], "4h": []} for s in symbols}
live_prices = {s: 0.0 for s in symbols}
config = {"ENABLE_1H": True, "ENABLE_4H": True}
positions = {}
active_signals = {} # Menyimpan data TP untuk dipasang saat Limit Filled
CSV_FILE = "riwayat_trading.csv"

total_pnl, total_wins, total_losses = 0.0, 0, 0
current_month_str = datetime.now().strftime("%Y-%m")

# --- UTILS ---
def sync_time():
    try: return requests.get(BASE_URL + "/fapi/v1/time").json()["serverTime"] - int(time.time() * 1000)
    except: return 0
time_offset = sync_time()
def ts(): return int(time.time() * 1000 + time_offset)

def send_telegram(msg):
    def run():
        t, c = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
        if t and c:
            try: requests.post(f"https://api.telegram.org/bot{t}/sendMessage", json={"chat_id": c, "text": msg}, timeout=5)
            except: pass
    threading.Thread(target=run, daemon=True).start()

precisions = {}
def load_precisions():
    try:
        info = requests.get(BASE_URL + "/fapi/v1/exchangeInfo").json()
        for s in info["symbols"]:
            if s["symbol"] in symbols:
                t, st = s["filters"][0]["tickSize"], s["filters"][1]["stepSize"]
                precisions[s["symbol"]] = {"tick": max(0, int(round(-math.log10(float(t))))), "step": max(0, int(round(-math.log10(float(st)))))}
    except: pass
def round_v(v, p): return f"{round(v, p):.{p}f}" if p > 0 else str(int(round(v)))

def log_trade(symbol, side, pnl, mode):
    file_exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, mode='a', newline='') as f:
        w = csv.writer(f)
        if not file_exists: w.writerow(['Waktu', 'Simbol', 'Posisi', 'PnL (USDT)', 'Mode', 'Status'])
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), symbol, side, round(pnl,4), mode, "WIN" if pnl>0 else "LOSS"])

def post_api(params, endpoint, method="POST"):
    params["timestamp"] = ts()
    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(SECRET_KEY.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    payload = f"{query_string}&signature={signature}"
    if method == "POST": return requests.post(f"{BASE_URL}{endpoint}", headers=headers, data=payload).json()
    if method == "DELETE": return requests.delete(f"{BASE_URL}{endpoint}?{payload}", headers=headers).json()

# --- SMC LOGIC ---
def get_unmitigated_poi(c, depth=40, min_size=0.1):
    if len(c) < depth + 3: return [], []
    fvgs, obs = [], []
    for i in range(len(c) - depth, len(c) - 3):
        if c[i]["h"] < c[i+2]["l"]: 
            if ((c[i+2]["l"] - c[i]["h"])/c[i]["h"]*100) >= min_size:
                if not any(c[j]["l"] <= c[i+2]["l"] for j in range(i+3, len(c)-1)):
                    fvgs.append(("BUY", c[i]["h"], c[i+2]["l"]))
        elif c[i]["l"] > c[i+2]["h"]: 
            if ((c[i]["l"] - c[i+2]["h"])/c[i+2]["h"]*100) >= min_size:
                if not any(c[j]["h"] >= c[i+2]["h"] for j in range(i+3, len(c)-1)):
                    fvgs.append(("SELL", c[i+2]["h"], c[i]["l"]))
    return fvgs, obs

def get_target(c, d, depth=15):
    swings = [x["h"] if d == "BUY" else x["l"] for x in c[-depth:]]
    return max(swings) if d == "BUY" else min(swings)

# --- ENGINE CLASS ---
class Engine:
    def __init__(self, symbol, mode):
        self.symbol = symbol
        self.mode = mode 
        self.reset()
        if mode == "1H_BIAS": self.tf_poi, self.tf_conf, self.tf_trig = "1h", "15m", "1m"
        else: self.tf_poi, self.tf_conf, self.tf_trig = "4h", "1h", "5m"

    def reset(self):
        self.state = "IDLE"; self.dir = None; self.zones = []
        self.fc2 = None; self.entry = None; self.sl = None; self.tp = None
        self.pending_order_id = None
        self.pending_sl_id = None

    def tick(self):
        if (self.mode == "1H_BIAS" and not config["ENABLE_1H"]) or \
           (self.mode == "4H_BIAS" and not config["ENABLE_4H"]):
            if self.state != "IDLE": self.cancel_pending_limit(); self.reset()
            return

        if self.symbol in positions: return
        
        c_poi = klines_data[self.symbol][self.tf_poi]
        c_conf = klines_data[self.symbol][self.tf_conf]
        c_trig = klines_data[self.symbol][self.tf_trig]
        price = live_prices[self.symbol]
        
        if len(c_poi) < 10 or price == 0: return

        if self.state == "IDLE":
            f, o = get_unmitigated_poi(c_poi)
            last = c_poi[-1]
            for t, l, h in f:
                if (t == "SELL" and last["h"] >= l and last["l"] <= h) or (t == "BUY" and last["l"] <= h and last["h"] >= l):
                    self.dir = t; self.zones = f; self.state = "WAIT_CONF"; return

        elif self.state in ["WAIT_CONF", "WAIT_C3"]:
            lows = [z[1] for z in self.zones if z[0] == self.dir]; highs = [z[2] for z in self.zones if z[0] == self.dir]
            if (self.dir == "BUY" and price < min(lows)) or (self.dir == "SELL" and price > max(highs)):
                self.reset(); return
            if len(c_conf) < 2: return
            p, c = c_conf[-2], c_conf[-1]
            if self.state == "WAIT_CONF":
                if (self.dir == "BUY" and c["l"] < p["l"] and c["c"] > p["l"]) or (self.dir == "SELL" and c["h"] > p["h"] and c["c"] < p["h"]): self.state = "WAIT_TRIG"
                elif (self.dir == "BUY" and c["l"] < p["l"]) or (self.dir == "SELL" and c["h"] > p["h"]): self.fc2 = c; self.state = "WAIT_C3"
            elif self.state == "WAIT_C3" and c["t"] > self.fc2["t"]:
                if (self.dir == "BUY" and c["c"] > max(self.fc2["o"], self.fc2["c"])) or (self.dir == "SELL" and c["c"] < min(self.fc2["o"], self.fc2["c"])): self.state = "WAIT_TRIG"
                else: self.state = "WAIT_CONF"

        elif self.state == "WAIT_TRIG":
            if len(c_trig) < 5: return
            c = c_trig[-2]
            if (c["h"]-c["l"]) > 0 and abs(c["c"]-c["o"])/(c["h"]-c["l"]) > 0.7:
                r = c_trig[-5:]; hb = [max(x["o"],x["c"]) for x in r[:-1]]; lb = [min(x["o"],x["c"]) for x in r[:-1]]
                if (self.dir == "BUY" and r[-1]["c"] > max(hb)) or (self.dir == "SELL" and r[-1]["c"] < min(lb)):
                    self.entry = (r[-1]["o"] + r[-1]["c"]) / 2
                    self.sl = r[-1]["h"] if self.dir == "SELL" else r[-1]["l"]
                    self.tp = get_target(c_conf, self.dir) 
                    if abs(self.tp - self.entry)/abs(self.entry - self.sl) < MIN_RR: self.reset(); return 
                    
                    self.state = "WAIT_ENTRY"
                    self.place_limit_and_sl_async() # Pasang Limit & SL bersamaan!

        elif self.state == "WAIT_ENTRY":
            # Batal jika harga mencapai TP sebelum menjemput Limit
            if (self.dir == "SELL" and price <= self.tp) or (self.dir == "BUY" and price >= self.tp):
                send_telegram(f"❌ {self.symbol} Setup Batal: Harga menyentuh TP sebelum Limit terisi. Menghapus Limit Order...")
                self.cancel_pending_limit()
                self.reset()

    # --- FITUR BARU: PASANG LIMIT DAN SL BERSAMAAN ---
    def place_limit_and_sl_async(self):
        def run():
            try:
                pr = precisions[self.symbol]
                fq, fe = round_v((MARGIN_USDT * LEVERAGE) / self.entry, pr["step"]), round_v(self.entry, pr["tick"])
                fsl = round_v(self.sl, pr["tick"])
                opp_side = "SELL" if self.dir == "BUY" else "BUY"
                
                # 1. Pasang Limit Order
                params_limit = {"symbol": self.symbol, "side": self.dir, "type": "LIMIT", "quantity": fq, "price": fe, "timeInForce": "GTC"}
                res_limit = post_api(params_limit, "/fapi/v1/order")
                
                if "orderId" in res_limit:
                    self.pending_order_id = res_limit["orderId"]
                    
                    # 2. LANGSUNG Pasang Stop Loss (Perlindungan Absolut)
                    params_sl = {"symbol": self.symbol, "side": opp_side, "type": "STOP_MARKET", "algoType": "CONDITIONAL", "triggerPrice": fsl, "closePosition": "true"}
                    res_sl = post_api(params_sl, "/fapi/v1/algoOrder")
                    
                    if "orderId" in res_sl:
                        self.pending_sl_id = res_sl["orderId"]
                    else:
                        send_telegram(f"⚠️ Limit terpasang, tapi API SL Error {self.symbol}: {res_sl.get('msg')}")

                    # Simpan target TP untuk dipasang nanti setelah Limit Filled
                    active_signals[self.symbol] = {"mode": self.mode, "tp": self.tp, "dir": self.dir, "qty": fq}
                    send_telegram(f"⏳ {self.symbol} LIMIT & SL PLACED ({self.mode})\nEntry: {fe}\nSL: {fsl}\nTP: {round_v(self.tp, pr['tick'])} (Menunggu Limit)")
                else:
                    send_telegram(f"⚠️ Gagal Pasang Limit {self.symbol}: {res_limit.get('msg')}")
                    self.reset()
            except Exception as e: send_telegram(f"⚠️ Error Limit {self.symbol}: {e}")
        threading.Thread(target=run, daemon=True).start()

    def cancel_pending_limit(self):
        if self.pending_order_id:
            def run():
                # Hapus Limit Order
                post_api({"symbol": self.symbol, "orderId": self.pending_order_id}, "/fapi/v1/order", method="DELETE")
                # Hapus Stop Loss yang menggantung (jika ada)
                if self.pending_sl_id:
                    post_api({"symbol": self.symbol, "orderId": self.pending_sl_id}, "/fapi/v1/order", method="DELETE")
            threading.Thread(target=run, daemon=True).start()
            self.pending_order_id = None
            self.pending_sl_id = None

# --- WS & START ---
def on_market_msg(ws, msg):
    d = json.loads(msg)
    if "data" in d:
        k, s, tf = d["data"]["k"], d["data"]["s"], d["data"]["k"]["i"]
        if tf == "1m": live_prices[s] = float(k["c"])
        if k["x"] or tf == "1m":
            if k["x"]: klines_data[s][tf].append({"t":k["t"],"o":float(k["o"]),"h":float(k["h"]),"l":float(k["l"]),"c":float(k["c"]),"x":True}); klines_data[s][tf]=klines_data[s][tf][-60:]
            for e in engines: 
                if e.symbol == s: e.tick()

def on_user_msg(ws, m):
    global total_pnl, total_wins, total_losses, current_month_str
    d = json.loads(m)
    
    # 1. DETEKSI LIMIT FILLED -> PASANG TP SAJA (SL sudah dipasang di awal)
    if d.get("e") == "ORDER_TRADE_UPDATE" and d["o"]["X"] == "FILLED" and d["o"]["ot"] == "LIMIT":
        s = d["o"]["s"]
        if s in active_signals:
            sig = active_signals[s]; pr = precisions[s]
            opp = "SELL" if sig["dir"] == "BUY" else "BUY"
            # Hanya perlu menembak TP ke API AlgoOrder
            post_api({"symbol":s,"side":opp,"type":"TAKE_PROFIT_MARKET","algoType":"CONDITIONAL","triggerPrice":round_v(sig["tp"],pr["tick"]),"closePosition":"true"}, "/fapi/v1/algoOrder")
            send_telegram(f"🚀 {s} LIMIT FILLED! Take Profit (TP) telah dipasang otomatis.")

    # 2. DETEKSI POSISI CLOSED -> UPDATE PNL
    if d.get("e") == "ORDER_TRADE_UPDATE" and d["o"]["X"] == "FILLED" and float(d["o"].get("rp", 0)) != 0:
        rp = float(d["o"]["rp"]); s = d["o"]["s"]
        now_ym = datetime.now().strftime("%Y-%m")
        if now_ym != current_month_str: total_pnl, total_wins, total_losses = 0.0, 0, 0; current_month_str = now_ym
        total_pnl += rp; total_wins += 1 if rp > 0 else 0; total_losses += 1 if rp < 0 else 0
        mode = active_signals.get(s, {}).get("mode", "UNKNOWN")
        log_trade(s, d["o"]["S"], rp, mode)
        send_telegram(f"💰 {s} Closed. PnL: {round(rp, 4)} USDT ({mode})")

    if d.get("e") == "ACCOUNT_UPDATE":
        for p in d["a"]["P"]:
            if float(p["pa"]) == 0: 
                positions.pop(p["s"], None)
                active_signals.pop(p["s"], None)
            else: positions[p["s"]] = {"side": "BUY" if float(p["pa"]) > 0 else "SELL", "qty": abs(float(p["pa"]))}

def keep_alive_listenkey():
    while True:
        time.sleep(45 * 60) 
        try: requests.put(BASE_URL + "/fapi/v1/listenKey", headers={"X-MBX-APIKEY": API_KEY})
        except: pass

def load_monthly_pnl():
    global total_pnl, total_wins, total_losses, current_month_str
    total_pnl, total_wins, total_losses = 0.0, 0, 0
    current_month_str = datetime.now().strftime("%Y-%m")
    if not os.path.isfile(CSV_FILE): return
    try:
        with open(CSV_FILE, mode='r') as f:
            reader = csv.reader(f)
            next(reader, None) 
            for row in reader:
                if len(row) >= 6:
                    waktu = row[0]
                    if waktu.startswith(current_month_str):
                        pnl = float(row[3])
                        total_pnl += pnl
                        if pnl > 0: total_wins += 1
                        elif pnl < 0: total_losses += 1
    except: pass

def start():
    load_precisions()
    load_monthly_pnl()
    for s in symbols:
        for tf in ["4h", "1h", "15m", "5m"]:
            res = requests.get(f"{BASE_URL}/fapi/v1/klines", params={"symbol":s,"interval":tf,"limit":60}).json()
            klines_data[s][tf] = [{"t":k[0],"o":float(k[1]),"h":float(k[2]),"l":float(k[3]),"c":float(k[4]),"x":(i<len(res)-1)} for i,k in enumerate(res)]
    
    streams = "/".join([f"{s.lower()}@kline_{tf}" for s in symbols for tf in ["1m","5m","15m","1h","4h"]])
    threading.Thread(target=lambda: websocket.WebSocketApp(f"{WS_BASE}/stream?streams={streams}", on_message=on_market_msg).run_forever(ping_interval=60)).start()
    
    lk = requests.post(BASE_URL + "/fapi/v1/listenKey", headers={"X-MBX-APIKEY": API_KEY}).json().get("listenKey")
    threading.Thread(target=keep_alive_listenkey, daemon=True).start()
    threading.Thread(target=lambda: websocket.WebSocketApp(f"{WS_BASE}/ws/{lk}", on_message=on_user_msg).run_forever(ping_interval=60)).start()

engines = [Engine(s, m) for s in symbols for m in ["1H_BIAS", "4H_BIAS"]]
if __name__ == "__main__":
    start(); print("🔥 BOT v4.9 (ULTIMATE PROTECTOR) ACTIVE...")
    while True: time.sleep(1)
