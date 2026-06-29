@echo off
REM Lanceur backtest Windows
cd /d "%~dp0"

if not exist results mkdir results

echo Lancement backtest...
python scripts\backtest.py > results\backtest_log.txt 2>&1
echo Termine. Voir results\backtest_log.txt
notepad results\backtest_log.txt
pause
