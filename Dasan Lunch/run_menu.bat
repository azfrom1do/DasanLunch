@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3.12 menu_collector_free.py
start "" "today_menu.html"
pause