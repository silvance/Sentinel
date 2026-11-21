@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Sentinel - start a new recording log
rem ------------------------------------------------------------------------------------------------
rem After testing, before the real recordings start: clears the test recordings in the supported way.
rem Runs the installed Sentinel's own command "new-recording-log" (0.3.27 or later), which
rem   - sets the recording log aside (kept, never deleted),
rem   - deletes the test recordings from this computer,
rem   - empties the archive drives that are plugged in (they keep their names),
rem   - starts a new, empty recording log.
rem Settings (camera, schedule, motion, storage) are not changed.
rem
rem Use:   close the Sentinel window, plug in every archive drive, then
rem        right-click this file, Run as administrator.
rem   or:  START-A-NEW-RECORDING-LOG.cmd "installed program folder"
rem
rem The window never closes by itself: whatever happened stays on the screen until CLOSE is typed
rem (field report 2026-11-20: a first run ended with its window gone and nothing to read).
rem ------------------------------------------------------------------------------------------------
set "APP=%~1"
set "SYS=%SystemRoot%\System32"
cd /d "%SystemRoot%"

rem Is there a keyboard? timeout.exe refuses input that comes from a file or another program.
rem Without a keyboard nothing waits at the end.
set "KEYBOARD=1"
"%SYS%\timeout.exe" /t 0 /nobreak >nul 2>&1
if errorlevel 1 set "KEYBOARD=0"

echo.
echo  Sentinel - start a new recording log
echo  ====================================
echo.

"%SYS%\whoami.exe" /groups | "%SYS%\find.exe" "S-1-16-12288" >nul
if errorlevel 1 goto not_admin

if not defined APP set "APP=%ProgramFiles%\Sentinel"
if exist "%APP%\Sentinel.exe" goto have_app
echo  Sentinel was not found in "%APP%".
echo  Type the full path of the installed Sentinel folder - the folder that holds Sentinel.exe -
echo  and press Enter. To find it: right-click the Sentinel shortcut, Open file location.
echo.
set "APP="
set /p "APP=  Installed Sentinel folder: "
if not defined APP goto no_app
set "APP=%APP:"=%"
if not exist "%APP%\Sentinel.exe" goto no_app
:have_app
if "%APP:~-1%"=="\" set "APP=%APP:~0,-1%"

rem An older Sentinel does not know the command: say so instead of showing its help text.
"%APP%\Sentinel.exe" new-recording-log --help >nul 2>&1
if errorlevel 1 goto too_old

echo  Before you go on:
echo    1. CLOSE the Sentinel window (every Sentinel window on this computer).
echo    2. PLUG IN every archive drive (SSD_1 and SSD_2). A drive that is not plugged in
echo       keeps its test recordings.
echo.
echo  Then press a key ONCE. Nothing is removed yet: Sentinel first shows what it would do
echo  and asks you to type words to confirm.
echo  Until that question is on the screen, press nothing more.
echo.
pause
echo.
echo  One moment ...
echo.

"%APP%\Sentinel.exe" new-recording-log
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" goto done
if "%RC%"=="4" goto done_with_notes
echo  ==========================================================================
echo   NOT DONE. Nothing was removed unless the lines above say so.
echo  ==========================================================================
echo  The lines above say why. To try again: close this window and start this file again.
echo  Then open Sentinel from its usual icon and check that it is recording.
goto end

:done_with_notes
echo  See the lines above: something did not go as expected.
:done
echo  NEXT: open Sentinel from its usual icon.
echo  Check: Status has no alert about missing recordings; Recordings is empty;
echo  Drives shows the archive drive as "In use".
goto end

:not_admin
echo  ==========================================================================
echo   NOT STARTED: this needs administrator rights. Nothing was changed.
echo  ==========================================================================
echo  Close this window. Then RIGHT-click START-A-NEW-RECORDING-LOG.cmd and choose
echo  "Run as administrator".
goto end

:no_app
echo  No installed Sentinel was found there. Nothing was changed.
goto end

:too_old
echo  The installed Sentinel is older than 0.3.27 and cannot do this.
echo  Install the Sentinel in this folder first (INSTALL-ON-THE-LAPTOP.cmd), then run this again.
echo  Nothing was changed.
goto end

:end
echo.
if "%KEYBOARD%"=="0" goto closed
echo  This window stays open so that the lines above can be read.
:wait_close
set "ANS="
set /p "ANS= To close it, type CLOSE and press Enter: "
if not defined ANS goto wait_close
set "ANS=%ANS:"=%"
if not defined ANS goto wait_close
if /i "%ANS%"=="CLOSE" goto closed
goto wait_close
:closed
endlocal
