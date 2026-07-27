@echo off
REM Builds the portable Windows package. Run from the repo root:
REM     packaging\build.bat
REM
REM Needs Python 3.10+ with the runtime dependencies installed, plus
REM pyinstaller. The result is dist\ModeShift\, zipped to
REM dist\ModeShift-windows-portable.zip.

setlocal
cd /d "%~dp0.."

echo Installing build dependencies...
python -m pip install --upgrade pyinstaller openrgb-python psutil pystray pillow pynput PySide6 || goto :err

echo.
echo Building...
python -m PyInstaller --clean --noconfirm packaging\modeshift.spec || goto :err

echo.
echo Zipping...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\ModeShift\*' -DestinationPath 'dist\ModeShift-windows-portable.zip' -Force" || goto :err

echo.
echo Done. Portable build: dist\ModeShift\
echo Zip for release:      dist\ModeShift-windows-portable.zip
goto :eof

:err
echo.
echo BUILD FAILED. See the output above.
exit /b 1
