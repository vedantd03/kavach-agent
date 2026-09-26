@echo off
REM Drag a folder onto this file to scan it, or double-click and type the path.
cd /d "%~dp0"
if "%~1"=="" (
  set /p TARGET="Folder to scan: "
) else (
  set "TARGET=%~1"
)
kavach-agent.exe scan "%TARGET%"
echo.
pause
