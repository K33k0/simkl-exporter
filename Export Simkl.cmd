@echo off
setlocal
set "SIMKL_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe"
if exist "%SIMKL_PYTHON%" goto launch
for %%V in (Python314 Python313 Python312 Python311) do if exist "%LOCALAPPDATA%\Programs\Python\%%V\pythonw.exe" set "SIMKL_PYTHON=%LOCALAPPDATA%\Programs\Python\%%V\pythonw.exe"
if exist "%SIMKL_PYTHON%" goto launch
echo Python 3.11 or newer with Tkinter is required.
echo Install Python from https://www.python.org/downloads/windows/ then run exporter.py.
pause
exit /b 1
:launch
start "" "%SIMKL_PYTHON%" "%~dp0exporter.py"
exit /b 0
