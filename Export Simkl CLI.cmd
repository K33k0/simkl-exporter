@echo off
setlocal
set "SIMKL_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%SIMKL_PYTHON%" goto launch
for %%V in (Python314 Python313 Python312 Python311) do if exist "%LOCALAPPDATA%\Programs\Python\%%V\python.exe" set "SIMKL_PYTHON=%LOCALAPPDATA%\Programs\Python\%%V\python.exe"
if exist "%SIMKL_PYTHON%" goto launch
echo Python 3.11 or newer is required. 1>&2
exit /b 1
:launch
"%SIMKL_PYTHON%" "%~dp0exporter.py" --cli %*
exit /b %errorlevel%
