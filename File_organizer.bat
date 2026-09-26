@echo off
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw file_organizer.py & exit /b)
where pythonw >nul 2>nul && (start "" pythonw file_organizer.py & exit /b)
echo Python was not found. Install it from https://www.python.org and tick "Add python.exe to PATH".
pause