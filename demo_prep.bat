@echo off
chcp 65001 >nul
title HIS Demo Prep - Hospital Outpatient System
cd /d "%~dp0"

echo ============================================================
echo   Demo Prep - fill today's schedules + print demo checklist
echo ============================================================
echo.

rem ================= Find Python =================
set "PY="
if exist "C:\Users\60104\AppData\Local\Programs\Python\Python312\python.exe" set "PY=C:\Users\60104\AppData\Local\Programs\Python\Python312\python.exe"
if not defined PY if exist "C:\Users\60104\AppData\Local\Programs\Python\Python313\python.exe" set "PY=C:\Users\60104\AppData\Local\Programs\Python\Python313\python.exe"
if not defined PY if exist "C:\Program Files\Python312\python.exe" set "PY=C:\Program Files\Python312\python.exe"
if not defined PY if exist "C:\Program Files\Python311\python.exe" set "PY=C:\Program Files\Python311\python.exe"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY goto NOPY

"%PY%" -c "import mysql.connector" >nul 2>&1
if errorlevel 1 goto NODEP

"%PY%" demo_prep.py
if errorlevel 1 goto FAILED

echo.
pause
exit /b 0

:NODEP
echo [ERROR] mysql-connector-python is missing.
echo         Run:  "%PY%" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
echo.
pause
exit /b 1

:NOPY
echo.
echo [ERROR] No usable Python interpreter found.
echo         Install Python 3.8+ or edit the PY path in this file.
echo.
pause
exit /b 1

:FAILED
echo.
echo [FAILED] Demo prep did not finish. Check the output above.
echo          Most likely: MySQL is not running. Start it with start_mysql.bat.
echo.
pause
exit /b 1
