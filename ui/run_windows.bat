@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0\.."
where py >nul 2>&1
if %ERRORLEVEL%==0 ( set "PY=py -3" ) else (
  where python >nul 2>&1
  if %ERRORLEVEL%==0 ( set "PY=python" ) else (
    echo X Python을 찾지 못했습니다.
    pause
    exit /b 1
  )
)
%PY% -c "import streamlit" >nul 2>&1
if errorlevel 1 (
  echo ^> 첫 실행 - 필요한 패키지를 설치합니다 ^(1~2분^)...
  %PY% -m pip install --upgrade pip
  %PY% -m pip install -r ui\requirements.txt
)
%PY% main.py
endlocal
