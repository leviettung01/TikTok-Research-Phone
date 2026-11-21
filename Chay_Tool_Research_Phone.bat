@echo off
chcp 65001 >nul
title TikTok Research ^& Phone Studio (Port 9098 - Doc Lap)
cd /d "%~dp0"

rem Giải phóng cổng 9098 nếu phiên cũ còn chạy
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":9098 .*LISTENING"') do taskkill /F /PID %%p >nul 2>&1

set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY (
  for %%v in (Python313 Python312 Python311 Python310) do (
    if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\%%v\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\%%v\python.exe"
  )
)
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  where py >nul 2>&1 && set "PY=py"
)
if not defined PY (
  echo [LOI] Chua cai dat Python 3.10+ tren may tinh nay!
  echo Vui long tai va cai Python tai: https://www.python.org/downloads/ ^( nho tick "Add Python to PATH" ^)
  pause
  exit /b 1
)

set PYTHONIOENCODING=utf-8
"%PY%" -c "import requests, openpyxl, qrcode, PIL" >nul 2>&1
if errorlevel 1 (
  echo [*] Dang tu dong cai dat thu vien Python can thiet cho lan dau chay...
  "%PY%" -m pip install --upgrade pip --quiet >nul 2>&1
  "%PY%" -m pip install -r "%~dp0requirements.txt"
)

"%PY%" "%~dp0server.py" %*
if errorlevel 1 (
  echo.
  echo [LOI] Tool dung voi ma loi %errorlevel%. Xem thong bao phia tren.
  pause
)
