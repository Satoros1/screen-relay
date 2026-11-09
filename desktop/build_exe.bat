@echo off
rem Builds dist\ScreenRelay.exe locally (the same thing GitHub Actions does)
cd /d "%~dp0"
py -m pip install -q -r requirements.txt pyinstaller
py -m PyInstaller --noconfirm --onefile --windowed --name ScreenRelay --icon assets\icon.ico --hidden-import pystray._win32 ScreenRelay.pyw
echo.
echo Done: %~dp0dist\ScreenRelay.exe
pause
