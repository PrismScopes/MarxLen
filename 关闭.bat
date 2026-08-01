@echo off
chcp 65001 >nul
%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\stop.ps1"
pause
