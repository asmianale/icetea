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

config = {
    "ENABLE_1H": True,
    "ENABLE_4H": True
}

positions = {}
active_signals = {} 
total_pnl, total_wins, total_losses = 0.0, 0, 0
CSV_FILE = "riwayat_trading.csv"

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

def keep_alive_listenkey():
    while True:
        time.sleep(45 * 60) 
        try: requests.put(BASE_URL + "/fapi/v1/listenKey", headers={"X-MBX-APIKEY": API_KEY})
        except: pass

# --- SMC LOGIC ---
def get_unmitigated_poi(c, depth=40, min_size=0.1):
    if len(c) < depth + 2: return [], []
    fvgs, obs = [], []
    
    # PERBAIKAN BUG FVG: Cek mitigasi hanya sampai H-1 (len(c)-1), agar sentuhan pertama hari ini terhitung Fresh!
    for i in range(len(c) - depth, len(c) - 2):
        if c[i]["h"] < c[i+2]["l"]: # BUY FVG
            if ((c[i+2]["l"] - c[i]["h"])/c[i]["h"]*100) >= min_size:
                if not any(c[j]["l"] <= c[i+2]["l"] for j in range(i+3, len(c)-1)):
                    fvgs.append(("BUY", c[i]["h"], c[i+2]["l"]))
                    
        elif c[i]["l"] > c[i+2]["h"]: # SELL FVG
            if ((c[i]["l"] - c[i+2]["h"])/c[i+2]["h"]*100) >= min_size:
                if not any(c[j]["h"] >= c[i+2]["h"] for j in range(i+3, len(c)-1)):
                    fvgs.append(("SELL", c[i+2]["h"], c[i]["l"]))
                    
        if c[i]["c"] < c[i]["o"] and c[i+1]["c"] > c[i+1]["o"] and (c[i+1]["c"]-c[i+1]["o"]) > abs(c[i]["c"]-c[i]["o"])*1.5:
            if not any(c[j]["l"] <= c[i]["h"] for j in range(i+2, len(c)-1)):
                obs.append(("BUY", c[i]["l"], c[i]["h"]))
                
        elif c[i]["c"] > c[i]["o"] and c[i+1]["c"] < c[i+1]["o"] and (c[i+1]["o"]-c[i+1]["c"]) > abs(c[i]["c"]-c[i]["o"])*1.5:
            if not any(c[j]["h"] >= c[i]["l"] for j in range(i+2, len(c)-1)):
                obs.append(("SELL", c[i]["l"], c[i]["h"]))
                
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
        
        if mode == "1H_BIAS":
            self.tf_poi, self.tf_conf, self.tf_trig = "1h", "15m", "1m"
        else:
            self.tf_poi, self.tf_conf, self.tf_trig = "4h", "1h", "5m"

    def reset(self):
        self.state = "IDLE"; self.dir = None; self.zones = []
        self.fc2 = None; self.entry = None; self.sl = None; self.tp = None

    def tick(self):
        if (self.mode == "1H_BIAS" and not config["ENABLE_1H"]) or \
           (self.mode == "4H_BIAS" and not config["ENABLE_4H"]):
            if self.state != "IDLE": self.reset() 
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
            for t, l, h in f + o:
                if (t == "SELL" and last["h"] >= l and last["l"] <= h) or (t == "BUY" and last["l"] <= h and last["h"] >= l):
                    self.dir = t; self.zones = f + o; self.state = "WAIT_CONF"; return

        elif self.state in ["WAIT_CONF", "WAIT_C3"]:
            lows = [z[1] for z in self.zones if z[0] == self.dir]
            highs = [z[2] for z in self.zones if z[0] == self.dir]
            if (self.dir == "BUY" and price < min(lows)) or (self.dir == "SELL" and price > max(highs)):
                self.reset(); return

            if len(c_conf) < 2: return
            p, c = c_conf[-2], c_conf[-1]
            
            if self.state == "WAIT_CONF":
                if (self.dir == "BUY" and c["l"] < p["l"] and c["c"] > p["l"]) or (self.dir == "SELL" and c["h"] > p["h"] and c["c"] < p["h"]):
                    self.state = "WAIT_TRIG"
                elif (self.dir == "BUY" and c["l"] < p["l"]) or (self.dir == "SELL" and c["h"] > p["h"]):
                    self.fc2 = c; self.state = "WAIT_C3"
            
            elif self.state == "WAIT_C3" and c["t"] > self.fc2["t"]:
                if (self.dir == "BUY" and c["c"] > max(self.fc2["o"], self.fc2["c"])) or (self.dir == "SELL" and c["c"] < min(self.fc2["o"], self.fc2["c"])):
                    self.state = "WAIT_TRIG"
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
                    
                    jsl = abs(self.entry - self.sl); jtp = abs(self.tp - self.entry)
                    rr = round(jtp/jsl, 2) if jsl > 0 else 0
                    if rr < MIN_RR: self.reset(); return 
                    
                    self.state = "WAIT_ENTRY"; p = precisions[self.symbol]["tick"]
                    send_telegram(f"🔔 {self.symbol} {self.dir} ({self.mode})\nEntry: {round_v(self.entry, p)}\nSL: {round_v(self.sl, p)}\nTP: {round_v(self.tp, p)}\nRR: 1:{rr}\nStatus: Waiting Pullback")

        elif self.state == "WAIT_ENTRY":
            if not self.tp: return
            if (self.dir == "SELL" and price <= self.tp) or (self.dir == "BUY" and price >= self.tp):
                send_telegram(f"❌ {self.symbol} {self.dir} ({self.mode})\nSetup dibatalkan. Harga mencapai TP sebelum menjemput Entry.")
                self.reset(); return
            
            if (self.dir == "SELL" and price >= self.entry) or (self.dir == "BUY" and price <= self.entry):
                qty = (MARGIN_USDT * LEVERAGE) / price
                self.execute_binance_async(qty, price, self.dir, self.sl, self.tp, self.mode)
                self.reset()

    # PERBAIKAN BUG SIGNATURE: URL Encode ketat sesuai aturan Binance
    def execute_binance_async(self, qty, price, direction, sl, tp, mode):
        def run():
            try:
                pr = precisions[self.symbol]
                fq, fsl, ftp = round_v(qty, pr["step"]), round_v(sl, pr["tick"]), round_v(tp, pr["tick"])
                
                orders = [{"symbol": self.symbol, "side": direction, "type": "MARKET", "quantity": fq},
                          {"symbol": self.symbol, "side": "SELL" if direction == "BUY" else "BUY", "type": "STOP_MARKET", "stopPrice": fsl, "closePosition": "true"},
                          {"symbol": self.symbol, "side": "SELL" if direction == "BUY" else "BUY", "type": "TAKE_PROFIT_MARKET", "stopPrice": ftp, "closePosition": "true"}]
                
                # 1. Hapus spasi dari string JSON
                batch_json = json.dumps(orders, separators=(',', ':'))
                
                # 2. Siapkan parameter mentah
                params = {"batchOrders": batch_json, "timestamp": ts()}
                
                # 3. Ubah ke format URL Query String persis seperti yang akan dikirim
                query_string = urllib.parse.urlencode(params)
                
                # 4. Tanda tangani (Sign) Query String tersebut
                signature = hmac.new(SECRET_KEY.encode(), query_string.encode(), hashlib.sha256).hexdigest()
                
                # 5. Kirim via header Form-Urlencoded murni
                headers = {"X-MBX-APIKEY": API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
                payload = f"{query_string}&signature={signature}"
                
                res = requests.post(f"{BASE_URL}/fapi/v1/batchOrders", headers=headers, data=payload).json()
                
                if isinstance(res, list):
                    errors = [x for x in res if "code" in x]
                    if not errors:
                        active_signals[self.symbol] = mode 
                        send_telegram(f"🚀 {self.symbol} {direction} EXECUTED ({mode})\nEntry: {round_v(price, pr['tick'])}\nSL: {fsl}\nTP: {ftp}")
                    else:
                        send_telegram(f"⚠️ API Error {self.symbol}: {errors[0].get('msg')}")
                else:
                    send_telegram(f"⚠️ API Error {self.symbol}: {res.get('msg', 'Unknown Error')}")
            except Exception as e:
                send_telegram(f"⚠️ Script Error {self.symbol}: Gagal menghubungi Binance. {e}")

        threading.Thread(target=run, daemon=True).start()

engines = []
for s in symbols:
    engines.append(Engine(s, "1H_BIAS"))
    engines.append(Engine(s, "4H_BIAS"))

# --- WS & TELEGRAM ---
def on_market_msg(ws, msg):
    d = json.loads(msg)
    if "data" in d:
        k, s, tf = d["data"]["k"], d["data"]["s"], d["data"]["k"]["i"]
        fk = {"t": k["t"], "o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"]), "x": k["x"]}
        
        if tf == "1m": live_prices[s] = float(k["c"])
        
        if k["x"] or tf == "1m": 
            if k["x"]: 
                klines_data[s][tf].append(fk)
                klines_data[s][tf] = klines_data[s][tf][-60:] 
            for e in engines:
                if e.symbol == s: e.tick()

