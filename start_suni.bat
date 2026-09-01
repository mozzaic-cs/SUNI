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
rem Refuse to start a SECOND instance against the same data directory.
rem
rem The Windows scheduled task retries on a repeating trigger so an outage
rem cannot last days (see docs). That is only safe if starting when SUNI is
rem already up is a no-op: two processes on one memory/ directory means two
rem writers on the same SQLite files, and the loser of the port bind dies noisily
rem while the operator sees a task that "ran fine".
rem
rem The port is the check because it is the thing that is actually exclusive.
rem SUNI_PORT from the environment wins; otherwise read it from .env (which
rem web.py itself loads), else the 8765 default — so the guard follows the port
rem the server will really use rather than a guess.
rem
rem The notice goes to launcher.log, NOT startup.log: the running server
rem holds startup.log open as its stdout, so a write there fails with
rem "used by another process" in precisely the case this branch handles.
if not defined SUNI_PORT if exist ".env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
        if /i "%%a"=="SUNI_PORT" set "SUNI_PORT=%%b"
    )
)
if not defined SUNI_PORT set "SUNI_PORT=8765"
set "SUNI_PORT=%SUNI_PORT: =%"

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
    netstat -ano | findstr /r /c:":%SUNI_PORT% .*LISTENING" >nul 2>&1
    if errorlevel 1 (echo port   : %SUNI_PORT% [free - would start]) else (echo port   : %SUNI_PORT% [IN USE - would decline])
    echo model  : resolved by SUNI - admin config, else best installed
    exit /b 0
)

netstat -ano | findstr /r /c:":%SUNI_PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [%date% %time%] Already listening on %SUNI_PORT% - not starting a second instance >> logs\launcher.log 2>nul
    exit /b 0
)

rem Where to log. Normally logs\startup.log — but a SUNI that was KILLED (rather
rem than stopped) can leave orphaned multiprocessing children behind, and those
rem inherit the log handle and hold the file open. Windows then refuses the
rem append, cmd aborts, and the relaunch dies before Python is ever reached.
rem
rem That is not a corner case: it is the exact state an automatic restart finds
rem after the crash it exists to recover from. Observed on 01/09/2026 — two
rem orphans of a force-killed server blocked every restart attempt, silently.
rem So a locked log must never be fatal: fall back to a second file and start.
rem Detected by actually opening the file for append. A failed REDIRECT does
rem not set errorlevel — verified, not assumed — so `(echo x >> locked) 2>nul`
rem followed by `if errorlevel 1` silently does nothing and the launcher dies
rem anyway. Opening it explicitly is the check that reports the truth.
set "SUNI_LOG=logs\startup.log"
powershell -NoProfile -Command "try{$f=[IO.File]::Open('%CD%\logs\startup.log','Append','Write','Read');$f.Close();exit 0}catch{exit 1}" >nul 2>&1
if errorlevel 1 (
    set "SUNI_LOG=logs\startup-alt.log"
    echo [%date% %time%] startup.log is LOCKED - a killed SUNI may have left orphaned child processes holding it. Logging here instead. >> "logs\startup-alt.log" 2>nul
)
echo [%date% %time%] Starting SUNI... >> "%SUNI_LOG%" 2>nul

%SUNI_PY% web.py >> "%SUNI_LOG%" 2>&1
