@echo off
chcp 65001 >nul
setlocal

title JAVIP AI STORE - Import 9router v2.0
cd /d "%~dp0"

echo.
echo  ╔════════════════════════════════════════════════╗
echo  ║   JAVIP AI STORE - Import 9router v2.0       ║
echo  ║   Auto Login OAuth + Bulk Import 9router      ║
echo  ╚════════════════════════════════════════════════╝
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo  [!] Khong tim thay Python.
    echo  [!] Cai Python truoc: https://www.python.org/downloads/
    echo      Nho tick "Add python.exe to PATH" khi cai.
    echo.
    pause
    exit /b 1
)

echo  [1/4] Kiem tra pip...
python -m ensurepip --upgrade >nul 2>nul
python -m pip --version >nul 2>nul
if errorlevel 1 (
    echo  [!] May nay chua co pip hoac pip bi loi.
    echo      Hay cai lai Python va tick "pip" + "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

echo  [2/4] Cai/cap nhat pyotp + playwright...
python -m pip install --upgrade pyotp playwright
if errorlevel 1 (
    echo.
    echo  [!] Loi khi cai pyotp/playwright.
    echo      Thu chay thu cong: python -m pip install --upgrade pyotp playwright
    echo.
    pause
    exit /b 1
)

echo.
echo  [3/4] Cai Chromium cho Playwright neu chua co...
python -m playwright install chromium
if errorlevel 1 (
    echo.
    echo  [!] Loi khi cai Chromium Playwright.
    echo      Thu chay thu cong: python -m playwright install chromium
    echo.
    pause
    exit /b 1
)

echo.
echo  [4/4] Dang khoi dong server...
echo.
python "%~dp0server.py"
if errorlevel 1 (
    echo.
    echo  [!] Server bi loi. Kiem tra log ben tren.
    echo.
    pause
)
