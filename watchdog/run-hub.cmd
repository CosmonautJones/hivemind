@echo off
rem Forge Control hub launcher (task ForgeHub, runs as the installing user via S4U).
rem Lives in <ForgeHome>\current\watchdog\. Starts uvicorn from current\hub; kills nothing itself
rem (the watchdog owns restarts). Logs to data\hub.log, rotated at start when > 5 MB.
setlocal
if not defined FORGE_HOME set "FORGE_HOME=%ProgramData%\Forge"
set "FORGE_DATA=%FORGE_HOME%\data"
set "FORGE_RELEASE_DIR=%FORGE_HOME%\current"
set "FORGE_RELEASES_DIR=%FORGE_HOME%\releases"
rem 2.3 R5: a release whose requirements differ from the base venv carries its own releases\<ver>\venv
set "PY=%FORGE_HOME%\venv\Scripts\python.exe"
if exist "%FORGE_RELEASE_DIR%\venv\Scripts\python.exe" set "PY=%FORGE_RELEASE_DIR%\venv\Scripts\python.exe"
set "LOG=%FORGE_DATA%\hub.log"
set PYTHONUNBUFFERED=1
if not exist "%FORGE_DATA%" mkdir "%FORGE_DATA%"
if exist "%LOG%" for %%F in ("%LOG%") do if %%~zF GTR 5242880 move /y "%LOG%" "%LOG%.old" >nul
cd /d "%FORGE_RELEASE_DIR%"
echo [%DATE% %TIME%] run-hub.cmd starting hub from %FORGE_RELEASE_DIR% with %PY% >> "%LOG%"
"%PY%" "%FORGE_RELEASE_DIR%\hub\serve.py" >> "%LOG%" 2>&1
echo [%DATE% %TIME%] hub exited with code %ERRORLEVEL% >> "%LOG%"
endlocal
