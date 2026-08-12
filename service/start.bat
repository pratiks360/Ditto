@echo off
REM Starts the job knowledge service. Double-click, or run from a terminal.
REM Set your key once with:  setx OPENROUTER_API_KEY sk-or-v1-...

cd /d "%~dp0"
setlocal EnableExtensions

REM One config file for both ways of running: .env next to this script.
REM Values here win over anything already in the environment, so a stale
REM OPENROUTER_API_KEY left in a shell cannot quietly shadow the real one.
REM `eol=#` skips comments; `delims==` splits on the first = only.
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
    if not "%%~a"=="" if not "%%~b"=="" set "%%a=%%b"
  )
  echo   loaded .env
)

if not exist ".venv\Scripts\python.exe" (
  echo No virtual environment found. Creating one...
  py -m venv .venv || goto :nopython
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)

if "%OPENROUTER_API_KEY%"=="" (
  echo.
  echo   OPENROUTER_API_KEY is not set. The service will still run and fill
  echo   everything it can match locally, but it cannot route or draft answers.
  echo   Put it in .env next to this script - see .env.example.
  echo.
)

REM Free models only.
REM
REM Note that credit on the account raises the free-model DAILY cap (50 to
REM 1000/day) but not the 20-requests-per-minute ceiling shared across all free
REM models -- that is a rate, not a quota. When it is hit the service waits for
REM the window to reset rather than burning more of the budget.
REM
REM If a fill ever stalls on that ceiling, setting this to 1.0 admits cheap paid
REM models as fallback rungs only (deepseek-v4-flash is $0.08 per million tokens,
REM a fraction of a cent per form). Free models are always tried first.
if "%JOBKB_MAX_PRICE%"=="" set JOBKB_MAX_PRICE=0

REM Where your resume lives. Both are re-read at every start and only rewritten
REM when the file has actually changed, so a resume you keep editing stays in
REM step by itself.
REM
REM   JOBKB_RESUME       the text version (.txt/.md) - grounds written answers
REM   JOBKB_RESUME_FILE  the document (.pdf/.docx)   - attached to upload fields
REM
REM Set them here, or with setx, or use the picker in the extension's Options.
if "%JOBKB_RESUME%"=="" set JOBKB_RESUME=%~dp0..\resume.txt
REM if "%JOBKB_RESUME_FILE%"=="" set JOBKB_RESUME_FILE=%USERPROFILE%\Documents\resume.pdf

.venv\Scripts\python.exe run.py
goto :eof

:nopython
echo.
echo   Python was not found. Install it from https://python.org and tick
echo   "Add python.exe to PATH" during setup.
echo.
pause
