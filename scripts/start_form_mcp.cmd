@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_form_mcp.ps1"
exit /b %errorlevel%
