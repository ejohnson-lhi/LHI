@echo off
REM Remove the autostart entry created by install_watch_autostart.bat.
REM
REM Note: this does NOT stop any watch_recordings.bat windows that are
REM currently running — close those by hand (or via Task Manager) if
REM you want syncing to stop immediately. Without this removal, the
REM watcher restarts again on the next login.

setlocal

set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT=%STARTUP_DIR%\Iris recordings watcher.lnk"

if not exist "%SHORTCUT%" (
    echo Autostart shortcut was not present:
    echo   %SHORTCUT%
    echo Nothing to uninstall.
    echo.
    pause
    exit /b 0
)

del "%SHORTCUT%"
if errorlevel 1 (
    echo ERROR: could not delete:
    echo   %SHORTCUT%
    echo Try deleting manually from File Explorer.
    pause
    exit /b 1
)

echo Autostart shortcut removed.
echo.
echo If a watcher window is currently running, close it manually to stop
echo syncing right now — otherwise it keeps polling until you log off /
echo reboot. (It just won't come back after that.)
echo.
pause
endlocal
