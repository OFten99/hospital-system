@echo off
chcp 65001 >nul
title HIS One-Click Demo
cd /d "%~dp0"

echo ============================================================
echo   Hospital Outpatient System - ONE-CLICK DEMO
echo ============================================================
echo.
echo   正在启动服务，几秒后会自动打开「演示控制台」...
echo   控制台里可以免密切换五个角色，并一键走完整条就诊闭环。
echo.

rem 先把「6 秒后打开演示控制台」挂到后台，再交给 run_web.bat 去做
rem Python 检测 / 装依赖 / 起 MySQL / 启动 Web 的完整流程。
rem 用 explorer 而不是 start，是为了避免嵌套引号把 URL 传丢。
start "" /b cmd /c "timeout /t 6 /nobreak >nul & explorer http://127.0.0.1:5000/demo"

rem 演示模式由 run_web.bat 内部开启（set HIS_DEMO_MODE=1）
call "%~dp0run_web.bat"
