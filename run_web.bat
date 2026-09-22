@echo off
chcp 65001 >nul
title HIS Web - Hospital Outpatient System
cd /d "%~dp0web_app"

echo ============================================================
echo   Hospital Outpatient Management System - Web
echo ============================================================
echo.

rem ================= 1. Find Python =================
set "PY="
if exist "C:\Users\60104\AppData\Local\Programs\Python\Python312\python.exe" set "PY=C:\Users\60104\AppData\Local\Programs\Python\Python312\python.exe"
if not defined PY if exist "C:\Users\60104\AppData\Local\Programs\Python\Python313\python.exe" set "PY=C:\Users\60104\AppData\Local\Programs\Python\Python313\python.exe"
if not defined PY if exist "C:\Program Files\Python312\python.exe" set "PY=C:\Program Files\Python312\python.exe"
if not defined PY if exist "C:\Program Files\Python311\python.exe" set "PY=C:\Program Files\Python311\python.exe"

if not defined PY goto NOPY
echo [1/4] Python: %PY%

rem ================= 2. Check dependencies =================
"%PY%" -c "import flask, mysql.connector" >nul 2>&1
if errorlevel 1 goto INSTALLDEP
echo [2/4] Dependencies: OK
goto CHECKMYSQL

:INSTALLDEP
echo [2/4] Installing dependencies ...
"%PY%" -m pip install --no-warn-script-location --timeout 60 --retries 5 -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
if errorlevel 1 goto DEPFAIL
echo [2/4] Dependencies: installed
goto CHECKMYSQL

rem ================= 3. Ensure MySQL is running =================
:CHECKMYSQL
netstat -ano | findstr ":3306" | findstr "LISTENING" >nul
if not errorlevel 1 goto MYSQLOK
echo [3/4] MySQL not running, starting it ...
call "%~dp0start_mysql.bat" silent
netstat -ano | findstr ":3306" | findstr "LISTENING" >nul
if errorlevel 1 goto MYSQLFAIL

:MYSQLOK
echo [3/4] MySQL: OK (port 3306)
"%PY%" -c "import mysql.connector,sys;c=mysql.connector.connect(host='127.0.0.1',port=3306,user='root',password='123456');c.close()" >nul 2>&1
if errorlevel 1 goto DBFAIL

rem ================= 4. Start Web =================
echo [4/4] Starting web service ...
echo.
echo ------------------------------------------------------------
echo   Open in browser:  http://127.0.0.1:5000
echo   Test accounts:    admin01 / doctor01 / reg01 / cash01
echo   Default password: 123456
echo.
echo   KEEP THIS WINDOW OPEN. Closing it stops the service.
echo ------------------------------------------------------------
echo.

"%PY%" app.py

echo.
echo [Service stopped]
pause
exit /b 0

rem ================= Error handlers =================
:NOPY
echo.
echo [ERROR] No usable Python interpreter found.
echo         Install Python 3.8+ or edit the PY path in this file.
echo.
pause
exit /b 1

:DEPFAIL
echo.
echo [ERROR] Dependency installation failed. Check your network.
echo.
pause
exit /b 1

:MYSQLFAIL
echo.
echo [ERROR] MySQL failed to start. Web cannot connect to database.
echo         Run start_mysql.bat separately to see the details.
echo.
pause
exit /b 1

:DBFAIL
echo.
echo [ERROR] Cannot connect to MySQL with root/123456.
echo         Check the password or whether the database is initialized.
echo.
pause
exit /b 1
