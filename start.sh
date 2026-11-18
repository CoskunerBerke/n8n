#!/usr/bin/env bash
set -euo pipefail

if [ ! -x ".venv/bin/python" ]; then
  echo "[UYARI] .venv bulunamadi, once install.sh calistiriliyor..."
  ./install.sh
fi

if [ ! -f ".env" ]; then
  cp .env.example .env
fi

LAN_IP="localhost"
if command -v ip >/dev/null 2>&1; then
  LAN_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -n 1 || true)
elif command -v ipconfig >/dev/null 2>&1; then
  LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)
fi
LAN_IP=${LAN_IP:-localhost}

N8N_PID=""
cleanup() {
  echo ""
  echo "Servisler durduruluyor..."
  if [ -n "$N8N_PID" ] && kill -0 "$N8N_PID" 2>/dev/null; then
    kill "$N8N_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "============================================================"
echo "  GTA 6 Auto-Clipper (TR + GLOBAL) Baslatiliyor"
echo "============================================================"
echo "  Bilgisayar Paneli   : http://localhost:8000"
echo "  Telefon (Ayni Wi-Fi): http://${LAN_IP}:8000"
echo "  n8n Otomasyon       : http://localhost:5678"
echo "============================================================"

if command -v npx >/dev/null 2>&1; then
  echo "[n8n] Arka planda baslatiliyor..."
  N8N_PORT=5678 npx --yes n8n >/tmp/gta6_n8n.log 2>&1 &
  N8N_PID=$!
else
  echo "[BILGI] npx bulunamadi; sadece Clipper Motoru baslatiliyor (dahili zamanlayici aktif)."
fi

exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
