@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Sentinel - install the program folder beside this file
rem ------------------------------------------------------------------------------------------------
rem Installs a complete Sentinel program folder on a computer that already runs Sentinel, WITHOUT an
rem update file: for a build made where the release signing key was not at hand, or when an update
rem file cannot be applied. docs/08_PACKAGING.md section 10.
rem
rem Put this file and the built program folder (named "Sentinel") side by side on a USB stick.
rem
rem How: the new folder is copied next to the installed one as "<program folder>.previous", and the
rem installed Sentinel is then asked to switch to it ("Sentinel.exe update rollback"). That command
rem stops recording properly, swaps the two folders, lets the new version check itself, starts
rem recording again, writes the change into the recording log, and swaps back by itself if the check
rem fails. The version that was installed is kept as "<program folder>.previous".
rem
rem Use:   right-click this file, Run as administrator.
rem   or:  INSTALL-FULL-FOLDER.cmd "installed program folder" [options passed to 'update rollback']
rem ------------------------------------------------------------------------------------------------
set "NEW=%~dp0Sentinel"
set "APP=%~1"
set "EXTRA=%2 %3 %4 %5 %6 %7"
set "SYS=%SystemRoot%\System32"
set "CHECK=%TEMP%\sentinel-install-check.txt"
rem Never stand inside a program folder: a folder in use cannot be swapped.
cd /d "%SystemRoot%"

echo.
echo  Sentinel - install the program folder beside this file, without an update file
echo  ===============================================================================
echo.

if not exist "%NEW%\Sentinel.exe" goto no_new
rem The version comes from the new folder itself: "Installed: Sentinel X.Y.Z".
set "NEWVER="
"%NEW%\Sentinel.exe" update status >"%CHECK%" 2>&1
for /f "usebackq tokens=1-3" %%a in ("%CHECK%") do if "%%a %%b"=="Installed: Sentinel" set "NEWVER=%%c"
if not defined NEWVER goto new_unreadable
echo  New version beside this file:  Sentinel %NEWVER%
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
if /i "%APP%"=="%NEW%" goto same_folder

set "PREV=%APP%.previous"
set "KEEP=%APP%.previous-before-%NEWVER%"
set "WORK=%APP%.updates"
set "WHY=Full program folder install of %NEWVER% (no update file: the release signing key was not available on site)"

echo  Installed program folder:  "%APP%"
echo  New program folder:        "%NEW%"
echo.
"%APP%\Sentinel.exe" update status
echo.
"%APP%\Sentinel.exe" update status | "%SYS%\find.exe" "Installed: Sentinel %NEWVER%" >nul
if not errorlevel 1 goto already

echo  Step 1 of 5 - checking the new program folder on this drive (about a minute)...
"%NEW%\Sentinel.exe" about --verify >"%CHECK%" 2>&1
"%SYS%\find.exe" "Sentinel %NEWVER% " "%CHECK%" >nul
if errorlevel 1 goto new_bad
"%SYS%\find.exe" "installed files match the build manifest" "%CHECK%" >nul
if errorlevel 1 goto new_bad
echo               OK: Sentinel %NEWVER%, every file matches its build manifest.

if not exist "%PREV%\" goto nothing_aside
"%APP%\Sentinel.exe" update status | "%SYS%\find.exe" "Previous version kept: %NEWVER%" >nul
if not errorlevel 1 goto nothing_aside
if exist "%KEEP%\" goto keep_exists
echo  Step 2 of 5 - an older version is kept beside the program folder; setting it aside as
echo               "%KEEP%"
move "%PREV%" "%KEEP%" >nul
if errorlevel 1 goto move_failed
goto copy_new
:nothing_aside
echo  Step 2 of 5 - no older version to set aside.

:copy_new
echo  Step 3 of 5 - copying the new program folder beside the installed one...
"%SYS%\robocopy.exe" "%NEW%" "%PREV%" /MIR /R:2 /W:2 /NFL /NDL /NJH /NJS /NP
if errorlevel 8 goto copy_failed
if not exist "%APP%\sentinel.toml" goto verify_copy
echo               This computer keeps its settings file inside the program folder; copying it across.
copy /y "%APP%\sentinel.toml" "%PREV%\" >nul
if errorlevel 1 goto copy_failed
if exist "%APP%\camera.secret" copy /y "%APP%\camera.secret" "%PREV%\" >nul

