@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\manage_project_sessions.ps1"
if errorlevel 1 pause
