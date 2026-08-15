@echo off
rem Start SUNI (Windows). POSIX counterpart: start_suni.sh
rem
rem Paths are resolved from this script's own location (%~dp0), so a clone in
rem any directory works — no absolute path is baked in.
rem
rem Credentials are loaded from .env by python-dotenv, never hardcoded here.
rem Every value below is a DEFAULT: anything already set in the environment
rem (or in .env) wins, so this file should not need editing.

cd /d "%~dp0"
if not exist logs mkdir logs
echo [%date% %time%] Starting SUNI... >> logs\startup.log

rem Interpreter: explicit override, then the project venv, then the system
rem Python (py launcher first — it resolves the newest install).
set "SUNI_PY=%SUNI_PYTHON%"
if not defined SUNI_PY if exist "%~dp0.venv\Scripts\python.exe" set "SUNI_PY=%~dp0.venv\Scripts\python.exe"
if not defined SUNI_PY if exist "C:\Python312\python.exe" set "SUNI_PY=C:\Python312\python.exe"
if not defined SUNI_PY (
    where py >nul 2>&1 && set "SUNI_PY=py -3"
)
if not defined SUNI_PY set "SUNI_PY=python"

rem No model is set here. Which model to run is configured in the admin panel
rem (Configuration - Model) and persists in SUNI's own config; the launcher
rem pinning it would silently override whatever the admin chose. With nothing
rem configured, SUNI uses the best model installed at the core tier.
rem
rem Ollama / GPU tuning belongs to the host, so set it in .env if the defaults
rem do not suit your hardware. See .env.example.

rem Print the resolved setup and exit, so a launcher can be checked without
rem starting a second instance against the same data directory.
if defined SUNI_DRYRUN (
    echo python : %SUNI_PY%
    echo workdir: %CD%
    echo model  : resolved by SUNI - admin config, else best installed
    exit /b 0
)

%SUNI_PY% web.py >> logs\startup.log 2>&1
