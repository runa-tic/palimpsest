@echo off
REM Open Claude Code in this vault (Windows). Double-click it, or run it from anywhere.
REM
REM Uses %~dp0 — the folder THIS file sits in — rather than a hardcoded path, so the launcher
REM keeps working after you move or rename the vault. Arguments pass straight through:
REM   "Claude Code.cmd" --continue
cd /d "%~dp0"

REM Pre-flight: with an unreadable palimpsest.json the sync and the session opener run on DEFAULTS
REM (pull, push and state OFF), and the opener only hears of it from the next sync's receipt.
REM Exit code 3 means exactly that; a missing python (9009) or a crash (1) says nothing here.
python tools\config.py --check >nul 2>&1
if %errorlevel%==3 (
  python tools\config.py --check
  echo Press any key to open Claude Code anyway, and fix it there.
  pause >nul
)

where claude >nul 2>&1
if %errorlevel%==0 (
  claude %*
) else if exist "%USERPROFILE%\.local\bin\claude.exe" (
  "%USERPROFILE%\.local\bin\claude.exe" %*
) else (
  echo Could not find the claude CLI — not on PATH, and not at
  echo   %USERPROFILE%\.local\bin\claude.exe
  echo Install it, or edit this file to point at your install.
  pause
  exit /b 1
)
