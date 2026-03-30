🚀 icetea-Engine Trading Bot (Binance Futures)

Bot trading otomatis untuk Binance Futures yang dibangun menggunakan Python. Bot ini mengeksekusi perdagangan berdasarkan **Smart Money Concepts (SMC)** dan model **TTrades Fractal**, dengan fokus pada efisiensi tinggi dan manajemen risiko yang ketat.

## ✨ Fitur Utama

* **Dual-Engine Logic:** Memantau dua skenario *Point of Interest* (POI) secara bersamaan tanpa bentrok:
    * **Konservatif (Swing):** POI 4H ➔ Konfirmasi 1H ➔ Eksekusi 5M.
    * **Agresif (Scalping):** POI 1H ➔ Konfirmasi 15M ➔ Eksekusi 1M.
* **Smart Invalidation & Time-Lock:** Mencegah *fakeout* dan *noise* dengan menunggu *candle close* pada timeframe konfirmasi (C3 Time-Lock).
* **Dynamic Liquidity Targets (ERL to IRL):** Secara otomatis menargetkan *Internal Range Liquidity* (Swing High/Low terbaru) sebagai Take Profit, bukan target statis.
* **Virtual Limit Orders:** Menunggu harga *pullback* ke zona keseimbangan (*equilibrium*) sebelum mengeksekusi *Market Order* untuk rasio Risk/Reward (RR) yang optimal.
* **Telegram Integration:** Memberikan notifikasi *real-time* saat *setup* ditemukan (beserta estimasi RR), saat order dieksekusi, dan laporan PnL harian/bulanan via perintah `/pnl`.
* **Auto-Logging:** Menyimpan riwayat perdagangan (waktu, koin, arah, PnL, dan strategi yang digunakan) ke dalam file `riwayat_trading.csv` untuk analisis kinerja.
* **Resource Efficient:** Menggunakan satu koneksi WebSocket tunggal untuk memantau berbagai koin dan timeframe secara bersamaan.

## 🛠️ Prasyarat

Sebelum menjalankan bot, pastikan kamu memiliki:

1.  Python 3.8 atau yang lebih baru.
2.  Akun Binance (disarankan menggunakan **Testnet** terlebih dahulu).
3.  API Key dan Secret Key dari Binance Futures.
4.  Bot Telegram dan Chat ID kamu (untuk notifikasi).

## 🚀 Instalasi & Persiapan

1.  **Clone repositori ini:**
    ```bash
    git clone [https://github.com/username-kamu/nama-repo-kamu.git](https://github.com/username-kamu/nama-repo-kamu.git)
    cd nama-repo-kamu
    ```

2.  **Buat Virtual Environment (Sangat Disarankan):**
    ```bash
    python3 -m venv myenv
    source myenv/bin/activate  # Di Windows gunakan: myenv\Scripts\activate
    ```

3.  **Instal Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Konfigurasi File `.env`:**
    Buat file bernama `.env` di folder utama proyek dan isi dengan kredensial kamu. Gunakan format berikut:
    ```env
    BINANCE_API_KEY=api_key_kamu_disini
    BINANCE_SECRET_KEY=secret_key_kamu_disini
    MODE=TESTNET # Ubah ke MAINNET jika sudah siap live
    MARGIN_USDT=1.0 # Modal awal per posisi (dalam USDT)
    LEVERAGE=10
    MIN_RR=1.5
    MAX_TRADES_PER_DAY=5
    TELEGRAM_TOKEN=token_bot_telegram_kamu
    TELEGRAM_CHAT_ID=chat_id_telegram_kamu
    ```

## 🏃‍♂️ Cara Menjalankan Bot

Untuk menjalankan bot agar tetap aktif di server/VPS meskipun kamu menutup terminal, gunakan `screen` atau `tmux`.

```bash
screen -S icetea
python3 bot.py
Untuk keluar dari screen tanpa mematikan bot: Tekan Ctrl + A lalu D.

Untuk kembali melihat log bot: screen -r smc_bot.

📈 Cara Mengecek Status Bot
Buka bot Telegram kamu dan ketikkan perintah:

Plaintext
/pnl
Bot akan membalas dengan ringkasan profit/loss, winrate, dan posisi yang sedang aktif.

⚠️ Disclaimer
Bot ini dibuat untuk tujuan edukasi dan eksperimen. Perdagangan mata uang kripto (terutama futures/leverage) memiliki risiko tinggi. Gunakan di akun Testnet terlebih dahulu sampai kamu memahami sepenuhnya cara kerja dan risiko dari strategi ini. Pengembang tidak bertanggung jawab atas segala kerugian finansial yang mungkin terjadi.

Dibangun dengan sabar untuk menunggu setup terbaik. 🎯
