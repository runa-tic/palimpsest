@echo off
REM Open Claude Code in this vault (Windows). Double-click it, or run it from anywhere.
REM
REM Uses %~dp0 — the folder THIS file sits in — rather than a hardcoded path, so the launcher
REM keeps working after you move or rename the vault. Arguments pass straight through:
REM   "Claude Code.cmd" --continue
cd /d "%~dp0"

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
