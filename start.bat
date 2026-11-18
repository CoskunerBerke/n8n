@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

if not exist ".venv\Scripts\python.exe" (
    echo [UYARI] .venv bulunamadi, once install.bat calistiriliyor...
    call install.bat
)

if not exist ".env" (
    copy /Y ".env.example" ".env" >nul
)

for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /R /C:"IPv4 Address" /C:"IPv4 Adresi"') do (
    set "RAW_IP=%%a"
    set "LAN_IP=!RAW_IP: =!"
    goto :got_ip
)
:got_ip
if "!LAN_IP!"=="" set "LAN_IP=localhost"

echo ============================================================
echo   GTA 6 Auto-Clipper (TR + GLOBAL) Baslatiliyor
echo ============================================================
echo   Bilgisayar Paneli : http://localhost:8000
echo   Telefon (Ayni Wi-Fi): http://!LAN_IP!:8000
echo   n8n Otomasyon     : http://localhost:5678
echo ============================================================
echo.

where npx >nul 2>&1
if not errorlevel 1 (
    echo [n8n] Arka planda baslatiliyor...
    start "GTA6-n8n" cmd /k "set N8N_PORT=5678 && npx --yes n8n"
) else (
    echo [BILGI] Node/npx bulunamadi; sadece Clipper Motoru baslatiliyor (dahili zamanlayici aktif).
)

echo [Clipper Engine] http://0.0.0.0:8000 uzerinde baslatiliyor...
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
