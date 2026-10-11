@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "VENV_PY=%~dp0.runtime\venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo Ambiente virtual nao encontrado. Execute setup.bat primeiro.
    pause
    exit /b 1
)

"%VENV_PY%" -c "import fastapi, uvicorn, jinja2" >nul 2>&1
if errorlevel 1 (
    echo Dependencias ausentes no venv local. Execute setup.bat.
    pause
    exit /b 1
)

"%VENV_PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 9000
if errorlevel 1 (
    echo O SQL Server Advisor encerrou com erro.
    pause
    exit /b 1
)
exit /b 0