@echo off
cd /d "%~dp0"
chcp 65001 >nul
setlocal EnableDelayedExpansion

if exist ".venv\Scripts\python.exe" goto :check_env
echo [UYARI] .venv bulunamadi, once install.bat calistiriliyor...
call install.bat
if errorlevel 1 goto :exit_pause

:check_env
if exist ".env" goto :detect_ip
copy /Y ".env.example" ".env" >nul

:detect_ip
set "LAN_IP=localhost"
for /f "usebackq delims=" %%i in (`.venv\Scripts\python.exe -c "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); print(s.getsockname()[0]); s.close()" 2^>nul`) do set "LAN_IP=%%i"
if "!LAN_IP!"=="" set "LAN_IP=localhost"

echo ============================================================
echo   GTA 6 Auto-Clipper - TR ve GLOBAL Baslatiliyor
echo ============================================================
echo   Bilgisayar Paneli   : http://localhost:8000
echo   Telefon Ayni Wi-Fi  : http://!LAN_IP!:8000
echo   n8n Otomasyon       : http://localhost:5678
echo ============================================================
echo.

where npx >nul 2>&1
if errorlevel 1 goto :no_npx
echo [n8n] Arka planda baslatiliyor...
start "GTA6-n8n" /D "%~dp0" cmd /k "set N8N_PORT=5678 && npx --yes n8n"
goto :run_engine

:no_npx
echo [BILGI] Node/npx bulunamadi; sadece Clipper Motoru baslatiliyor, dahili zamanlayici aktif.

:run_engine
echo [Clipper Engine] http://0.0.0.0:8000 uzerinde baslatiliyor...
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:8000"
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000

:exit_pause
echo.
echo [BILGI] Sunucu durduruldu. Cikmak icin bir tusa basin...
pause
