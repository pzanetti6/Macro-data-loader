@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "APP_NAME=Macro Data Loader"
set "APP_FILE=%CD%\app.py"
set "VENV_DIR=%CD%\.venv"
set "REQ_FILE=%CD%\requirements.txt"
set "REQ_STAMP=%VENV_DIR%\.requirements.installed"
set "PORT=8534"
set "PYTHON_CMD="

echo.
echo ========================================
echo  %APP_NAME%
echo ========================================
echo.

rem Prefer the Windows Python Launcher and supported CPython versions.
where py >nul 2>&1
if not errorlevel 1 (
    for %%V in (3.12 3.13 3.11 3.10) do (
        if not defined PYTHON_CMD (
            py -%%V -c "import sys; raise SystemExit(0 if (3,10) ^<= sys.version_info[:2] ^< (3,14) else 1)" >nul 2>&1
            if not errorlevel 1 set "PYTHON_CMD=py -%%V"
        )
    )
)

rem Fall back to python.exe on PATH if it is a compatible version.
if not defined PYTHON_CMD (
    where python >nul 2>&1
    if not errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if (3,10) ^<= sys.version_info[:2] ^< (3,14) else 1)" >nul 2>&1
        if not errorlevel 1 set "PYTHON_CMD=python"
    )
)

if not defined PYTHON_CMD (
    echo No compatible Python 3.10-3.13 installation was found.
    echo.
    echo Install 64-bit Python 3.12 from python.org.
    echo During installation, enable "Add python.exe to PATH" or install the Python Launcher.
    echo Then double-click this file again.
    echo.
    pause
    exit /b 1
)

echo Using Python:
%PYTHON_CMD% --version

rem Rebuild a copied or stale virtual environment automatically.
if exist "%VENV_DIR%\Scripts\python.exe" (
    "%VENV_DIR%\Scripts\python.exe" -c "import sys; raise SystemExit(0 if (3,10) ^<= sys.version_info[:2] ^< (3,14) else 1)" >nul 2>&1
    if errorlevel 1 (
        echo Removing an incompatible .venv...
        rmdir /s /q "%VENV_DIR%"
    )
) else if exist "%VENV_DIR%" (
    echo Removing a non-Windows or incomplete .venv...
    rmdir /s /q "%VENV_DIR%"
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Creating the app environment...
    %PYTHON_CMD% -m venv "%VENV_DIR%"
    if errorlevel 1 goto :error
)

set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"

rem Install packages only when requirements.txt changed since the last install.
set "NEED_INSTALL=1"
if exist "%REQ_STAMP%" (
    fc /b "%REQ_FILE%" "%REQ_STAMP%" >nul 2>&1
    if not errorlevel 1 set "NEED_INSTALL=0"
)
if "%NEED_INSTALL%"=="1" (
    echo Installing required packages...
    "%VENV_PYTHON%" -m pip install --upgrade pip setuptools wheel
    if errorlevel 1 goto :error
    "%VENV_PYTHON%" -m pip install -r "%REQ_FILE%"
    if errorlevel 1 goto :error
    copy /y "%REQ_FILE%" "%REQ_STAMP%" >nul
) else (
    echo Packages are up to date ^(delete .venv to force a reinstall^).
)

echo.
echo Starting %APP_NAME% on http://localhost:%PORT%
echo To stop it, return to this window and press Ctrl+C.
echo.

"%VENV_PYTHON%" -m streamlit run "%APP_FILE%" --server.port %PORT% --server.address localhost --browser.serverAddress localhost
if errorlevel 1 goto :error
exit /b 0

:error
echo.
echo The app could not be started. Review the error above.
echo If needed, delete the .venv folder and run this launcher again.
echo.
pause
exit /b 1
