@echo off
chcp 65001 >nul
title HIS Test Suite - Hospital Outpatient System
cd /d "%~dp0"

echo ============================================================
echo   Hospital Outpatient System - Automated Test Suite
echo ============================================================
echo.

rem ================= 1. Find Python =================
set "PY="
if exist "C:\Users\60104\AppData\Local\Programs\Python\Python312\python.exe" set "PY=C:\Users\60104\AppData\Local\Programs\Python\Python312\python.exe"
if not defined PY if exist "C:\Users\60104\AppData\Local\Programs\Python\Python313\python.exe" set "PY=C:\Users\60104\AppData\Local\Programs\Python\Python313\python.exe"
if not defined PY if exist "C:\Program Files\Python312\python.exe" set "PY=C:\Program Files\Python312\python.exe"
if not defined PY if exist "C:\Program Files\Python311\python.exe" set "PY=C:\Program Files\Python311\python.exe"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY goto NOPY
echo [1/3] Python: %PY%

rem ================= 2. Check dependencies =================
"%PY%" -c "import flask, mysql.connector" >nul 2>&1
if errorlevel 1 goto INSTALLDEP
echo [2/3] Dependencies: OK
goto CHECKMYSQL

:INSTALLDEP
echo [2/3] Installing dependencies ...
"%PY%" -m pip install --no-warn-script-location --timeout 60 --retries 5 -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
if errorlevel 1 goto DEPFAIL
goto CHECKMYSQL

rem ================= 3. Database (optional) =================
rem 离线校验用例（路由清单 / 异常分层 / 密码哈希 / 结果标志…）不需要数据库。
rem 数据库不可用时，业务集成用例会优雅跳过而不是报错，所以这里不强制启动 MySQL。
:CHECKMYSQL
netstat -ano | findstr ":3306" | findstr "LISTENING" >nul
if errorlevel 1 goto NODB
echo [3/3] MySQL: OK (port 3306) - integration tests will run
goto RUN

:NODB
echo [3/3] MySQL: NOT running (port 3306)
echo       Integration tests will be SKIPPED; offline checks still run.
echo       To run everything: start MySQL, then run init_database.py once.
goto RUN

rem ================= Run =================
:RUN
echo.
echo ------------------------------------------------------------
"%PY%" -m unittest discover -s tests -v
set "RC=%ERRORLEVEL%"
echo ------------------------------------------------------------
echo.
if not "%RC%"=="0" goto FAILED
echo [OK] All tests passed.
echo.
pause
exit /b 0

:FAILED
echo [FAILED] Some tests did not pass. See the log above.
echo          Exit code: %RC%
echo.
pause
exit /b %RC%

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
