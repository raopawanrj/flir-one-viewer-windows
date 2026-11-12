@echo off
setlocal
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (where python >nul 2>nul && set PY=python)
if not defined PY (
  echo Python was not found. Install Python 3.10 or newer from python.org
  echo and tick "Add python.exe to PATH" in the installer, then run this again.
  goto :fail
)
if not exist .venv\setup_ok.txt (
  echo First run: installing the needed packages, this takes a few minutes...
  %PY% -m venv .venv || goto :fail
  .venv\Scripts\python -m pip install -r requirements.txt || goto :fail
  echo ok> .venv\setup_ok.txt
)
.venv\Scripts\python -m pip install -q pyinstaller || goto :fail
.venv\Scripts\pyinstaller --noconfirm --windowed --name FlirOneViewer --collect-all libusb_package app.py || goto :fail
echo Built dist\FlirOneViewer\FlirOneViewer.exe
pause
exit /b 0

:fail
echo.
echo ---- Something failed. See docs\troubleshooting.md, or open a GitHub issue with the text above. ----
pause
exit /b 1
