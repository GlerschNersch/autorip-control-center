@echo off
title AutoRip Control Center Launcher
echo ========================================================
echo         AutoRip Control Center - Portable Edition
echo ========================================================
echo.
echo Checking Python environment...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python is not installed or not in PATH.
    echo Please install Python 3.10+ from python.org
    pause
    exit /b
)

echo Installing required Python packages (Flask)...
python -m pip install flask --quiet

echo.
echo Launching AutoRip Control Center Web Server...
echo Open your browser to: http://localhost:5000
echo.

start "" "http://localhost:5000"
python app.py

pause
