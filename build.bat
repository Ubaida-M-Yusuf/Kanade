@echo off
rem Build dist\Kanade\Kanade.exe with PyInstaller (no Python needed to run it).
rem ffmpeg.exe / ffprobe.exe found on PATH are copied to dist\Kanade\ffmpeg\
rem (pass --no-ffmpeg to skip that; Kanade then uses FFmpeg from PATH or config.json).
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv 2>nul || python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean kanade.spec || exit /b 1

if /i "%~1"=="--no-ffmpeg" goto done
if not exist "dist\Kanade\ffmpeg" mkdir "dist\Kanade\ffmpeg"
for %%T in (ffmpeg ffprobe) do (
    for /f "delims=" %%P in ('where %%T 2^>nul') do (
        if not exist "dist\Kanade\ffmpeg\%%T.exe" copy /y "%%P" "dist\Kanade\ffmpeg\%%T.exe" >nul
    )
)
:done
echo.
echo Built: dist\Kanade\Kanade.exe
