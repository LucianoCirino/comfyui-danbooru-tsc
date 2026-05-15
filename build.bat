@echo off
REM Rebuild danbooru.db from the CSVs in csv\.
REM Uses the ComfyUI portable's embedded Python so you don't need a system install.

setlocal
REM Force UTF-8 on stdout/stderr so Unicode in script output doesn't crash on Windows cp1252.
set "PYTHONIOENCODING=utf-8"
set "PACK=%~dp0"
if "%PACK:~-1%"=="\" set "PACK=%PACK:~0,-1%"
set "PYEXE=%PACK%\..\..\..\python_embeded\python.exe"

if not exist "%PYEXE%" (
    echo [danbooru-tsc] ERROR: embedded Python not found at:
    echo     %PYEXE%
    echo If your portable layout differs, edit this .bat and adjust PYEXE.
    pause
    exit /b 1
)

cd /d "%PACK%"
echo [danbooru-tsc] Running scripts\build_db.py ...
"%PYEXE%" scripts\build_db.py %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo [danbooru-tsc] build_db.py exited with code %RC%.
pause
exit /b %RC%
