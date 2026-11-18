#!/usr/bin/env bash
set -euo pipefail

echo "============================================================"
echo "  GTA 6 Dual-Pipeline Auto-Clipper (TR + GLOBAL) Kurulum"
echo "============================================================"

PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  echo "[HATA] Python 3.11+ bulunamadi. Lutfen Python kurun."
  exit 1
fi
echo "[OK] Python: $("$PYTHON_BIN" --version)"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "[UYARI] FFmpeg bulunamadi!"
  if [[ "$OSTYPE" == "darwin"* ]]; then
    if command -v brew >/dev/null 2>&1; then
      echo "macOS Homebrew tespit edildi. FFmpeg kuruluyor..."
      brew install ffmpeg
    else
      echo "Lutfen once Homebrew ile kurun: brew install ffmpeg"
    fi
  else
    echo "Linux kurulumu icin:"
    echo "  Ubuntu/Debian: sudo apt update && sudo apt install -y ffmpeg fonts-dejavu-core"
    echo "  Fedora:        sudo dnf install -y ffmpeg dejavu-sans-fonts"
    echo "  Arch:          sudo pacman -S ffmpeg ttf-dejavu"
  fi
else
  echo "[OK] FFmpeg: $(ffmpeg -version | head -n 1)"
fi

if [ ! -d ".venv" ]; then
  echo "[1/3] Sanal ortam (.venv) olusturuluyor..."
  "$PYTHON_BIN" -m venv .venv
else
  echo "[1/3] Mevcut .venv bulundu."
fi

echo "[2/3] Python bagimliliklari yukleniyor..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [ ! -f ".env" ]; then
  echo "[3/3] .env dosyasi .env.example uzerinden olusturuluyor..."
  cp .env.example .env
else
  echo "[3/3] .env dosyasi zaten mevcut, korunuyor."
fi

mkdir -p data credentials

chmod +x start.sh install.sh

echo ""
echo "[TAMAM] Kurulum tamamlandi!"
echo "Baslatmak icin: ./start.sh"
