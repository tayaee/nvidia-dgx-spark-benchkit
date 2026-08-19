@echo off
rem web.bat - benchkit results dashboard (FastAPI) launcher
rem Usage: web.bat [--port 8001] [--host 127.0.0.1]
setlocal
cd /d %~dp0

set PORT=8001
set HOST=127.0.0.1

:parse
if "%~1"=="" goto run
if "%~1"=="--port" (set PORT=%~2 & shift & shift & goto parse)
if "%~1"=="--host" (set HOST=%~2 & shift & shift & goto parse)
if "%~1"=="-h" goto help
if "%~1"=="--help" goto help
echo unknown arg: %~1
exit /b 2

:help
echo usage: web.bat [--port N] [--host H]
exit /b 0

:run
set BENCHKIT_RESULTS_ROOT=%CD%\results
echo Results root : %BENCHKIT_RESULTS_ROOT%
echo Dashboard    : http://%HOST%:%PORT%

if exist .venv\Scripts\python.exe (
  set PY=.venv\Scripts\python.exe
) else (
  set PY=uv run --with fastapi --with "uvicorn[standard]" --python 3.12 python
)

%PY% -c "import os, uvicorn; from benchkit.webapp.app import create_app; app = create_app(os.environ.get('BENCHKIT_RESULTS_ROOT')); uvicorn.run(app, host='%HOST%', port=int('%PORT%'), log_level='warning')"