:verify_copy
echo  Step 4 of 5 - checking the copy (about a minute)...
"%PREV%\Sentinel.exe" about --verify >"%CHECK%" 2>&1
"%SYS%\find.exe" "installed files match the build manifest" "%CHECK%" >nul
if errorlevel 1 goto copy_bad
"%APP%\Sentinel.exe" update status | "%SYS%\find.exe" "Previous version kept: %NEWVER%" >nul
if errorlevel 1 goto copy_bad
echo               OK.
echo.
echo  Step 5 of 5 - asking the installed Sentinel to switch to %NEWVER%.
echo.
echo    * Sentinel calls this "going back". That is expected: it is its command for switching to
echo      the program folder kept beside it. When it asks, type BACK and press Enter.
echo    * Recording stops for about a minute and starts again by itself.
echo    * A window titled "Sentinel update - do not close" opens. Leave it alone.
echo.
"%APP%\Sentinel.exe" update rollback --reason "%WHY%" %EXTRA%
if errorlevel 1 goto not_switched

echo.
echo  Waiting for the switch to finish (do not close this window either)...
set /a N=0
:wait
if not exist "%WORK%\pending.json" goto finished
set /a N+=1
if %N% GTR 300 goto slow
"%SYS%\ping.exe" -n 3 127.0.0.1 >nul
goto wait
:finished
"%SYS%\ping.exe" -n 4 127.0.0.1 >nul
echo.
if not exist "%APP%\Sentinel.exe" goto swapped_back
"%APP%\Sentinel.exe" update status
"%APP%\Sentinel.exe" update status | "%SYS%\find.exe" "Installed: Sentinel %NEWVER%" >nul
if errorlevel 1 goto swapped_back
echo.
echo  DONE: Sentinel %NEWVER% is installed. The version from before is kept as
echo        "%PREV%"
echo.
echo  Now check in the Sentinel window that has opened:
echo    1. Status says "Recording normally" (give it a minute).
echo    2. System, Updates shows %NEWVER%.
echo    3. Drives: archive copying is running. If a "Start archive copying" button shows, press it.
echo  Look at Status once more after five minutes, before you leave.
set "RC=0"
goto end

:not_admin
echo  This needs an administrator: right-click INSTALL-FULL-FOLDER.cmd, Run as administrator.
goto fail
:no_new
echo  The new program folder was not found beside this file:
echo    "%NEW%"
goto fail
:new_unreadable
echo  The folder beside this file does not answer as a Sentinel program:
type "%CHECK%"
goto fail
:no_app
echo  No Sentinel.exe was found in that folder.
goto fail
:same_folder
echo  That is the new program folder on this drive, not the installed one.
goto fail
:already
echo  Sentinel %NEWVER% is already installed here.
goto fail
:new_bad
echo  The new program folder on this drive is not a complete Sentinel %NEWVER%:
type "%CHECK%"
echo  Copy it to this drive again from the build computer.
goto fail
:keep_exists
echo  An older version is kept as "%PREV%",
echo  and "%KEEP%" exists already.
echo  Rename or remove one of them, then run this again.
goto fail
:move_failed
echo  The older version in "%PREV%" could not be renamed.
echo  Close any window that shows that folder, then run this again.
goto fail
:copy_failed
echo  The new program folder could not be copied to "%PREV%".
goto fail
:copy_bad
echo  The copy in "%PREV%" does not check out:
type "%CHECK%"
"%APP%\Sentinel.exe" update status
goto fail
:not_switched
echo.
echo  Sentinel did not start the switch (see its message above). Recording was not interrupted,
echo  or was started again. The new version is ready in "%PREV%";
echo  run this again to retry.
goto fail
:slow
echo  The switch has not finished after ten minutes. Look at the window titled
echo  "Sentinel update - do not close" and at "%WORK%\update.log".
goto fail_changed
:swapped_back
echo.
echo  Sentinel %NEWVER% was NOT installed: the new version did not pass its own check, or the
echo  folders could not be swapped. The version from before is in use. What happened is in
echo    "%WORK%\update.log"
echo  and in System, Updates in the Sentinel window. Check that Status says "Recording normally".
goto fail_changed

:fail
echo.
echo  The installed program was not changed.
if defined KEEP if exist "%KEEP%\" echo  An older version kept beside it is now in "%KEEP%".
:fail_changed
set "RC=1"
:end
echo.
pause
endlocal & exit /b %RC%
