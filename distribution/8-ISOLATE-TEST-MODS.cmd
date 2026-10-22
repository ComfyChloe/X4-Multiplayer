@echo off
setlocal
cd /d "%~dp0"
"%~dp0runtime\python.exe" -I -B "%~dp0isolate_test_mods.py" %*
set "X4LAN_RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %X4LAN_RESULT%
