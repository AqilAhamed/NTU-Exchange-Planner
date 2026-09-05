@echo off
setlocal EnableDelayedExpansion
REM NTU Exchange Planner launcher. It bootstraps through start.ps1 when needed.
REM
REM Three things this script does that the obvious version does not, each because
REM the obvious version failed on them:
REM   1. It prefers :8000/:3000, but automatically selects fallback ports when
REM      an old or unrelated process cannot be released safely.
REM   2. It waits until both servers actually ANSWER before opening the browser.
REM      "next dev" compiles on first request and takes 10-20s from cold; a fixed
REM      3-second sleep lands the browser on a connection error.
REM   3. It uses "ping" rather than "timeout" to wait, because "timeout" aborts
REM      with "Input redirection is not supported" whenever stdin is redirected.

set "ROOT=%~dp0"
cd /d "%ROOT%"

echo NTU Exchange Planner
echo ====================

REM --- preconditions -------------------------------------------------------
if not exist "%ROOT%backend\.venv\Scripts\python.exe" (
  goto :bootstrap
)
if not exist "%ROOT%frontend\node_modules\.bin\next.cmd" (
  goto :bootstrap
)
where node.exe >nul 2>&1
if errorlevel 1 goto :bootstrap
where npm.cmd >nul 2>&1
if errorlevel 1 goto :bootstrap
if not exist "%ROOT%backend\.env" (
  goto :bootstrap
)
if not exist "%ROOT%coursefinder.db" (
  echo   [X] coursefinder.db not found in the project root.
  echo       Copy the complete project folder and run start.bat again.
  goto :fail
)

REM --- ports ---------------------------------------------------------------
set "API_PORT=8000"
set "UI_PORT=3000"
set "API_RUNNING=0"
set "UI_RUNNING=0"

call :prepare_api
if errorlevel 1 goto :fail
call :prepare_ui
if errorlevel 1 goto :fail

REM --- search sidecar (openserp) --------------------------------------------
REM The research lane needs the openserp search server on :7000. We ship a
REM self-contained Windows binary (no Docker needed) in tools\openserp. Start it
REM if it is not already running; if it is absent, the app still runs and the
REM research lane reports "not available" (the designed degradation path).
if not exist "%ROOT%tools\openserp\openserp.exe" (
  echo   [!] tools\openserp\openserp.exe not found.
  echo       The research lane will report "not available". Everything else works.
) else (
  call :port_in_use 7000
  if errorlevel 1 (
    echo   [OK] Search sidecar already running on :7000.
  ) else (
    echo   Starting search sidecar on http://127.0.0.1:7000 ...
    start "NTU Exchange Search" /D "%ROOT%tools\openserp" cmd /k openserp.exe serve -a 127.0.0.1 -p 7000
    call :waitfor "http://127.0.0.1:7000/duckduckgo/search?text=ping&limit=1" 45 SEARCH
  )
)

REM --- launch --------------------------------------------------------------
if "%API_RUNNING%"=="0" (
  echo   Starting API on http://127.0.0.1:%API_PORT% ...
  start "NTU Exchange API" /D "%ROOT%backend" cmd /k .venv\Scripts\python.exe -m uvicorn api.chat:app --app-dir src --port %API_PORT% --reload
) else (
  echo   Reusing the existing API on http://127.0.0.1:%API_PORT% ...
)

REM Do not expose a usable-looking UI until the API is healthy. Otherwise a
REM frontend can load successfully while every chat request returns 500.
call :waitfor "http://127.0.0.1:%API_PORT%/api/health" 60 API
if errorlevel 1 goto :slow

if "%UI_RUNNING%"=="0" (
  echo   Starting UI  on http://localhost:%UI_PORT% ...
  start "NTU Exchange UI" /D "%ROOT%frontend" cmd /k "set BACKEND_URL=http://127.0.0.1:%API_PORT%&& npm run dev -- --port %UI_PORT%"
) else (
  echo   Reusing the existing UI on http://localhost:%UI_PORT% ...
)

REM --- wait until the UI actually answers ----------------------------------
call :waitfor "http://localhost:%UI_PORT%" 90 UI
if errorlevel 1 goto :slow

echo.
echo   Both servers are up.
echo     API  http://127.0.0.1:%API_PORT%/api/health
echo     UI   http://localhost:%UI_PORT%
echo.
echo   Existing services were reused where available; newly started services open in separate windows.
start "" http://localhost:%UI_PORT%
endlocal
exit /b 0

REM =========================================================================
:prepare_api
REM Prefer the normal port, but never let a stale/unrelated process block the app.
REM Check the project health endpoint first; netstat can briefly lag while a
REM previous uvicorn child process is being detached on Windows.
set "API_HEALTH_RESPONDS=0"
curl.exe -s -f --max-time 3 "http://127.0.0.1:8000/api/health" >nul 2>&1
if not errorlevel 1 set "API_HEALTH_RESPONDS=1"
if "%API_HEALTH_RESPONDS%"=="1" (
  curl.exe -s -f --max-time 3 "http://127.0.0.1:8000/api/health" | findstr /c:"cost_of_living_excludes_rent":true >nul 2>&1
  if not errorlevel 1 (
    set "API_RUNNING=1"
    exit /b 0
  )
  echo   [WARN] Port 8000 is occupied by an outdated project API. Restarting it...
  set "STALE_API_PID="
  for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:":8000 .*LISTENING"') do set "STALE_API_PID=%%P"
  if defined STALE_API_PID taskkill /PID !STALE_API_PID! /T /F >nul 2>&1
) else (
  call :port_in_use 8000
  if errorlevel 1 exit /b 0
  echo   [WARN] Port 8000 is occupied by an unrelated service. Leaving it untouched.
)
call :wait_port_free 8000 10
if not errorlevel 1 exit /b 0

