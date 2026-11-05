@echo off
set PYTHONIOENCODING=utf-8
cd /d C:\CODE\crypto-trend
if not exist logs mkdir logs
echo ===== %date% %time% start >> logs\live.log
"C:\Users\tim05\AppData\Local\Programs\Python\Python312\python.exe" live_trader.py >> logs\live.log 2>&1
