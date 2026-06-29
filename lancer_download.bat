@echo off
cd /d "%~dp0"

if not exist data mkdir data

if "%1"=="recent" (
    echo Refresh recent uniquement...
    python scripts\download_data.py --refresh-only-recent
) else (
    echo Refresh complet...
    python scripts\download_data.py
)

echo.
echo ===============================================
echo Termine. Appuyez sur une touche pour fermer.
echo ===============================================
pause