call :find_free_port 8001 8010 API_PORT
if errorlevel 1 (
  echo   [X] No available API port was found between 8001 and 8010.
  exit /b 1
)
echo   [WARN] Using API fallback port %API_PORT%; the UI will be pointed to it automatically.
exit /b 0

:prepare_ui
if "%API_PORT%"=="8000" (
  call :ensure_service 3000 UI "http://localhost:3000"
  exit /b %errorlevel%
)

call :port_in_use 3000
if not errorlevel 1 exit /b 0
call :find_free_port 3001 3010 UI_PORT
if errorlevel 1 (
  echo   [X] No available UI port was found between 3001 and 3010.
  exit /b 1
)
echo   [WARN] Using UI fallback port %UI_PORT% so it uses the new API port.
exit /b 0

:find_free_port
REM %1 = first port, %2 = last port, %3 = output variable
for /l %%P in (%~1,1,%~2) do (
  call :port_in_use %%P
  if not errorlevel 1 (
    set "%~3=%%P"
    exit /b 0
  )
)
exit /b 1

:ensure_service
REM %1 = port, %2 = label, %3 = health URL
REM Reuse an existing healthy project service. Only reject the port when it is
REM occupied by something that does not answer the expected health URL.
netstat -ano | findstr /r /c:":%~1 .*LISTENING" >nul 2>&1
if errorlevel 1 exit /b 0

curl.exe -s -f -o nul --max-time 3 "%~3" >nul 2>&1
if not errorlevel 1 (
  if /i "%~2"=="API" (
    curl.exe -s -f --max-time 3 "%~3" | findstr /c:"general_questions_chroma_chunks" >nul 2>&1
    if errorlevel 1 (
      echo   [WARN] The API on :%~1 is an older project process. Restarting that exact API process...
      set "STALE_API_PID="
      for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:":%~1 .*LISTENING"') do set "STALE_API_PID=%%P"
      if defined STALE_API_PID taskkill /PID !STALE_API_PID! /T /F >nul 2>&1
      call :wait_port_free %~1 10
      if errorlevel 1 (
        echo   [X] Could not free port %~1 automatically.
        echo       Close the API process shown by: netstat -ano ^| findstr :%~1
        exit /b 1
      )
      exit /b 0
    )
  )
  echo   [OK] %~2 already running on :%~1.
  if /i "%~2"=="API" set "API_RUNNING=1"
  if /i "%~2"=="UI" set "UI_RUNNING=1"
  exit /b 0
)

echo   [X] Port %~1 is already in use, but the %~2 health check failed.
echo       Something unrelated is running there. Close it and try again, or find it with:
echo           netstat -ano ^| findstr :%~1
exit /b 1

:port_in_use
REM Return success when the port is free and errorlevel 1 when it is busy.
netstat -ano | findstr /r /c:":%~1 .*LISTENING" >nul 2>&1
if errorlevel 1 exit /b 0
exit /b 1

:wait_port_free
REM %1 = port, %2 = attempts
for /l %%i in (1,1,%~2) do (
  netstat -ano | findstr /r /c:":%~1 .*LISTENING" >nul 2>&1
  if errorlevel 1 exit /b 0
  ping -n 2 127.0.0.1 >nul 2>&1
)
exit /b 1

:waitfor
REM %1 = url, %2 = attempts, %3 = label
echo   Waiting for %~3 ...
for /l %%i in (1,1,%~2) do (
  curl.exe -s -o nul --max-time 3 "%~1" >nul 2>&1
  if not errorlevel 1 (
    echo   [OK] %~3 responded after about %%i seconds.
    exit /b 0
  )
  ping -n 2 127.0.0.1 >nul 2>&1
)
echo   [!] %~3 did not answer within %~2 seconds.
exit /b 1

:slow
echo.
echo   A required server is taking longer than expected or failed to start.
echo   Check the API/UI window for the real error, then visit:
echo     http://localhost:3000
echo.
pause
endlocal
exit /b 1

:bootstrap
echo.
echo   Starting first-time setup. This uses an execution-policy bypass so
echo   PowerShell security settings do not block the project launcher.
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%ROOT%start.ps1"
set "BOOTSTRAP_EXIT=%ERRORLEVEL%"
if not "%BOOTSTRAP_EXIT%"=="0" (
  echo.
  echo   Setup did not complete. Read the message above, fix the requirement,
  echo   and run start.bat again.
  pause
)
endlocal & exit /b %BOOTSTRAP_EXIT%

:fail
echo.
pause
endlocal
exit /b 1
