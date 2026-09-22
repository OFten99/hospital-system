@echo off
chcp 65001 >nul
title HIS MySQL Launcher

rem Silent mode: run_web.bat calls this with "silent" to avoid pausing
set "SILENT=0"
if /i "%~1"=="silent" set "SILENT=1"

if "%SILENT%"=="0" goto BANNER
goto CHECKPORT

:BANNER
echo ============================================================
echo   Starting MySQL 8.4 (no-service mode, datadir D:\MySQLData)
echo ============================================================

:CHECKPORT
netstat -ano | findstr ":3306" | findstr "LISTENING" >nul
if not errorlevel 1 goto ALREADY

set "MYSQL_BIN=C:\Program Files\MySQL\MySQL Server 8.4\bin\mysqld.exe"
if not exist "%MYSQL_BIN%" goto NOBIN
if not exist "D:\MySQLData" goto NODATA

rem NOTE: the project folder name contains non-ASCII characters.
rem Passing such a path to mysqld on the command line fails with
rem "Failed to open required defaults file". So copy the config to
rem a pure-ASCII path (the MySQL datadir) and load it from there.
set "MY_CNF=D:\MySQLData\my_his.cnf"
copy /y "%~dp0my_his.cnf" "%MY_CNF%" >nul
if errorlevel 1 goto NOCNF

if "%SILENT%"=="0" echo Starting MySQL, please wait ...

rem Use a config file: the basedir path contains spaces, passing
rem --basedir= directly would be split into several arguments and fail.
start "HIS-MySQL" /min "%MYSQL_BIN%" --defaults-file="%MY_CNF%"

rem Ping-based wait: "timeout" fails when stdin is redirected.
set /a WAIT=0
goto WAITLOOP

:WAITLOOP
ping -n 3 127.0.0.1 >nul 2>&1
set /a WAIT+=2
netstat -ano | findstr ":3306" | findstr "LISTENING" >nul
if not errorlevel 1 goto READY
if %WAIT% LSS 40 goto WAITLOOP
goto TIMEOUT

:ALREADY
if "%SILENT%"=="0" echo [OK] MySQL is already running on port 3306.
if "%SILENT%"=="0" pause
exit /b 0

:READY
if "%SILENT%"=="0" echo [OK] MySQL is ready on port 3306 (root / 123456).
if "%SILENT%"=="0" pause
exit /b 0

:TIMEOUT
echo.
echo [ERROR] MySQL did not become ready within 40 seconds.
echo         Possible causes:
echo           1) Antivirus blocked mysqld.exe - add it to the whitelist
echo           2) Datadir D:\MySQLData is locked or corrupted
echo           3) Port 3306 is occupied by another program
echo         Check log: D:\MySQLData\*.err
if "%SILENT%"=="0" pause
exit /b 1

:NOBIN
echo [ERROR] mysqld.exe not found: %MYSQL_BIN%
if "%SILENT%"=="0" pause
exit /b 1

:NODATA
echo [ERROR] Data directory not found: D:\MySQLData
if "%SILENT%"=="0" pause
exit /b 1

:NOCNF
echo [ERROR] Config file copy failed: my_his.cnf to %MY_CNF%
if "%SILENT%"=="0" pause
exit /b 1
