@echo off
REM Double-click this to run the Kavach agent daemon.
REM It reads .env from this same folder. Close the window or press Ctrl+C to stop.
cd /d "%~dp0"
kavach-agent.exe run
echo.
echo Agent stopped.
pause
