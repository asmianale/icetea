# 🎯 icetea - V8.3 (The Remote Controller)

Bot trading otomatis untuk Binance Futures yang digerakkan sepenuhnya oleh algoritma **Smart Money Concepts (SMC)**. Bot ini beroperasi layaknya *Sniper Institusi*: mencari jejak bandar (Order Block & Fair Value Gap), menunggu dengan sabar, dan mengeksekusi *Entry* pada *Timeframe* 1 Menit dengan presisi piksel.

---

## 🔥 Fitur Utama (V8 Series Upgrades)

* **Strict Virgin FVG (Zero Touch Tolerance):** Menggunakan logika SMC garis keras. Bot menolak FVG yang sudah pernah tersentuh ujung jarum walau hanya 1 tick di masa lalu.
* **The Wick Hunter (Anti-Tertinggal Kereta):** Memantau ekor *candle real-time*. Jika harga lari mencapai Take Profit sebelum jaring Limit terjemput, bot otomatis membatalkan pesanan untuk mencegah *Zombie Order*.
* **Unleashed Risk/Reward:** Tidak ada batasan rasio RR. Bot mengincar *Swing High/Low* dari *Timeframe* besar sebagai target murni, memungkinkan hasil RR monster (1:5, 1:10, dll).
* **The Truth Teller (Notifikasi Jujur):** Algoritma pintar yang bisa membedakan eksekusi manual, Take Profit Algo, dan Stop Loss *Wick Sweep* (sapuan jarum), lalu melaporkannya secara akurat ke Telegram.
* **Dynamic SL Buffer:** Stop loss otomatis diberi jarak napas (buffer) dari pucuk struktur untuk menghindari jebakan likuiditas *Market Maker*.
* **Telegram Remote Control:** Mengedit konfigurasi, modal, leverage, hingga mengganti API Key langsung dari *chat* Telegram tanpa perlu mematikan bot!

---

## 🧠 Arsitektur Multi-Timeframe (X-Ray Logic)

Bot berjalan dalam dua mode secara paralel untuk setiap koin:

1.  **1H BIAS (Day Trading Mode)**
    * Radar (POI): 1 Jam (1H)
    * Konfirmasi: 15 Menit (15m)
    * Eksekusi/Pelatuk: 1 Menit (1m)
2.  **4H BIAS (Swing Mode)**
    * Radar (POI): 4 Jam (4H)
    * Konfirmasi: 1 Jam (1H)
    * Eksekusi/Pelatuk: 5 Menit (5m)

---

## 🚀 Cara Instalasi

1.  Pastikan sudah menginstal Python 3.9+.
2.  Install library yang dibutuhkan:
    ```bash
    pip install requests websocket-client python-dotenv
    ```
3.  Buat file `.env` (lihat contoh `.env` template) dan isi dengan API Key Binance dan Token Telegram Anda.
4.  Jalankan bot:
    ```bash
    python bot.py
    ```

---

## 📱 Perintah Telegram (Remote Control)

Anda memegang kendali penuh atas bot melalui *chat* Telegram.

### 🎛️ Edit Konfigurasi (Langsung Aktif & Tersimpan di `.env`)
* `/margin <angka>` : Mengubah modal per koin (Contoh: `/margin 5.0`).
* `/leverage <angka>` : Mengubah *leverage* (Contoh: `/leverage 20`).
* `/buffer <angka>` : Mengubah jarak aman Stop Loss (Contoh: `/buffer 0.2`).
* `/minrr <angka>` : Mengubah batas bawah Risk/Reward (Contoh: `/minrr 2.0`).
* `/setapi <api_key>` : Mengganti Binance API Key.
* `/setsecret <secret_key>` : Mengganti Binance Secret Key.
* `/mode <1h / 4h / double>` : Menghidupkan/mematikan mesin analisa tertentu.

### 📊 Monitor & Eksekusi
* `/status` : Menampilkan posisi aktif (*floating PnL*) dan koin yang sedang dianalisa (*Wait Entry/C1/C2/C3*).
* `/pnl` : Menampilkan laporan profit/loss dan Winrate bulan ini.
* `/close <koin / all>` : Menutup paksa posisi via *Market Order* (Contoh: `/close sol` atau `/close all`).
* `/bep <koin / all>` : Memindahkan Stop Loss ke titik *Entry* (Break Even Point) agar posisi aman tanpa risiko rugi.

---
*Disclaimer: Trading aset kripto Futures memiliki risiko tinggi. Gunakan bot ini dengan bijak dan manajemen risiko yang ketat.*
