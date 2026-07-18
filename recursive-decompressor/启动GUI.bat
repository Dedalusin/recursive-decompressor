@echo off
chcp 65001 >nul
cd /d "%~dp0"

:: 查找 Python
set PY=
for %%p in (python python3 py) do (
    where %%p >nul 2>&1 && set PY=%%p && goto :found
)
echo [错误] 找不到 Python, 请安装 Python 3
pause
exit /b 1

:found
start "" "%PY%w" "%~dp0解压器GUI.pyw"
