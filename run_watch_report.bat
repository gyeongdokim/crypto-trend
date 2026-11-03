@echo off
set PYTHONIOENCODING=utf-8
cd /d C:\CODE\crypto-trend
if not exist logs mkdir logs
echo ===== %date% %time% %~nx0 start >> logs\watch.log
"C:\Users\tim05\AppData\Local\Programs\Python\Python312\python.exe" market_watch.py --report >> logs\watch.log 2>&1
