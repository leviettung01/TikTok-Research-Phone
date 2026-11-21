@echo off
chcp 65001 >nul
title Cai Dat Moi Truong - TikTok Research ^& Phone Studio
cd /d "%~dp0"

echo ================================================================
echo   CAI DAT MOI TRUONG CHO MAY MOI - TIKTOK RESEARCH ^& PHONE
echo ================================================================
echo.

set "BASE_PY="
for %%v in (Python313 Python312 Python311 Python310) do (
  if not defined BASE_PY if exist "%LOCALAPPDATA%\Programs\Python\%%v\python.exe" set "BASE_PY=%LOCALAPPDATA%\Programs\Python\%%v\python.exe"
)
if not defined BASE_PY (
  where python >nul 2>&1 && set "BASE_PY=python"
)
if not defined BASE_PY (
  where py >nul 2>&1 && set "BASE_PY=py"
)

if not defined BASE_PY (
  echo [LOI] Khong tim thay Python 3.10+ tren may tinh!
  echo Hay cai Python 3.11+ tu https://www.python.org/downloads/ ^(tick "Add Python to PATH"^) roi chay lai file nay.
  pause
  exit /b 1
)

echo [1/3] Dang tao moi truong ao .venv...
if not exist "%~dp0.venv\Scripts\python.exe" (
  "%BASE_PY%" -m venv "%~dp0.venv"
)

echo [2/3] Dang cai dat cac thu vien trong requirements.txt...
"%~dp0.venv\Scripts\python.exe" -m pip install --upgrade pip
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"

echo [3/3] Kiem tra bo cong cu ADB ^& scrcpy dong goi san...
if exist "%~dp0tools\adb.exe" (
  echo   [OK] Da co san tools\adb.exe + scrcpy-server + ADBKeyboard.apk
) else (
  echo   [CANH BAO] Thieu tools\adb.exe!
)

echo.
echo ================================================================
echo   HOAN TAT! Tu bay gio chi can mo Chay_Tool_Research_Phone.bat
echo ================================================================
pause
