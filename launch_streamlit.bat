@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul

echo ==============================================
echo   US Stock Watchlist - Streamlit App Launcher
echo ==============================================
echo.

REM Change to project directory
cd /d "%~dp0"

REM Check if virtual environment exists
if not exist ".venv\Scripts\python.exe" (
    echo [INFO] Virtual environment not found. Creating...
    python -m venv .venv

    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )

    echo [INFO] Virtual environment created successfully.
    echo.

    echo [INFO] Upgrading pip...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip

    if errorlevel 1 (
        echo [ERROR] Failed to upgrade pip.
        pause
        exit /b 1
    )

    if exist "requirements.txt" (
        echo [INFO] Installing dependencies from requirements.txt...
        ".venv\Scripts\python.exe" -m pip install -r requirements.txt

        if errorlevel 1 (
            echo [ERROR] Failed to install requirements.txt.
            pause
            exit /b 1
        )

        echo [INFO] Dependencies installed successfully.
    ) else (
        echo [WARNING] requirements.txt not found. Skipping dependency installation.
    )
) else (
    echo [INFO] Virtual environment already exists. Skipping dependency installation.
)

echo.

REM Check app_streamlit.py
if not exist "app_streamlit.py" (
    echo [ERROR] app_streamlit.py not found in:
    echo %cd%
    pause
    exit /b 1
)

echo [INFO] Starting Streamlit app...
echo [INFO] Please keep this window open.
echo.

".venv\Scripts\python.exe" -m streamlit run app_streamlit.py --server.port 8501 --server.address localhost --browser.gatherUsageStats false

pause