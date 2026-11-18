# GTA 6 Dual-Pipeline Auto-Clipper (TR + GLOBAL)

Kick, Twitch ve YouTube canlı yayınlarından ve trend kliplerden otomatik **30–45 saniyelik 9:16 dikey Shorts & Reels** üreten, **n8n + Telegram + Yapay Zeka (Gemini / OpenAI + Whisper)** destekli çift kanallı otomasyon platformu.

## Sistem Neleri Çözer?

1. **İki Tamamen Bağımsız Kanal Hattı:**
   - **🇹🇷 Türkçe Hattı (`tr`):** `rraenee` (RRaenee / Rane), `elraenn`, `jahrein`, `wtcn`, `mithrain` önceden tanımlıdır. Türkçe viral başlık, açıklama, etiket ve altyazı üretip **TR YouTube + TR Instagram** hesaplarına yollar.
   - **🌍 Global Hattı (`global`):** `ishowspeed`, `kaicenat`, `xqc`, `adinross`, `stableronaldo`, `caseoh_` önceden tanımlıdır. İngilizce viral başlık, açıklama, etiket ve altyazı üretip **Global YouTube + Global Instagram** hesaplarına yollar.
2. **Hem Canlı Yayın Hem Geçmiş Klipler:**
   - **Canlı Yayın Yakalayıcısı:** Yayında olan kanallara otomatik bağlanır ve sürekli son **2 dakikalık** video halkasını (`ring buffer`) tutar. Ses/reaksiyon patlaması veya sohbet (`chat`) patlaması algıladığında son **~40 saniyeyi** anında kesip işler.
   - **Trend & Geçmiş Klip Tarayıcısı:** Seçtiğin yayıncıların son kliplerini izlenme hızına (`izlenme / dakika`) göre tarar; aynı klibi asla iki kez işlemez.
3. **3 Katmanlı Akıllı Kalite Kapısı (Sessiz / Boş Koltuk / AFK Engeli):**
   - **Katman 1 — Ses & Konuşma (`FFmpeg silencedetect` + `faster-whisper`):** Sessiz kesitleri ve yayıncının hiç konuşmadığı klipleri indirir indirmez eler.
   - **Katman 2 — Görsel Hareket & Boş Koltuk (`OpenCV`):** Duraklatılmış oyun, BRB ekranı veya yayıncının tuvalete/suya gidip kamerada koltuğu boş bıraktığı anları tespit edip reddeder.
   - **Katman 3 — Çok Modlu AI Editör (`Gemini` / `OpenAI`):** Klibin 6 karesini ve konuşma metnini inceleyip `0–10` puan verir, en iyi 28–45 saniyeyi seçer ve YouTube/Instagram metinlerini hazırlar.
4. **Otomatik 9:16 Dikey Kurgu & Altyazı:**
   - `1080x1920` formatında bulanık arka plan + net oyun/kamera kadrajı + yüksek ses normalizasyonu (`-14 LUFS`) + ortalanmış dinamik altyazı (`.ass`) oluşturur.
5. **Telegram + Web + Telefon (PWA) Kontrolü:**
   - Her hazır video önce senin **Telegram**'ına videolu önizleme ve `[✅ YT + IG Paylaş]` / `[▶️ Sadece YT]` / `[📸 Sadece IG]` / `[🔄 Başlık Yenile]` / `[❌ Reddet]` butonlarıyla düşer (istersen **Otopilot** açıp hiç sormadan yayınlatabilirsin).

---

## Kurulum (Arkadaşlarının Bilgisayarı Dahil)

### 1. Windows (Tek Tık)
> Gereksinimler: Python 3.11+, FFmpeg (`winget install Gyan.FFmpeg`), isteğe bağlı Node.js (n8n için).
```bat
git clone https://github.com/CoskunerBerke/n8n.git
cd n8n
install.bat
start.bat
```

### 2. macOS ve Linux
```bash
git clone https://github.com/CoskunerBerke/n8n.git
cd n8n
chmod +x install.sh start.sh
./install.sh
./start.sh
```

### 3. Docker (En Temiz ve Sorunsuz Yol — Windows / Mac / Linux / Sunucu)
```bash
git clone https://github.com/CoskunerBerke/n8n.git
cd n8n
cp .env.example .env
docker compose up -d --build
```
- **Web & Telefon Paneli:** `http://localhost:8000`
- **n8n Otomasyon Paneli:** `http://localhost:5678`

---

## Telefondan ve Web'den Kullanım

1. **Aynı Wi-Fi Üzerinden:** `start.bat` veya `./start.sh` açıldığında ekranda `Telefon (Ayni Wi-Fi): http://192.168.x.x:8000` adresi yazar. Telefondan bu adresi açıp **"Ana Ekrana Ekle"** diyerek uygulama gibi kullanabilirsin.
2. **Dışarıdan (4G/5G ile Her Yerden):** Docker kullanıyorsan şu komutu çalıştır:
   ```bash
   docker compose --profile mobile up -d
   docker compose logs cloudflared
   ```
   Çıkan `https://xxxx.trycloudflare.com` adresini telefondan açabilir ve aynı adresi `.env` içindeki `PUBLIC_BASE_URL=` kısmına yazarak Instagram Reels yüklemelerini de sorunsuz çalıştırabilirsin.
3. **Telegram Bot Komutları:**
   - `/durum` — Canlı yayınlar ve günlük istatistikler
   - `/tara tr` veya `/tara global` — Hemen tarama başlat
   - `/klip tr <link>` veya `/klip global <link>` — Telefondan attığın linki 9:16 kesip hazırlar
   - `/yayinci_ekle tr kick rraenee` — Telefondan yayıncı ekler
   - `/autopilot tr on` — Otopilot modunu açıp kapatır

---

## API Anahtarlarını Nereden Alacaksınız?

1. **Gemini API Key (Ücretsiz Katman):** Google AI Studio (`aistudio.google.com`) üzerinden oluşturup Web Panel > `04 Kurulum` bölümüne yapıştırın.
2. **Telegram Bot:** Telegram'da `@BotFather` -> `/newbot` ile token alın. Botunuza `/start` yazın; bot Chat ID'nizi otomatik ekrana yazdıracaktır.
3. **YouTube Shorts (TR & Global):** Google Cloud Console'dan `YouTube Data API v3` etkinleştirip `OAuth 2.0 Client ID (Web application)` oluşturun, redirect URI olarak `http://localhost:8000/api/auth/youtube/callback` ekleyin, JSON dosyasını Web Panelden yükleyin ve `TR` ile `Global` için ayrı ayrı **YouTube Bağla** butonuna basın.
4. **Instagram Reels (TR & Global):** Instagram hesaplarınızı ücretsiz İçerik Üretici/İşletme hesabına geçirip bir Facebook sayfasına bağlayın; Meta Developers üzerinden `instagram_content_publish` izniyle `User ID` ve `Access Token` alıp panele kaydedin.
5. **Twitch & Kick API (Canlı Yayın & Chat Takibi):**
   - Twitch: `dev.twitch.tv/console` -> Uygulama oluştur (`Client ID` + `Client Secret`).
   - Kick: Kick hesabında `Settings > Developer` -> Uygulama oluştur (`Client ID` + `Client Secret`).
6. **n8n İş Akışları:** `http://localhost:5678` adresine girip `n8n/workflows/` altındaki 3 JSON dosyasını **Import from File** ile ekleyin ve aktif edin.
