@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "RUNTIME=%~dp0.runtime"
set "VENV=%RUNTIME%\venv"
set "VENV_PY=%VENV%\Scripts\python.exe"
set "ADVISOR_TEMP=%RUNTIME%\temp"

if not exist "%ADVISOR_TEMP%" mkdir "%ADVISOR_TEMP%"
set "TEMP=%ADVISOR_TEMP%"
set "TMP=%ADVISOR_TEMP%"

if not exist "%VENV_PY%" (
    echo Criando ambiente virtual em .runtime\venv ...
    py -3 -m venv "%VENV%"
    if errorlevel 1 (
        echo Nao foi possivel criar o ambiente virtual Python.
        pause
        exit /b 1
    )
) else (
    echo Ambiente virtual ja existe. Atualizando dependencias...
)

echo Atualizando pip...
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 (
    echo Nao foi possivel atualizar o pip.
    pause
    exit /b 1
)

echo Instalando dependencias no venv local...
"%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo Nao foi possivel instalar as dependencias.
    pause
    exit /b 1
)

echo.
echo Instalacao concluida em .runtime\venv
echo Execute start.bat e abra http://127.0.0.1:9000
pause
exit /b 0