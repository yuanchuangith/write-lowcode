@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0database_settings.ps1"
if errorlevel 1 pause
