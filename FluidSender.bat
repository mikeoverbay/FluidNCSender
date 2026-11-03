@echo off
rem FluidSender on Windows: starts the server (if it isn't running) and opens the page in its own
rem Chrome or Edge window, as fluidsender.sh does on Linux. Double-click it.
rem Needs Python 3.12 or newer from python.org ("Add python.exe to PATH" ticked when installing).
rem The server runs with no window of its own (pythonw) and quits by itself a minute after the last FluidSender
rem window closes, when no machine is connected and nothing runs. Its messages go to fluidsender-server.log here.
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
rem (the same Python without a console window: pyw for py, pythonw for python)
if "%PY%"=="py -3" (set "PYW=pyw -3") else (set "PYW=pythonw")
if not defined PY goto nopython

start "" %PYW% server.py --no-browser --port %PORT% --quit-when-closed
for /l %%i in (1,1,20) do (
    timeout /t 1 /nobreak >NUL
    curl.exe -s -o NUL "%URL%" && goto browser
)
echo FluidSender's server did not start. The reason is in fluidsender-server.log in this folder,
echo or run this in this folder to see it:   %PY% server.py
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
