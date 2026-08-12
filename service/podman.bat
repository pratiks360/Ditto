@echo off
REM Build and run the knowledge service under Podman on Windows.
REM
REM   podman.bat up      build (if needed) and start
REM   podman.bat down    stop and remove
REM   podman.bat logs    follow the log
REM   podman.bat rebuild force a fresh image
REM
REM Your knowledge base stays on the host at %USERPROFILE%\.jobkb and is bind
REM mounted in, so it survives every rebuild and stays editable in Notepad.

setlocal
cd /d "%~dp0"

set IMAGE=jobkb
set NAME=jobkb
REM The knowledge base lives in the project so it can be version controlled.
REM Set JOBKB_DATA_DIR in .env to keep it somewhere else.
set DATA=%~dp0..\kb

where podman >nul 2>&1 || goto :nopodman

podman machine inspect >nul 2>&1 || goto :nomachine
podman info >nul 2>&1 || goto :nomachine

if "%1"=="down" (
  podman rm -f %NAME%
  goto :eof
)
if "%1"=="logs" (
  podman logs -f %NAME%
  goto :eof
)
if "%1"=="rebuild" (
  podman rm -f %NAME% 2>nul
  podman build --no-cache --format docker -t %IMAGE% -f Containerfile . || goto :eof
  goto :run
)

podman image exists %IMAGE% || (
  echo Building %IMAGE% ...
  podman build --format docker -t %IMAGE% -f Containerfile . || goto :eof
)

:run
if not exist "%DATA%" mkdir "%DATA%"

if "%OPENROUTER_API_KEY%"=="" (
  echo.
  echo   OPENROUTER_API_KEY is not set. The service will still fill everything
  echo   it can match from storage, but cannot route or draft answers.
  echo   Fix with:  setx OPENROUTER_API_KEY sk-or-v1-...   then open a new window.
  echo.
)

podman rm -f %NAME% >nul 2>&1

REM .env next to this script, if you made one. See .env.example.
set ENVFILE=
if exist ".env" set ENVFILE=--env-file .env

REM An explicit -e beats --env-file, so forwarding OPENROUTER_API_KEY from the
REM shell would overwrite the one in .env -- with an empty string when the shell
REM does not have it set. Only forward it when .env does not define it.
set KEYARG=
if defined OPENROUTER_API_KEY set KEYARG=-e OPENROUTER_API_KEY=%OPENROUTER_API_KEY%
if exist ".env" (
  findstr /b /i "OPENROUTER_API_KEY=" .env >nul 2>&1 && set KEYARG=
)
if not defined KEYARG (
  if not exist ".env" (
    echo.
    echo   No OPENROUTER_API_KEY anywhere. The service will still fill what it
    echo   can match from storage, but cannot route or draft answers.
    echo   Put it in .env - see .env.example.
    echo.
  )
)

REM Read JOBKB_RESUME_DIR out of .env so the folder holding your resume can be
REM mounted. It is a Windows path used here on the host; the JOBKB_RESUME and
REM JOBKB_RESUME_FILE values inside .env are container paths under /resume.
set RESUMEDIR=
if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    if /i "%%a"=="JOBKB_RESUME_DIR" set RESUMEDIR=%%b
  )
)
set RESUMEMOUNT=
if defined RESUMEDIR (
  if exist "%RESUMEDIR%" (
    set RESUMEMOUNT=-v "%RESUMEDIR%:/resume:ro"
  ) else (
    echo   JOBKB_RESUME_DIR is set to "%RESUMEDIR%" but that folder does not exist.
  )
)

REM -p 127.0.0.1:8765:8765 publishes to loopback only. Dropping that prefix
REM would put your employment history on every network interface.
podman run -d --name %NAME% ^
  -p 127.0.0.1:8765:8765 ^
  -v "%DATA%:/data" ^
  %RESUMEMOUNT% ^
  %ENVFILE% ^
  %KEYARG% ^
  --restart unless-stopped ^
  %IMAGE% || goto :eof

echo.
echo   started. knowledge base: %DATA%
echo   health: curl http://127.0.0.1:8765/health
echo   logs  : podman.bat logs
goto :eof

:nopodman
echo.
echo   Podman was not found on PATH. Install Podman Desktop from
echo   https://podman-desktop.io and reopen this window.
echo.
pause
goto :eof

:nomachine
echo.
echo   The Podman machine is not running. Start it with:
echo       podman machine start
echo   (first time only:  podman machine init)
echo.
pause
goto :eof
