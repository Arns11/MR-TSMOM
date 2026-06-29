@echo off
cd /d "%~dp0"

echo Lancement dashboard Combo XNDX MR + TSMOM...
echo.

python -c "import streamlit" 2>nul
if errorlevel 1 (
    echo Installation streamlit en cours...
    pip install streamlit pandas numpy matplotlib
)

streamlit run dashboard\app.py

echo.
echo ===============================================
echo Si erreur, appuyez sur une touche pour fermer.
echo ===============================================
pause
