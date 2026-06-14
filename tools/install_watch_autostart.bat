@echo off
REM Install watch_recordings.bat as a Windows autostart entry so the sync
REM watcher resumes automatically after every reboot / user login.
REM
REM Mechanism: drops a .lnk into %APPDATA%\Microsoft\Windows\Start Menu\
REM Programs\Startup pointing at watch_recordings.bat. Runs in the user's
REM logon session so it inherits the same ssh-agent + OpenSSH key the
REM manual runs use; no elevated privileges required.
REM
REM To remove: run uninstall_watch_autostart.bat (or delete the .lnk by hand).

setlocal

set "WATCH_BAT=%~dp0watch_recordings.bat"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT=%STARTUP_DIR%\Iris recordings watcher.lnk"

if not exist "%WATCH_BAT%" (
    echo ERROR: watch_recordings.bat not found at:
    echo   %WATCH_BAT%
    echo This installer must live next to watch_recordings.bat in the tools folder.
    pause
    exit /b 1
)

echo Installing autostart shortcut...
echo   Target script: %WATCH_BAT%
echo   Shortcut path: %SHORTCUT%
echo.

REM PowerShell creates the .lnk. WindowStyle = 7 means "Minimized" so the
REM CMD window doesn't grab focus or pop up obtrusively at every login;
REM Eric can restore it from the taskbar if he wants to see logs.
REM WorkingDirectory is set to tools/ so the bat's relative path resolves.
powershell -NoProfile -Command ^
    "$wsh = New-Object -ComObject WScript.Shell;" ^
    "$lnk = $wsh.CreateShortcut('%SHORTCUT%');" ^
    "$lnk.TargetPath = '%WATCH_BAT%';" ^
    "$lnk.WorkingDirectory = '%~dp0';" ^
    "$lnk.WindowStyle = 7;" ^
    "$lnk.Description = 'Continuously pulls Iris call recordings from the droplet';" ^
    "$lnk.Save()"

if errorlevel 1 (
    echo.
    echo ERROR: PowerShell failed to create the shortcut.
    pause
    exit /b 1
)

if not exist "%SHORTCUT%" (
    echo.
    echo ERROR: shortcut was not created. Path may not be writable.
    pause
    exit /b 1
)

echo Installed.
echo.
echo The watcher will start automatically on the next login/reboot.
echo To remove, run uninstall_watch_autostart.bat in this folder.
echo.
echo Start the watcher right now without rebooting? [Y/n]
set "ANSWER="
set /p "ANSWER=> "
if not defined ANSWER set "ANSWER=Y"
if /i "%ANSWER%"=="Y" (
    start "Iris recordings watcher" /min "%WATCH_BAT%"
    echo Watcher started in a minimized window.
)

echo.
pause
endlocal