def telegram_cmd():
    global total_pnl, total_wins, total_losses
    t = os.getenv("TELEGRAM_TOKEN"); lid = 0
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{t}/getUpdates?offset={lid}&timeout=10").json()
            if r.get("ok"):
                for i in r["result"]:
                    lid = i["update_id"] + 1
                    msg = i.get("message", {}).get("text", "").lower()
                    chat_id = i["message"]["chat"]["id"]
                    
                    if msg == "/pnl":
                        wr = (total_wins/(total_wins+total_losses)*100) if (total_wins+total_losses)>0 else 0
                        if config["ENABLE_1H"] and config["ENABLE_4H"]: st_mode = "DOUBLE (1H & 4H)"
                        elif config["ENABLE_1H"]: st_mode = "HANYA 1H BIAS"
                        elif config["ENABLE_4H"]: st_mode = "HANYA 4H BIAS"
                        else: st_mode = "SEMUA MATI"
                        resp = f"📊 PnL: {round(total_pnl, 4)} USDT\nWinrate: {round(wr, 1)}%\nWins: {total_wins} | Loss: {total_losses}\nActive: {len(positions)}\n\n⚙️ Mode Aktif: {st_mode}"
                        requests.post(f"https://api.telegram.org/bot{t}/sendMessage", json={"chat_id": chat_id, "text": resp})
                        
                    elif msg == "/mode 1h":
                        config["ENABLE_1H"] = True; config["ENABLE_4H"] = False
                        for e in engines: 
                            if e.mode == "4H_BIAS": e.reset()
                        requests.post(f"https://api.telegram.org/bot{t}/sendMessage", json={"chat_id": chat_id, "text": "✅ Mode diubah: HANYA mencari setup 1H Bias."})
                        
                    elif msg == "/mode 4h":
                        config["ENABLE_1H"] = False; config["ENABLE_4H"] = True
                        for e in engines: 
                            if e.mode == "1H_BIAS": e.reset()
                        requests.post(f"https://api.telegram.org/bot{t}/sendMessage", json={"chat_id": chat_id, "text": "✅ Mode diubah: HANYA mencari setup 4H Bias."})
                        
                    elif msg == "/mode double":
                        config["ENABLE_1H"] = True; config["ENABLE_4H"] = True
                        requests.post(f"https://api.telegram.org/bot{t}/sendMessage", json={"chat_id": chat_id, "text": "✅ Mode diubah: DOUBLE aktif (1H & 4H)."})
        except: time.sleep(5)

