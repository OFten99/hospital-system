@echo off
chcp 936 >nul
title HIS 一键演示（浏览器模拟）
cd /d "%~dp0"

echo ============================================================
echo   医院门诊管理系统 - 一键演示（浏览器模拟）
echo ============================================================
echo.
echo   不修改系统任何代码，全程用真实浏览器操作：
echo   自动打开页面、自动填账号密码登录、按动线切换 5 个角色。
echo.

rem ================= 1. 找 Python =================
set "PY="
if exist "C:\Users\60104\AppData\Local\Programs\Python\Python312\python.exe" set "PY=C:\Users\60104\AppData\Local\Programs\Python\Python312\python.exe"
if not defined PY if exist "C:\Users\60104\AppData\Local\Programs\Python\Python313\python.exe" set "PY=C:\Users\60104\AppData\Local\Programs\Python\Python313\python.exe"
if not defined PY if exist "C:\Program Files\Python312\python.exe" set "PY=C:\Program Files\Python312\python.exe"
if not defined PY if exist "C:\Program Files\Python311\python.exe" set "PY=C:\Program Files\Python311\python.exe"

if not defined PY goto NOPY
echo [1/3] Python: %PY%

rem ================= 2. 确保 Web 服务在跑 =================
netstat -ano | findstr ":5000" | findstr "LISTENING" >nul
if not errorlevel 1 goto WEBUP

echo [2/3] Web 服务未启动，正在后台拉起 run_web.bat ...
start "HIS Web" /min cmd /c "%~dp0run_web.bat"

set /a WAIT=0
:WAITPORT
timeout /t 1 /nobreak >nul
netstat -ano | findstr ":5000" | findstr "LISTENING" >nul
if not errorlevel 1 goto WEBUP
set /a WAIT+=1
if %WAIT% lss 40 goto WAITPORT
goto WEBFAIL

:WEBUP
echo [2/3] Web 服务在线：http://127.0.0.1:5000

rem ================= 3. 跑浏览器演示 =================
echo [3/3] 启动浏览器演示 ...
echo.
echo ------------------------------------------------------------
echo   依赖：Edge 已打开，且 browser-skill 扩展已启用。
echo   常用写法：
echo     一键演示.bat                    交互巡览（每站回车继续）
echo     一键演示.bat --auto 8           全自动（每站停 8 秒）
echo     一键演示.bat login cash01       只登录到收费人员
echo     一键演示.bat goto lab01 lab_tests
echo     一键演示.bat roles              列出角色与动线
echo ------------------------------------------------------------
echo.

"%PY%" "%~dp0demo_browser.py" %*

echo.
echo [演示结束] 浏览器窗口保持打开，可继续手工演示或复盘。
pause
exit /b 0

rem ================= 错误处理 =================
:NOPY
echo.
echo [错误] 没找到可用的 Python 解释器，请安装 Python 3.8+。
echo.
pause
exit /b 1

:WEBFAIL
echo.
echo [错误] Web 服务 40 秒内没起来。
echo        请单独双击 run_web.bat，看它打印到哪一步失败。
echo.
pause
exit /b 1
