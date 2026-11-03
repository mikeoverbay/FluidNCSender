@echo off
rem FluidSender on Windows: starts the server (if it isn't running) and opens the page in its own
rem Chrome or Edge window, as fluidsender.sh does on Linux. Double-click it.
rem Needs Python 3.12 or newer from python.org ("Add python.exe to PATH" ticked when installing).
rem The server runs in a minimized window called "FluidSender server": close that window to stop it.
setlocal
rem (pushd, not cd: it also works when this folder is on a network share, by mapping it to a drive letter)
pushd "%~dp0"
if "%FLUIDSENDER_PORT%"=="" (set "PORT=8765") else (set "PORT=%FLUIDSENDER_PORT%")
set "URL=http://localhost:%PORT%/"

rem Already running? (curl.exe comes with Windows 10 and 11)
curl.exe -s -o NUL "%URL%" && goto browser

rem Python: the py launcher first, else python on the PATH (not the Microsoft Store's stand-in, which can't run it)
set "PY="
py -3 --version >NUL 2>&1 && set "PY=py -3"
if not defined PY python --version >NUL 2>&1 && set "PY=python"
if not defined PY goto nopython

start "FluidSender server" /min %PY% server.py --no-browser --port %PORT%
for /l %%i in (1,1,20) do (
    timeout /t 1 /nobreak >NUL
    curl.exe -s -o NUL "%URL%" && goto browser
)
echo FluidSender's server did not start. Look in the "FluidSender server" window for the reason.
pause
exit /b 1

:browser
set "APP="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "APP=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined APP if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "APP=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined APP if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "APP=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if not defined APP if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "APP=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not defined APP if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "APP=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if defined APP (
    start "" "%APP%" --app=%URL% --window-size=1280,800
) else (
    start "" %URL%
)
exit /b 0

:nopython
echo Python was not found. Install Python 3.12 or newer from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" in the installer, then double-click FluidSender.bat again.
pause
exit /b 1
