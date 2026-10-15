@echo off
chcp 936 >nul
cd /d "%~dp0"
echo ============================================================
echo  Hospital Outpatient HIS - Cloud Database Deployment
echo ============================================================
echo.
python deploy_cloud_db.py %*
echo.
echo ------------------------------------------------------------
pause
