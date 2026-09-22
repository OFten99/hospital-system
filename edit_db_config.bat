@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "my_cloud_db.cnf" (
  echo 还没有配置文件，先运行一次 deploy_cloud_db.bat 生成模板，再回来编辑。
  echo.
  pause
  exit /b 1
)
echo 正在用记事本打开 my_cloud_db.cnf ...
echo 把 host / port / user / password 四项填在 = 号后面，保存后关闭记事本，
echo 然后双击 deploy_cloud_db.bat 执行导入。
echo.
start "" notepad "%~dp0my_cloud_db.cnf"