def start():
    load_precisions()
    for s in symbols:
        for tf in ["4h", "1h", "15m", "5m"]:
            try:
                res = requests.get(f"{BASE_URL}/fapi/v1/klines", params={"symbol": s, "interval": tf, "limit": 60}).json()
                klines_data[s][tf] = [
                    {"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]), "x": (i < len(res) - 1)} 
                    for i, k in enumerate(res)
                ]
            except: pass
    
    threading.Thread(target=telegram_cmd, daemon=True).start()
    streams = "/".join([f"{s.lower()}@kline_{tf}" for s in symbols for tf in ["1m", "5m", "15m", "1h", "4h"]])
    threading.Thread(target=lambda: websocket.WebSocketApp(f"{WS_BASE}/stream?streams={streams}", on_message=on_market_msg).run_forever(ping_interval=60)).start()
    
    lk_response = requests.post(BASE_URL + "/fapi/v1/listenKey", headers={"X-MBX-APIKEY": API_KEY}).json()
    lk = lk_response.get("listenKey")
    
    if lk:
        threading.Thread(target=keep_alive_listenkey, daemon=True).start()
        def on_user(ws, m):
            global total_pnl, total_wins, total_losses
            d = json.loads(m)
            if d.get("e") == "ORDER_TRADE_UPDATE" and d["o"]["X"] == "FILLED":
                rp = float(d["o"].get("rp", 0))
                if rp != 0: 
                    total_pnl += rp; total_wins += 1 if rp > 0 else 0; total_losses += 1 if rp < 0 else 0
                    s = d["o"]["s"]
                    side = d["o"]["S"]
                    mode = active_signals.get(s, "UNKNOWN")
                    log_trade(s, side, rp, mode)
                    send_telegram(f"💰 {s} Closed. PnL: {round(rp, 4)} USDT ({mode})")
                    
            if d.get("e") == "ACCOUNT_UPDATE":
                for p in d["a"]["P"]:
                    s = p["s"]
                    if float(p["pa"]) == 0: 
                        positions.pop(s, None)
                        active_signals.pop(s, None)
                    else: 
                        positions[s] = {"side": "BUY" if float(p["pa"]) > 0 else "SELL", "qty": abs(float(p["pa"]))}
                        
        threading.Thread(target=lambda: websocket.WebSocketApp(f"{WS_BASE}/ws/{lk}", on_message=on_user).run_forever(ping_interval=60)).start()
    else: print("Gagal mendapatkan ListenKey. Cek API Key Anda.")

if __name__ == "__main__":
    start()
    print("🔥 BOT DUAL-ENGINE v4.3 (BUGFIX) ACTIVE...")
    while True: time.sleep(1)
