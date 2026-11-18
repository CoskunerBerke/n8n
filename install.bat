@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

echo ============================================================
echo   GTA 6 Dual-Pipeline Auto-Clipper (TR + GLOBAL) Kurulum
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [HATA] Python bulunamadi! Lutfen Python 3.11+ kurun: https://www.python.org/downloads/
    pause
    exit /b 1
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo [UYARI] FFmpeg PATH uzerinde bulunamadi.
    echo Windows icin: winget install Gyan.FFmpeg
    echo Kurduktan sonra terminali yeniden acin.
)

if not exist ".venv" (
    echo [1/4] Sanal ortam (.venv) olusturuluyor...
    python -m venv .venv
    if errorlevel 1 (
        echo [HATA] .venv olusturulamadi.
        pause
        exit /b 1
    )
) else (
    echo [1/4] Mevcut .venv bulundu.
)

echo [2/4] Pip guncelleniyor...
.venv\Scripts\python.exe -m pip install --upgrade pip

echo [3/4] Python bagimliliklari yukleniyor...
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo [HATA] Bagimlilik yuklemesi basarisiz oldu.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [4/4] .env dosyasi .env.example dosyasindan olusturuluyor...
    copy /Y ".env.example" ".env" >nul
) else (
    echo [4/4] .env dosyasi zaten mevcut, korunuyor.
)

if not exist "data" mkdir data
if not exist "credentials" mkdir credentials

echo.
echo [TAMAM] Kurulum basariyla tamamlandi!
echo Simdi start.bat dosyasini calistirarak sistemi baslatabilirsiniz.
pause
