@echo off
title Setting up Flask App Environment
echo Setting up Flask App Environment...
echo.

python --version >nul 2>&1
if errorlevel 1 goto NOPYTHON

if exist venv goto VENVEXISTS
echo Creating virtual environment...
python -m venv venv
if errorlevel 1 goto VENVFAIL
goto INSTALL

:VENVEXISTS
echo Virtual environment already exists.
goto INSTALL

:INSTALL
echo.
echo Installing dependencies from requirements.txt...
echo.
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

if errorlevel 1 goto PIPFAIL

echo.
echo Setup Completed Successfully!
echo Double-click run.bat to start.
echo.
pause
exit /b 0

:NOPYTHON
echo [ERROR] Python was not found!
echo Please install Python and check "Add Python to PATH" during setup.
echo.
pause
exit /b 1

:VENVFAIL
echo [ERROR] Failed to create virtual environment.
echo.
pause
exit /b 1

:PIPFAIL
echo [ERROR] Failed to install dependencies. Check your requirements.txt.
echo.
pause
exit /b 1