@echo off
title Demarrage PresenceApp
echo ========================================================
echo    Lancement de l'Application de Gestion de Presence
echo ========================================================
echo.

cd /d "%~dp0"

echo Verification du serveur...
timeout /t 2 >nul

:: Ouvrir automatiquement le navigateur sur l'adresse fixe
start http://localhost:8080

echo Application demarree !
echo Vous pouvez y acceder sur ce PC : http://localhost:8080
echo.
echo Pour arreter l'application, fermez simplement cette fenetre.
echo ========================================================
echo.

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" app.py
) else (
    python app.py
)
pause
