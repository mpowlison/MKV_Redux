@echo off
setlocal
if exist "%~dp0dist\MKV Redux.exe" (
    start "" "%~dp0dist\MKV Redux.exe"
    exit /b
)
where pyw.exe >nul 2>&1
if not errorlevel 1 (
    start "" pyw.exe -3 "%~dp0mkv_redux_gui.pyw"
    exit /b
)
where pythonw.exe >nul 2>&1
if not errorlevel 1 (
    start "" pythonw.exe "%~dp0mkv_redux_gui.pyw"
    exit /b
)
echo Python 3.10 or later with Tkinter is required to run MKV Redux.
echo Install it from https://www.python.org/downloads/windows/ and try again.
pause
