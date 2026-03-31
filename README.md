Bot trading otomatis *Institutional-Grade* untuk Binance Futures yang dibangun menggunakan Python. Bot ini mengeksekusi perdagangan berdasarkan **icetea** dan model **TTrades Fractal**, dilengkapi dengan arsitektur *asynchronous* penuh dan kontrol dinamis melalui Telegram.

## ✨ Fitur Utama (v4.2 Stable)

* **True Dual-Engine Architecture:** Memantau dua skenario Bias secara independen dan paralel pada koin yang sama tanpa bentrok:
    * **Konservatif (Swing):** POI 4H ➔ Konfirmasi 1H ➔ Eksekusi 5M.
    * **Agresif (Scalping):** POI 1H ➔ Konfirmasi 15M ➔ Eksekusi 1M.
* **Unmitigated POI Filter:** Algoritma cerdas yang hanya memvalidasi FVG (Fair Value Gap) dan Order Block yang masih "perawan" (belum pernah tersentuh harga setelah terbentuk).
* **Dynamic Telegram Control:** Ubah strategi bot secara *real-time* (ON/OFF mode 1H atau 4H) langsung dari chat Telegram tanpa perlu me-restart server.
* **Asynchronous Execution & Anti-Freeze:** Pengiriman order API dan notifikasi berjalan di *background thread*, memastikan aliran data WebSocket harga tidak pernah terblokir (Anti-Lag).
* **Keep-Alive WebSocket:** Sistem otomatis yang memperpanjang umur `ListenKey` Binance setiap 45 menit untuk mencegah bot terputus (*disconnect*) dari aliran data akun.
* **Smart Invalidation:** Pembatalan *setup* otomatis jika harga menyentuh Target Profit (TP) sebelum menjemput area Entry, dilengkapi dengan notifikasi Telegram (`❌ Setup dibatalkan`).
* **Advanced Logging & Tracking:** Mencatat setiap riwayat perdagangan ke `riwayat_trading.csv` secara akurat, lengkap dengan identifikasi sinyal (apakah trade berasal dari mesin 1H atau 4H).

## 🛠️ Prasyarat Penting

Sebelum menjalankan bot, pastikan Anda memenuhi syarat berikut:

1.  Python 3.8+ terinstal di server/VPS.
2.  Akun Binance Futures (Sangat disarankan menggunakan **Testnet** untuk uji coba awal).
3.  **Wajib: Akun Futures berada dalam One-Way Mode** (Bukan Hedge Mode), karena bot menggunakan parameter `closePosition`.
4.  API Key dan Secret Key Binance.
5.  Bot Telegram & Chat ID untuk menerima notifikasi dan mengirim perintah.

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
    Buat file bernama `.env` di folder utama proyek dan isi dengan kredensial Anda:
    ```env
    BINANCE_API_KEY=api_key_kamu_disini
    BINANCE_SECRET_KEY=secret_key_kamu_disini
    MODE=TESTNET # Ubah ke MAINNET jika sudah siap live
    MARGIN_USDT=1.0 # Modal awal per posisi (USDT)
    LEVERAGE=10
    MIN_RR=1.5
    TELEGRAM_TOKEN=token_bot_telegram_kamu
    TELEGRAM_CHAT_ID=chat_id_telegram_kamu
    ```

## 🏃‍♂️ Cara Menjalankan Bot di VPS

Agar bot tetap berjalan di latar belakang (meskipun koneksi SSH ditutup), gunakan `screen`.

```bash
screen -S icetea
python3 bot.py
Untuk keluar dari layar (Detach): Tekan Ctrl + A lalu D.

Untuk kembali melihat log bot (Reattach): screen -r icetea.

📱 Perintah Telegram (Live Control)
Bot ini dapat dikendalikan sepenuhnya melalui Telegram. Ketik perintah berikut di chat bot Anda:

/pnl ➔ Menampilkan ringkasan Profit & Loss, Winrate, jumlah posisi aktif, dan Status Mode yang sedang berjalan.

/mode 1h ➔ Bot hanya akan mencari setup berdasarkan POI 1H (Scalping/Intraday). Mesin 4H akan dimatikan.

/mode 4h ➔ Bot hanya akan mencari setup berdasarkan POI 4H (Swing). Mesin 1H akan dimatikan.

/mode double ➔ Mengaktifkan keduanya. Bot memantau setup 1H dan 4H secara paralel.

⚠️ Disclaimer
Bot ini dibuat untuk tujuan edukasi, penelitian algoritma, dan otomatisasi strategi teknikal. Perdagangan mata uang kripto (terutama futures dengan leverage) memiliki risiko finansial yang sangat tinggi. Selalu gunakan Testnet terlebih dahulu sampai Anda memahami sepenuhnya probabilitas dan manajemen risiko dari strategi ini. Pengembang (Developer) tidak bertanggung jawab atas segala kerugian finansial yang terjadi akibat penggunaan perangkat lunak ini.
