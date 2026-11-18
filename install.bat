@echo off
cd /d "%~dp0"
chcp 65001 >nul
setlocal EnableDelayedExpansion

echo ============================================================
echo   GTA 6 Dual-Pipeline Auto-Clipper - TR ve GLOBAL Kurulum
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 goto :no_python

where ffmpeg >nul 2>&1
if not errorlevel 1 goto :check_venv
echo [UYARI] FFmpeg PATH uzerinde bulunamadi.
echo Windows icin: winget install Gyan.FFmpeg
echo Kurduktan sonra terminali yeniden acin.

:check_venv
if exist ".venv\Scripts\python.exe" goto :venv_exists
echo [1/4] Sanal ortam olusturuluyor...
python -m venv .venv
if errorlevel 1 goto :venv_err
goto :upgrade_pip

:venv_exists
echo [1/4] Mevcut .venv bulundu.

:upgrade_pip
echo [2/4] Pip guncelleniyor...
".venv\Scripts\python.exe" -m pip install --upgrade pip

echo [3/4] Python bagimliliklari yukleniyor...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :pip_err

if exist ".env" goto :env_exists
echo [4/4] .env dosyasi .env.example dosyasindan olusturuluyor...
copy /Y ".env.example" ".env" >nul
goto :make_dirs

:env_exists
echo [4/4] .env dosyasi zaten mevcut, korunuyor.

:make_dirs
if not exist "data" mkdir data
if not exist "credentials" mkdir credentials

echo.
echo [TAMAM] Kurulum basariyla tamamlandi!
echo Simdi start.bat dosyasini calistirarak sistemi baslatabilirsiniz.
pause
exit /b 0

:no_python
echo [HATA] Python bulunamadi! Lutfen Python 3.11+ kurun: https://www.python.org/downloads/
pause
exit /b 1

:venv_err
echo [HATA] .venv olusturulamadi.
pause
exit /b 1

:pip_err
echo [HATA] Bagimlilik yuklemesi basarisiz oldu.
pause
exit /b 1
