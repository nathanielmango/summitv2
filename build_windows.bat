@echo off
setlocal
REM Builds Summit.exe. Run this on Windows, in the folder that holds app.py.
REM Needs Python 3.9+ installed (tick "Add Python to PATH" in the installer).
REM
REM Put these next to app.py before building (all optional except app.py + xtts_worker.py):
REM   summit.ico, summit.png   window / taskbar / toast icon
REM   tomas_Nikl.mp3           the voice used for dubbing
REM   ffmpeg.exe               otherwise it is copied from your PATH
REM   aria2c.exe               optional, makes video downloads faster

cd /d "%~dp0"

if not exist app.py goto :missing_app
if not exist xtts_worker.py goto :missing_worker

echo Installing packages...
python -m pip install --upgrade "yt-dlp[default]" "youtube-transcript-api>=1.0.0" deep-translator Pillow pytesseract pyinstaller || goto :error

set ICON=
if exist summit.ico set ICON=--icon summit.ico

REM yt-dlp's YouTube helper package (only bundled if it is installed)
set EXTRA=
python -c "import yt_dlp_ejs" >nul 2>nul && set EXTRA=--collect-all yt_dlp_ejs

echo Building Summit.exe (this takes a few minutes)...
python -m PyInstaller --noconfirm --clean --windowed --name Summit %ICON% ^
  --collect-all yt_dlp --collect-all deep_translator --collect-all youtube_transcript_api ^
  --hidden-import PIL.ImageTk --hidden-import pytesseract %EXTRA% ^
  --exclude-module torch --exclude-module TTS --exclude-module cv2 --exclude-module numpy ^
  app.py || goto :error

REM files the app expects next to Summit.exe (it reads them from its own folder)
if exist summit.png copy /y summit.png dist\Summit\ >nul
if exist summit.ico copy /y summit.ico dist\Summit\ >nul
copy /y xtts_worker.py dist\Summit\ >nul

if exist tomas_Nikl.mp3 copy /y tomas_Nikl.mp3 dist\Summit\ >nul
if not exist tomas_Nikl.mp3 echo WARNING: tomas_Nikl.mp3 not found - dubbing will not work in this build.

REM ffmpeg is needed to merge video + audio and to build the dub
if exist ffmpeg.exe copy /y ffmpeg.exe dist\Summit\ >nul
if not exist dist\Summit\ffmpeg.exe for /f "delims=" %%i in ('where ffmpeg 2^>nul') do copy /y "%%i" dist\Summit\ >nul
if not exist dist\Summit\ffmpeg.exe echo WARNING: ffmpeg.exe not found - put it next to app.py and build again.

REM aria2c is optional: with it the app downloads videos with 16 connections
if exist aria2c.exe copy /y aria2c.exe dist\Summit\ >nul
if not exist dist\Summit\aria2c.exe for /f "delims=" %%i in ('where aria2c 2^>nul') do copy /y "%%i" dist\Summit\ >nul

echo.
echo Done. The app is in  dist\Summit  (run Summit.exe inside it).
echo To make the installer, open Summit.iss in Inno Setup and press Compile (F9).
echo Or just zip the folder dist\Summit and send it.
echo.
echo Reminder: the XTTS voice engine (the separate Python with TTS installed) is NOT
echo bundled. On each computer, set the "XTTS Python" box in the app's settings.
pause
exit /b 0

:missing_app
echo.
echo app.py was not found. Run this file from the folder that holds app.py.
goto :error

:missing_worker
echo.
echo xtts_worker.py was not found next to app.py.
goto :error

:error
echo.
echo Build failed - see the messages above.
pause
exit /b 1
