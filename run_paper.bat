@echo off
set PYTHONIOENCODING=utf-8
cd /d C:\CODE\crypto-trend
if not exist logs mkdir logs
echo ===== %date% %time% start >> logs\paper.log
"C:\Users\tim05\AppData\Local\Programs\Python\Python312\python.exe" paper_trader.py >> logs\paper.log 2>&1
