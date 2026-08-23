@echo off
setlocal
cd /d "%~dp0"
if exist "runtime\pythonw.exe" (
  start "" "runtime\pythonw.exe" "%~dp0main.py"
) else (
  pyw -3.12 "%~dp0main.py"
)
endlocal
