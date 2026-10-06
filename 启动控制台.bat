@echo off
cd /d "%~dp0"
netstat -ano | findstr "LISTENING" | findstr ":8734" >nul 2>&1
if %errorlevel%==0 (
  echo Console is already running, opening browser...
  start "" "http://127.0.0.1:8734/"
) else (
  echo Starting Douyin Auto Fire console...
  start "" /min ".venv\Scripts\python.exe" web_server.py
  timeout /t 2 /nobreak >nul
  start "" "http://127.0.0.1:8734/"
)
