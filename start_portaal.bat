@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo De virtuele Python-omgeving is niet gevonden.
    echo Voer eerst de installatie uit zoals beschreven in README.md.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" app.py
if errorlevel 1 (
    echo.
    echo Het GitHub Software Portaal is gestopt met een fout.
)
pause
