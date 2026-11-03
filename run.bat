@echo off
setlocal
rem Start DE-AI on Windows. The first run sets everything up (takes a few minutes).
cd /d "%~dp0"
title DE-AI

rem Use an ffmpeg that isn't on PATH yet (e.g. C:\Users\you\ffmpeg)
for %%D in ("%USERPROFILE%\ffmpeg" "%USERPROFILE%\ffmpeg\bin" "C:\ffmpeg\bin") do (
  if exist "%%~D\ffmpeg.exe" set "PATH=%%~D;%PATH%"
)

if exist ".venv\Scripts\python.exe" goto run

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY ( where python >nul 2>&1 && set "PY=python" )
if not defined PY (
  echo.
  echo Python is not installed. Install it with:  winget install Python.Python.3.12
  echo or from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^), then run this again.
  pause
  exit /b 1
)

echo Setting up DE-AI for the first time...
%PY% -m venv .venv
if not exist ".venv\Scripts\python.exe" (
  echo Couldn't create the Python environment. Check that Python 3.10 or newer is installed.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -U pip
".venv\Scripts\python.exe" -m pip install -r requirements-full.txt
if errorlevel 1 (
  echo Installing the packages failed. Check your internet connection and run this again.
  rmdir /s /q .venv
  pause
  exit /b 1
)
rem Deep scan can also use your installed Google Chrome, so this step is optional.
".venv\Scripts\python.exe" -m playwright install chromium

:run
echo Checking for updates...
".venv\Scripts\python.exe" -m pip install -q -r requirements-full.txt >nul 2>&1
".venv\Scripts\python.exe" -m pip install -q -U "yt-dlp[default]" >nul 2>&1
where ffmpeg >nul 2>&1 || echo Tip: install ffmpeg for HD video and MP3:  winget install Gyan.FFmpeg
echo.
echo DE-AI is running at http://localhost:8000  ^(close this window to stop it^)
start "" http://localhost:8000
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
