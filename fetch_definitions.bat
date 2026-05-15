@echo off
REM Scrape Danbooru wiki definitions and rewrite csv\danbooru_tags_with_definitions.csv.
REM Pass extra args through to the script (e.g. --rate 3 --limit 1000).

setlocal
REM Force UTF-8 on stdout/stderr so Unicode in script output doesn't crash on Windows cp1252.
set "PYTHONIOENCODING=utf-8"
set "PACK=%~dp0"
if "%PACK:~-1%"=="\" set "PACK=%PACK:~0,-1%"
set "PYEXE=%PACK%\..\..\..\python_embeded\python.exe"

if not exist "%PYEXE%" (
    echo [danbooru-tsc] ERROR: embedded Python not found at:
    echo     %PYEXE%
    pause
    exit /b 1
)

cd /d "%PACK%"
echo [danbooru-tsc] Running scripts\fetch_tag_definitions.py ...
"%PYEXE%" scripts\fetch_tag_definitions.py %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo [danbooru-tsc] fetch_tag_definitions.py exited with code %RC%.
pause
exit /b %RC%
