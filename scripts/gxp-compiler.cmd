@echo off
setlocal
set "GXP_COMPILER_PYTHON=%LOCALAPPDATA%\GxpLowcodeEditor\.venv\Scripts\python.exe"
if not exist "%GXP_COMPILER_PYTHON%" set "GXP_COMPILER_PYTHON=python"
"%GXP_COMPILER_PYTHON%" "%~dp0gxp_compiler.py" %*
exit /b %errorlevel%
