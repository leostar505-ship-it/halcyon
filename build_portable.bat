@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller==6.22.3 || goto :fail
python -m PyInstaller halcyon_portable.spec --noconfirm --distpath portable --workpath build_portable || goto :fail
echo.
echo Done: %~dp0portable\Halcyon-Portable.exe
pause
exit /b 0
:fail
echo Build failed
pause
exit /b 1
