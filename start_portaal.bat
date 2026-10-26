@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo De virtuele Python-omgeving is niet gevonden.
    echo Voer eerst de installatie uit zoals beschreven in README.md.
    pause
    exit /b 1
)

rem Start volledig losgekoppeld zonder consolevenster; de server stopt automatisch als de browser sluit.
".venv\Scripts\pythonw.exe" app.py --spawn-detached
exit
