@echo off
REM Diagnostic : verifie environnement Python et dossier
cd /d "%~dp0"

echo ===============================================
echo DIAGNOSTIC ENVIRONNEMENT
echo ===============================================
echo.
echo Dossier courant : %CD%
echo.

echo --- Test 1 : Python installe ? ---
python --version
if errorlevel 1 (
    echo ERREUR : Python non trouve. Installer Python depuis python.org
    goto end
)
echo.

echo --- Test 2 : Modules requis ---
python -c "import pandas; print('pandas', pandas.__version__)"
python -c "import numpy; print('numpy', numpy.__version__)"
python -c "import matplotlib; print('matplotlib', matplotlib.__version__)"
python -c "import streamlit; print('streamlit', streamlit.__version__)" 2>nul
if errorlevel 1 echo streamlit : NON installe (necessaire pour dashboard)
echo.

echo --- Test 3 : Structure dossier ---
if exist src\strategy.py (echo OK src\strategy.py) else (echo MANQUE src\strategy.py)
if exist scripts\backtest.py (echo OK scripts\backtest.py) else (echo MANQUE scripts\backtest.py)
if exist scripts\download_data.py (echo OK scripts\download_data.py) else (echo MANQUE scripts\download_data.py)
if exist config\parameters.json (echo OK config\parameters.json) else (echo MANQUE config\parameters.json)
if exist data\ (echo OK dossier data\) else (echo MANQUE dossier data\)
if exist dashboard\app.py (echo OK dashboard\app.py) else (echo MANQUE dashboard\app.py)
echo.

echo --- Test 4 : Donnees telechargees ? ---
if exist data\XNDX.csv (echo OK data\XNDX.csv) else (echo MANQUE data\XNDX.csv - lancer download_data.py d'abord)
if exist data\SPXTR.csv (echo OK data\SPXTR.csv) else (echo MANQUE data\SPXTR.csv)
if exist data\GLD.csv (echo OK data\GLD.csv) else (echo MANQUE data\GLD.csv)
if exist data\TLT.csv (echo OK data\TLT.csv) else (echo MANQUE data\TLT.csv)
if exist data\CL.csv (echo OK data\CL.csv) else (echo MANQUE data\CL.csv)
echo.

echo --- Test 5 : Import strategy ---
python -c "import sys; sys.path.insert(0, '.'); from src.strategy import load_config; print('OK import strategy')"
echo.

:end
echo ===============================================
echo Appuyez sur une touche pour fermer
echo ===============================================
pause
