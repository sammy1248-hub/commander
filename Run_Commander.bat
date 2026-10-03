@echo off
setlocal

rem Always run from the folder this .bat file lives in, regardless of
rem where it's double-clicked from or what the current directory is.
cd /d "%~dp0"

rem Default per-user install location of Python 3.12
set "PY312=%LocalAppData%\Programs\Python\Python312"

rem Check that a real Python works (this also ignores the Microsoft Store stub).
python --version >nul 2>nul
if not errorlevel 1 goto python_ok

echo Python was not found. Installing Python 3.12...
echo.

rem --- Option 1: winget (built into Windows 10/11) ---
where winget >nul 2>nul
if errorlevel 1 goto install_direct

winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto install_direct
goto refresh_path

:install_direct
rem --- Option 2: download the installer from python.org ---
echo Downloading Python 3.12 from python.org...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile '%TEMP%\python-3.12-installer.exe'"
if errorlevel 1 goto install_failed

echo Installing Python 3.12 (this can take a minute)...
"%TEMP%\python-3.12-installer.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
if errorlevel 1 goto install_failed
del "%TEMP%\python-3.12-installer.exe" >nul 2>nul

:refresh_path
rem The current window doesn't see the new PATH yet, so add it manually.
set "PATH=%PY312%;%PY312%\Scripts;%PATH%"

python --version >nul 2>nul
if errorlevel 1 goto install_failed

echo.
echo Python 3.12 installed successfully.
echo.
goto python_ok

:install_failed
echo.
echo [ERROR] Automatic Python install failed.
echo Install Python 3.12 manually from https://www.python.org/downloads/
echo and make sure "Add python.exe to PATH" is checked, then try again.
echo.
pause
exit /b 1

:python_ok

if not exist "app.py" (
    echo [ERROR] app.py not found in this folder.
    echo Make sure Run_Commander.bat sits in the same folder as
    echo app.py, engine.py, and controller.py.
    echo.
    pause
    exit /b 1
)

rem Install dependencies only once - a marker file records that it's done.
rem Delete .setup_done (or edit requirements.txt) to force a re-install,
rem e.g. after pulling an update that changed requirements.txt.
if not exist ".setup_done" (
    if not exist "requirements.txt" (
        echo [ERROR] requirements.txt not found in this folder.
        pause
        exit /b 1
    )

    echo First-time setup: installing dependencies...
    echo This can take a few minutes - subsequent launches will skip this.
    echo.
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] pip install failed - see the message above.
        pause
        exit /b 1
    )

    echo done > ".setup_done"
    echo.
    echo Setup complete.
    echo.
)

echo Starting Commander...
echo (Close this window, or press Ctrl+C to stop the app.)
echo.

python app.py

if errorlevel 1 (
    echo.
    echo [ERROR] Commander exited with an error - see the message above.
    echo.
    pause
)

endlocal
