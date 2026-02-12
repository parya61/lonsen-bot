@echo off
chcp 65001 >nul
title Lonsen Bot

if not exist venv (
    echo Создаю виртуальное окружение...
    python -m venv venv
    call venv\Scripts\activate
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate
)

if not exist .env (
    echo Файл .env не найден!
    echo Создай .env по примеру .env.example
    pause
    exit /b 1
)

echo Запуск бота...
python main.py
pause
