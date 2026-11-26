@echo off
rem Hivemind 2.3 agent loop (workstream R, item R7). Installed as <ForgeHome>\bin\agent-loop.cmd by
rem Install-WdLauncher (watchdog.ps1) / install.ps1. Tasks ForgeAgent (user lane) and ForgeSystemAgent (system lane) run
rem   cmd.exe /d /s /c ""<ForgeHome>\bin\agent-loop.cmd" user|system"
rem It runs current\agent\forge-agent.ps1 -Child forever with a 5 s pause, so a crash or a bundle update (the worker
rem exits after draining) restarts the worker on the new code. Replaces the in-script supervisor.
rem LoopVersion=1
rem FORGE_LOOP_ONCE=1 (tests): run the worker once and exit with its exit code.
setlocal
set "LANE=%~1"
if "%LANE%"=="" set "LANE=user"
if /i not "%LANE%"=="user" if /i not "%LANE%"=="system" (
  echo agent-loop: lane must be user or system
  exit /b 2
)
for %%I in ("%~dp0..") do set "FH=%%~fI"
:loop
powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%FH%\current\agent\forge-agent.ps1" -Child -Lane %LANE% -ConfigPath "%FH%\data\agent-config.json"
set "RC=%ERRORLEVEL%"
if "%FORGE_LOOP_ONCE%"=="1" exit /b %RC%
rem 5 s pause (timeout.exe refuses to run without a console under Task Scheduler)
ping -n 6 127.0.0.1 >nul
goto loop
