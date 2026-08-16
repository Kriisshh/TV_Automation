@echo off
REM Run this ONCE per release on a machine that has Python installed.
REM Bump __version__ in updater.py first, then run this, then publish a
REM GitHub release whose tag matches (e.g. v1.1.0) with the .exe attached.
REM Produces dist\ChromeSequencer.exe which needs NO Python to run.

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m PyInstaller --onefile --noconsole --name ChromeSequencer main.py

echo.
echo Done. Your standalone app is at:  dist\ChromeSequencer.exe
echo Next: attach that .exe to a GitHub release tagged to match __version__.
pause
