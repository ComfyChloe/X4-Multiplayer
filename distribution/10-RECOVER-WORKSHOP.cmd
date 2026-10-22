@echo off
setlocal
cd /d "%~dp0"
"%~dp0runtime\python.exe" -I -B -c "import sys; sys.path.insert(0, '.'); import recover_workshop; raise SystemExit(recover_workshop.main())" %*
set "X4LAN_RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %X4LAN_RESULT%
