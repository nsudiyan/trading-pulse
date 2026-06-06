@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo === Trading Bot Launcher ===
echo.

start "pump_detector" cmd /k "set PYTHONIOENCODING=utf-8 && python pump_detector.py watch"
timeout /t 2 /nobreak > nul

start "screener" cmd /k "set PYTHONIOENCODING=utf-8 && python screener.py --top-n 50"
timeout /t 2 /nobreak > nul

start "telegram_bot" cmd /k "set PYTHONIOENCODING=utf-8 && python telegram_bot.py daemon"
timeout /t 2 /nobreak > nul

start "sweep_watcher" cmd /k "set PYTHONIOENCODING=utf-8 && python sweep_watcher.py"
timeout /t 2 /nobreak > nul

echo Все 4 процесса запущены.
echo Закрой это окно.
