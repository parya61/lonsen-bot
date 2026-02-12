# Lonsen

Telegram-бот для запоминания английских слов. Добавляешь слова — бот помогает учить через карточки, квизы и интервальное повторение.

## Что умеет

**Добавление слов** — напиши слово или фразу на любом языке, бот переведёт и покажет карточку с транскрипцией, примером и синонимом. Или сразу пару: `word - перевод`.

**Учить** — одна кнопка, бот сам решает что повторить: сначала просроченные слова (spaced repetition), потом квиз по случайным.

**Тренировка** — ручной режим: выбираешь количество, направление (EN→RU / RU→EN / микс) и тип (ввод текстом или выбор из 4 вариантов).

**Автоквизы** — бот сам присылает вопросы в течение дня (10:00, 14:00, 18:00). До 3 попыток на вопрос с подсказками.

**Статистика** — серия дней, ранг, прогресс к дневной цели, точность за неделю.

**AI-чат** — просто напиши слово или вопрос по английскому в чат вне режимов — бот переведёт или объяснит.

## Запуск

Нужен Python 3.11+, PostgreSQL, токен бота (@BotFather) и ключ OpenRouter (openrouter.ai).

```bash
git clone https://github.com/your-username/lonsen-bot.git
cd lonsen-bot
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/Mac
pip install -r requirements.txt
```

Создай `.env`:

```
BOT_TOKEN=токен_от_botfather
AI_TOKEN=ключ_openrouter
DATABASE_URL=postgresql://user:password@localhost:5432/english_bot
```

Создай БД и запусти:

```bash
createdb english_bot
python main.py
```

Таблицы создадутся автоматически.

## Хостинг

Бесплатные варианты:

- **Railway.app** — $5/мес кредит, деплой из GitHub в 1 клик
- **Oracle Cloud** — бесплатный VPS навсегда (1 GB RAM)
- **Домашний ноут** — ~100 руб/мес за электричество

## Стек

aiogram 3 / asyncpg / PostgreSQL / APScheduler / OpenRouter API (Gemini 2.0 Flash)

## Лицензия

MIT
