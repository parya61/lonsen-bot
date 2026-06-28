import asyncio
import os
import re
import json
import random
import sqlite3
from dotenv import load_dotenv
from datetime import datetime, date, timedelta, time as dt_time
from pathlib import Path
import tempfile

import httpx
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, Voice,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.fsm.context import FSMContext
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from openai import AsyncOpenAI

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
AI_TOKEN = os.getenv("AI_TOKEN")
DB_PATH = os.getenv("DB_PATH", "english.db")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN not set in .env")
if not AI_TOKEN:
    raise RuntimeError("AI_TOKEN not set in .env")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
dp = Dispatcher(storage=MemoryStorage())

db: aiosqlite.Connection | None = None  # type: ignore
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
openai_client = AsyncOpenAI(api_key=AI_TOKEN, base_url="https://openrouter.ai/api/v1")

# =====================================================================
#  FSM
# =====================================================================

class Learning(StatesGroup):
    adding_word = State()

class Train(StatesGroup):
    selecting_count = State()
    selecting_mode = State()
    selecting_type = State()
    in_quiz = State()

class SettingsMenu(StatesGroup):
    main = State()
    selecting_quiz_count = State()
    selecting_daily_goal = State()

class AutoQuiz(StatesGroup):
    answering = State()
    retry_with_hint = State()

class Flashcard(StatesGroup):
    reviewing = State()

class SmartLearn(StatesGroup):
    in_session = State()

class DictDelete(StatesGroup):
    waiting_input = State()

# =====================================================================
#  Buttons & Keyboards
# =====================================================================

BTN_ADD      = "Добавить"
BTN_DICT     = "Словарь"
BTN_LEARN    = "Учить"
BTN_TRAIN    = "Тренировка"
BTN_SETTINGS = "Настройки"
BTN_STATS    = "Статистика"
BTN_DONE     = "Готово"
BTN_BACK     = "Назад"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_DICT)],
        [KeyboardButton(text=BTN_LEARN), KeyboardButton(text=BTN_TRAIN)],
        [KeyboardButton(text=BTN_STATS), KeyboardButton(text=BTN_SETTINGS)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Слово, фраза или вопрос..."
)

ADD_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_DONE), KeyboardButton(text=BTN_DICT)]],
    resize_keyboard=True,
    input_field_placeholder="слово, фраза или word - перевод"
)

KB_COUNT = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="5"), KeyboardButton(text="10"), KeyboardButton(text="20")],
        [KeyboardButton(text=BTN_BACK)]
    ],
    resize_keyboard=True,
    input_field_placeholder="Количество слов"
)

KB_MODE = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="EN → RU"), KeyboardButton(text="RU → EN")],
        [KeyboardButton(text="Смешанный"), KeyboardButton(text=BTN_BACK)]
    ],
    resize_keyboard=True,
    input_field_placeholder="Направление"
)

KB_TRAIN_TYPE = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Ввод"), KeyboardButton(text="Выбор из 4")],
        [KeyboardButton(text=BTN_BACK)]
    ],
    resize_keyboard=True,
    input_field_placeholder="Тип тренировки"
)

QUIZ_EXIT_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_DONE)]],
    resize_keyboard=True
)

# =====================================================================
#  Helpers / LLM / JSON
# =====================================================================

TUTOR_SYSTEM = (
    "Ты репетитор английского. Отвечай кратко и по делу. "
    "Никаких рассуждений/chain-of-thought. Только готовый ответ."
)

def _chunk(text: str, max_len: int = 3900) -> list[str]:
    return [text[i:i+max_len] for i in range(0, len(text), max_len)]

def _norm(s: str) -> str:
    return " ".join((s or "").lower().strip().split())

def _is_done_text(s: str) -> bool:
    raw = (s or "").strip()
    if raw == BTN_DONE:
        return True
    t = _norm(raw)
    return t in {"готово", "закончить добавление", "завершить добавление", "done"}

def _is_back_text(s: str) -> bool:
    t = _norm(s)
    return t in {"назад", "back", BTN_BACK.lower()}

def strip_code_fences_only(s: str) -> str:
    if not s:
        return s
    s = re.sub(r"(?is)```(?:json|yaml)?\s*", "", s)
    s = s.replace("```", "")
    return s.strip()

def strip_llm_artifacts(s: str) -> str:
    if not s:
        return s
    s = re.sub(r"(?is)<\/?think(?:ing)?>.*?<\/think(?:ing)?>", "", s)
    s = re.sub(r"(?is)</?thinking>", "", s)
    s = strip_code_fences_only(s)
    return s.strip()

def _find_first_json_object(text: str) -> str | None:
    if not text:
        return None
    s = text
    candidate = _scan_braces_for_json(s)
    if candidate:
        return candidate
    s2 = strip_code_fences_only(s)
    candidate = _scan_braces_for_json(s2)
    if candidate:
        return candidate
    for m in re.finditer(r"(?is)<think(?:ing)?>\s*(.*?)\s*</think(?:ing)?>", s):
        cand = _scan_braces_for_json(m.group(1))
        if cand:
            return cand
    return None

def _scan_braces_for_json(s: str) -> str | None:
    in_str = False
    esc = False
    depth = 0
    start = -1
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        else:
            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start != -1:
                        return s[start:i+1]
    return None

def extract_json(raw: str):
    obj = _find_first_json_object(raw)
    if not obj:
        return None
    try:
        return json.loads(obj)
    except Exception:
        try:
            fixed = re.sub(r"(?<!\\)'", '"', obj)
            return json.loads(fixed)
        except Exception:
            return None

def detect_intent(text: str) -> str:
    t = (text or "").strip().lower()
    if t.startswith("объясни") or t.startswith("explain"):
        return "EXPLAIN"
    if t.startswith("переведи") or t.startswith("translate"):
        return "TRANSLATE"
    if len(t.split()) <= 4:
        return "TRANSLATE"
    return "QA"

def detect_lang(text: str) -> str:
    t = text or ""
    has_ru = re.search(r"[а-яё]", t, re.I) is not None
    has_en = re.search(r"[a-z]", t, re.I) is not None
    if has_ru and not has_en:
        return "ru"
    if has_en and not has_ru:
        return "en"
    return "mixed"

def parse_translate_input(text: str):
    t = (text or "").strip()
    low = t.lower()
    if "переведи" in low or "translate" in low:
        t_clean = re.sub(r"(^\s*(переведи|translate)\s*|\s*(переведи|translate)\s*$)", "", t, flags=re.I).strip()
        lang = detect_lang(t_clean)
        if lang == "en":
            direction = "en_ru"
        elif lang == "ru":
            direction = "ru_en"
        else:
            cnt_en = len(re.findall(r"[a-z]", t_clean, re.I))
            cnt_ru = len(re.findall(r"[а-яё]", t_clean, re.I))
            direction = "en_ru" if cnt_en >= cnt_ru else "ru_en"
        return t_clean, direction
    else:
        phrase = t
        lang = detect_lang(phrase)
        direction = "ru_en" if lang == "ru" else "en_ru"
        return phrase, direction

def build_messages_qa(user_text: str) -> list[dict]:
    return [
        {"role": "system", "content": TUTOR_SYSTEM + " Максимум 1-2 предложения."},
        {"role": "user", "content": user_text}
    ]

def build_messages_translate(phrase: str, direction: str) -> list[dict]:
    dir_text = "en->ru" if direction == "en_ru" else "ru->en"
    instruction = (
        f"Ты — словарь+репетитор. Направление перевода: {dir_text}.\n"
        "Верни СТРОГО JSON без лишнего текста:\n"
        "{"
        '"direction":"DIR",'
        '"source":"...",'
        '"translation":"...",'
        '"level":"A1|A2|B1|B2|C1|C2",'
        '"examples":["EN: ... — RU: ...", "EN: ... — RU: ...", "EN: ... — RU: ..."],'
        '"note":"краткая подсказка по употреблению (можно опустить)"'
        "}\n"
        "Ответ должен содержать ТОЛЬКО JSON. Никаких пояснений, примечаний, кодблоков, разметки и тегов."
    ).replace("DIR", dir_text)
    return [
        {"role": "system", "content": TUTOR_SYSTEM},
        {"role": "user", "content": instruction + f"\nТекст: {phrase}"}
    ]

def build_messages_explain(user_text: str) -> list[dict]:
    return [
        {"role": "system", "content": TUTOR_SYSTEM + " Дай мини-пояснение (3-5 строк) и 2 упражнения по делу."},
        {"role": "user", "content": f"Объясни кратко: {user_text}. Сначала правило (1-2 стр.), затем 2 упражнения с ответами в конце (кратко)."}
    ]

# =====================================================================
#  Ranks, Streak, Daily goal helpers
# =====================================================================

RANKS = [
    (1000, "Мастер"),
    (400,  "Эксперт"),
    (150,  "Знаток"),
    (50,   "Ученик"),
    (0,    "Новичок"),
]

def get_rank(total_correct: int) -> tuple[str, int]:
    for threshold, name in RANKS:
        if total_correct >= threshold:
            idx = RANKS.index((threshold, name))
            next_t = RANKS[idx - 1][0] if idx > 0 else threshold
            return name, next_t
    return "Новичок", 50

def progress_bar(pct: float, length: int = 10) -> str:
    filled = round(pct / 100 * length)
    return "▓" * filled + "░" * (length - filled)

async def ensure_user_settings(user_id: int):
    await db.execute(
        """
        INSERT OR IGNORE INTO user_settings (user_id, quiz_enabled, quiz_times, quiz_count, streak, last_practice_date, total_correct, daily_goal, today_answers)
        VALUES (?, 1, '10:00,14:00,18:00', 5, 0, NULL, 0, 5, 0)
        """,
        (user_id,)
    )
    await db.commit()

async def update_streak_and_goal(user_id: int, is_correct: bool) -> str | None:
    """Update streak, total_correct, daily goal progress. Returns message if goal reached."""
    await ensure_user_settings(user_id)
    today = date.today()

    cursor = await db.execute(
        "SELECT streak, last_practice_date, total_correct, daily_goal, today_answers FROM user_settings WHERE user_id = ?",
        (user_id,)
    )
    settings = await cursor.fetchone()

    streak = settings['streak'] or 0
    last_date_str = settings['last_practice_date']
    last_date = date.fromisoformat(last_date_str) if last_date_str else None
    total_correct = settings['total_correct'] or 0
    daily_goal = settings['daily_goal'] or 5
    today_answers = settings['today_answers'] or 0

    if is_correct:
        total_correct += 1

    # Reset today_answers if new day
    if last_date is None or last_date < today:
        today_answers = 1
        if last_date is not None and last_date == today - timedelta(days=1):
            streak += 1
        else:
            streak = 1

        await db.execute(
            """
            UPDATE user_settings
            SET streak = ?, last_practice_date = ?, total_correct = ?, today_answers = ?
            WHERE user_id = ?
            """,
            (streak, today.isoformat(), total_correct, today_answers, user_id)
        )
        await db.commit()
    else:
        today_answers += 1
        await db.execute(
            "UPDATE user_settings SET total_correct = ?, today_answers = ? WHERE user_id = ?",
            (total_correct, today_answers, user_id)
        )
        await db.commit()

    # Check if daily goal just reached
    if today_answers == daily_goal:
        return f"Цель дня выполнена: {daily_goal}/{daily_goal}"
    return None

# =====================================================================
#  LLM calls
# =====================================================================

FREE_MODELS = [
    "arcee-ai/trinity-large-preview:free",
    "google/gemma-3-12b-it:free",
    "google/gemma-3-27b-it:free",
]

async def generate_chat(messages: list[dict], max_tokens: int = 256, ensure_json: bool = False) -> str:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {AI_TOKEN}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://in_yaz_bot.local",
        "X-Title": "in_yaz_bot",
    }

    for model in FREE_MODELS:
        # Gemma models don't support system prompts — move to user
        if model.startswith("google/gemma") and len(messages) > 1 and messages[0]["role"] == "system":
            msgs = [{"role": "user", "content": messages[0]["content"] + "\n\n" + messages[1]["content"]}] + messages[2:]
        else:
            msgs = messages

        payload = {
            "model": model,
            "messages": msgs,
            "temperature": 0.15,
            "max_tokens": max_tokens,
        }
        if ensure_json:
            payload["response_format"] = {"type": "json_object"}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload, headers=headers)

                # If json_object not supported, retry without it
                if resp.status_code == 400 and ensure_json:
                    payload.pop("response_format", None)
                    resp = await client.post(url, json=payload, headers=headers)

                if resp.status_code == 429:
                    print(f"Rate limited: {model}, trying next...")
                    continue

                resp.raise_for_status()
                data = resp.json()
                content = (data.get("choices", [{}])[0].get("message") or {}).get("content", "")
                return content
        except Exception as e:
            print(f"LLM error ({model}): {repr(e)}")
            continue

    print("All models failed")
    return ""

async def judge_semantic(direction: str, question: str, expected: str, user_ans: str) -> tuple[bool, str]:
    messages = [
        {"role": "system", "content": "Ты экзаменатор английского. Проверь ответ студента."},
        {
            "role": "user",
            "content": f"""Направление перевода: {direction}
Вопрос: {question}
Правильный ответ: {expected}
Ответ студента: {user_ans}

Оцени ответ студента и верни JSON:
{{
  "correct": true/false,
  "feedback": "краткая обратная связь (если есть ошибка, укажи её и правильный вариант)"
}}

Считай ответ правильным если:
- Полностью совпадает с эталоном
- Используется синоним (например, "big" вместо "large")
- Есть незначительные опечатки (1-2 буквы)

Считай ответ неправильным если:
- Смысл отличается
- Грубые ошибки (более 2 опечаток)
- Неправильная грамматическая форма
"""
        }
    ]
    try:
        raw = await generate_chat(messages, max_tokens=100, ensure_json=True)
        data = extract_json(raw)
        if data:
            return data.get("correct", False), data.get("feedback", "")
    except Exception as e:
        print(f"judge_semantic error: {e}")
    return _norm(user_ans) == _norm(expected), ""

async def ai_word_card(text: str) -> dict | None:
    """Get word card with translation, transcription, example, synonym.
    Works for single words AND phrases, in both directions (en->ru, ru->en).
    """
    lang = detect_lang(text)
    if lang == "ru":
        direction_hint = "ru->en"
        task = f'Переведи на английский: "{text}"'
    else:
        direction_hint = "en->ru"
        task = f'Переведи на русский: "{text}"'

    messages = [
        {"role": "system", "content": (
            "Ты словарь-переводчик. Отвечай СТРОГО JSON без пояснений, кодблоков и markdown.\n"
            "Поле english — ВСЕГДА английский текст.\n"
            "Поле translation — ВСЕГДА русский перевод."
        )},
        {"role": "user", "content": f"""{task}
Верни JSON:
{{
  "english": "английское слово или фраза",
  "translation": "русский перевод",
  "transcription": "IPA транскрипция английского слова (только для 1-2 слов, иначе пусто)",
  "example": "EN: пример — RU: перевод",
  "synonym": "английский синоним (если есть, иначе пусто)"
}}"""}
    ]
    raw = await generate_chat(messages, max_tokens=200, ensure_json=True)
    card = extract_json(raw)
    if not card:
        return None

    # Validate: english must have latin chars, translation must have cyrillic
    eng = (card.get("english") or "").strip()
    rus = (card.get("translation") or "").strip()
    if not eng or not rus:
        return None

    # Fix swapped fields
    eng_has_latin = bool(re.search(r"[a-z]", eng, re.I))
    rus_has_cyrillic = bool(re.search(r"[а-яё]", rus, re.I))
    if not eng_has_latin and rus_has_cyrillic:
        pass  # ok
    elif not eng_has_latin and not rus_has_cyrillic:
        return None
    if not rus_has_cyrillic and eng_has_latin:
        # Maybe swapped
        if re.search(r"[а-яё]", eng, re.I) and re.search(r"[a-z]", rus, re.I):
            card["english"], card["translation"] = rus, eng

    return card

async def transcribe_voice(voice_file_path: str) -> str:
    try:
        with open(voice_file_path, "rb") as audio_file:
            response = await openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="en"
            )
        return response.text.strip()
    except Exception as e:
        print(f"Transcription error: {e}")
        return ""

# =====================================================================
#  Smart reminders (scheduler jobs)
# =====================================================================

async def send_morning_word(user_id: int):
    """Send word of the day from user's vocabulary."""
    try:
        cursor = await db.execute(
            "SELECT english, translation FROM vocabulary WHERE user_id = ? ORDER BY random() LIMIT 1",
            (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return

        # Get a quick example
        messages = [
            {"role": "system", "content": "Ты словарь. Дай 1 короткий пример использования слова в предложении. Формат: EN: ... — RU: ..."},
            {"role": "user", "content": row['english']}
        ]
        raw = await generate_chat(messages, max_tokens=60)
        example = strip_llm_artifacts(raw).strip()

        text = f"*{row['english']}* — {row['translation']}"
        if example:
            text += f"\n\n{example}"

        await bot.send_message(user_id, text)
    except Exception as e:
        print(f"Morning word error for {user_id}: {e}")

async def send_evening_reminder(user_id: int):
    """Remind user if they haven't practiced today."""
    try:
        cursor = await db.execute(
            "SELECT last_practice_date, streak FROM user_settings WHERE user_id = ?",
            (user_id,)
        )
        settings = await cursor.fetchone()
        if not settings:
            return

        today = date.today()
        if settings['last_practice_date'] == today.isoformat():
            return  # already practiced

        cursor = await db.execute(
            "SELECT COUNT(*) FROM vocabulary WHERE user_id = ? AND sr_next_review <= datetime('now')",
            (user_id,)
        )
        row = await cursor.fetchone()
        due_count = row[0] if row else 0

        streak = settings['streak'] or 0
        text = f"{due_count} слов к повторению"
        if streak > 0:
            text += f"  /  серия {streak} д."

        await bot.send_message(
            user_id,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Учить", callback_data="smart_learn_start")]
            ])
        )
    except Exception as e:
        print(f"Evening reminder error for {user_id}: {e}")

async def send_morning_to_all():
    try:
        cursor = await db.execute(
            "SELECT DISTINCT user_id FROM user_settings WHERE quiz_enabled = 1"
        )
        users = await cursor.fetchall()
        for u in users:
            await send_morning_word(u['user_id'])
    except Exception as e:
        print(f"Morning broadcast error: {e}")

async def send_evening_to_all():
    try:
        cursor = await db.execute(
            "SELECT DISTINCT user_id FROM user_settings WHERE quiz_enabled = 1"
        )
        users = await cursor.fetchall()
        for u in users:
            await send_evening_reminder(u['user_id'])
    except Exception as e:
        print(f"Evening broadcast error: {e}")

# =====================================================================
#  Auto-quizzes (scheduler)
# =====================================================================

async def send_auto_quiz_to_user(user_id: int):
    try:
        cursor = await db.execute(
            "SELECT quiz_count FROM user_settings WHERE user_id = ?", (user_id,)
        )
        settings = await cursor.fetchall()
        quiz_count = settings[0]['quiz_count'] if settings else 5

        cursor = await db.execute(
            "SELECT id, english, translation FROM vocabulary WHERE user_id = ? ORDER BY random() LIMIT ?",
            (user_id, quiz_count)
        )
        rows = await cursor.fetchall()
        if not rows:
            return

        await db.execute("DELETE FROM active_quizzes WHERE user_id = ?", (user_id,))

        for r in rows:
            direction = random.choice(["en_ru", "ru_en"])
            if direction == "en_ru":
                question = f"Переведи на русский: *{r['english']}*"
                expected = r['translation']
            else:
                question = f"Переведи на английский: *{r['translation']}*"
                expected = r['english']

            await db.execute(
                "INSERT INTO active_quizzes (user_id, question, expected_answer, direction, word_id) VALUES (?,?,?,?,?)",
                (user_id, question, expected, direction, r['id'])
            )

        await db.commit()

        cursor = await db.execute(
            "SELECT id, question FROM active_quizzes WHERE user_id = ? ORDER BY id LIMIT 1", (user_id,)
        )
        first_quiz = await cursor.fetchone()
        if first_quiz:
            await bot.send_message(
                user_id,
                f"Практика\n\n{first_quiz['question']}",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text="Пропустить")]],
                    resize_keyboard=True
                )
            )
    except Exception as e:
        print(f"Error sending auto quiz to {user_id}: {e}")

async def schedule_quizzes_for_all_users():
    try:
        cursor = await db.execute(
            "SELECT DISTINCT user_id FROM user_settings WHERE quiz_enabled = 1"
        )
        users = await cursor.fetchall()
        for user in users:
            await send_auto_quiz_to_user(user['user_id'])
    except Exception as e:
        print(f"Error in schedule_quizzes_for_all_users: {e}")

def setup_scheduler():
    # Morning word at 9:00
    scheduler.add_job(send_morning_to_all, CronTrigger(hour=9, minute=0), id='morning_word', replace_existing=True)
    # Auto quizzes at 10, 14, 18
    scheduler.add_job(schedule_quizzes_for_all_users, CronTrigger(hour=10, minute=0), id='quiz_10', replace_existing=True)
    scheduler.add_job(schedule_quizzes_for_all_users, CronTrigger(hour=14, minute=0), id='quiz_14', replace_existing=True)
    scheduler.add_job(schedule_quizzes_for_all_users, CronTrigger(hour=18, minute=0), id='quiz_18', replace_existing=True)
    # Evening reminder at 21:00
    scheduler.add_job(send_evening_to_all, CronTrigger(hour=21, minute=0), id='evening_remind', replace_existing=True)
    scheduler.start()
    print("Scheduler started")

# =====================================================================
#  HANDLERS: /start
# =====================================================================

@dp.message(Command("start"))
async def command_start_handler(message: Message, state: FSMContext):
    await state.clear()
    await ensure_user_settings(message.from_user.id)
    await message.answer(
        "*Lonsen*\n\n"
        "Словарь и практика английского.\n"
        "Пиши слово или фразу — переведу.",
        reply_markup=MAIN_KB
    )

# =====================================================================
#  HANDLERS: Add words (with context card)
# =====================================================================

@dp.message(StateFilter(default_state), F.text == BTN_ADD)
@dp.message(StateFilter(default_state), Command("learn"))
async def enter_learning_mode(message: Message, state: FSMContext):
    await message.answer(
        "Добавление\n\n"
        "Напиши слово или фразу — переведу.\n"
        "Или сразу пару: `word - перевод`",
        reply_markup=ADD_KB
    )
    await state.set_state(Learning.adding_word)

@dp.message(Learning.adding_word)
async def add_word_process(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if _is_done_text(text):
        await state.clear()
        await message.answer("Добавление завершено.", reply_markup=MAIN_KB)
        return

    if text == BTN_DICT:
        await state.clear()
        await show_dictionary(message)
        return

    if text == BTN_ADD:
        await message.answer("Напиши слово или фразу.", reply_markup=ADD_KB)
        return

    if not text:
        await message.answer("Пусто.", reply_markup=ADD_KB)
        return

    # Check if user explicitly provided a pair: "word - перевод"
    # Must have " - " (with spaces) to distinguish from hyphenated words like "well-known"
    is_pair = False
    if " - " in text:
        parts = text.split(" - ", 1)
        left, right = parts[0].strip(), parts[1].strip()
        if left and right:
            is_pair = True

    if is_pair:
        # Determine which is english, which is russian
        left_lang = detect_lang(left)
        right_lang = detect_lang(right)

        if left_lang == "en" and right_lang == "ru":
            eng, rus = left, right
        elif left_lang == "ru" and right_lang == "en":
            eng, rus = right, left
        else:
            # Ambiguous — treat left as english
            eng, rus = left, right

        try:
            await db.execute(
                "INSERT INTO vocabulary (user_id, english, translation) VALUES(?, ?, ?)",
                (message.from_user.id, eng, rus)
            )
            await db.commit()
            await message.answer(f"*{eng}* — {rus}", reply_markup=ADD_KB)
        except Exception as e:
            print("DB insert error:", repr(e))
            await message.answer("Ошибка сохранения.", reply_markup=ADD_KB)
    else:
        # Auto-translate: single word, phrase, or sentence
        card = await ai_word_card(text)

        if not card or not card.get("english") or not card.get("translation"):
            # Fallback: simple translate
            lang = detect_lang(text)
            if lang == "ru":
                prompt_dir = "Переведи на английский"
            else:
                prompt_dir = "Переведи на русский"

            from_ai = strip_llm_artifacts(await generate_chat([
                {"role": "system", "content": "Ты словарь. Отвечай ТОЛЬКО переводом, ничего больше."},
                {"role": "user", "content": f"{prompt_dir}: {text}"}
            ], max_tokens=50)).strip().strip('"').strip("'")

            if not from_ai:
                await message.answer("Не удалось перевести. Попробуй: `word - перевод`", reply_markup=ADD_KB)
                return

            eng = from_ai if lang == "ru" else text
            rus = text if lang == "ru" else from_ai
            await state.update_data(pending_eng=eng, pending_rus=rus)
            await message.answer(
                f"*{eng}* — {rus}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="Сохранить", callback_data="save_word"),
                        InlineKeyboardButton(text="Изменить", callback_data="edit_word"),
                        InlineKeyboardButton(text="Отмена", callback_data="cancel_word"),
                    ]
                ])
            )
            return

        eng = card["english"]
        rus = card["translation"]
        transcription = (card.get("transcription") or "").strip()
        example = (card.get("example") or "").strip()
        synonym = (card.get("synonym") or "").strip()

        await state.update_data(pending_eng=eng, pending_rus=rus)

        lines = [f"*{eng}* — {rus}"]
        if transcription:
            lines.append(f"/{transcription}/")
        if example:
            lines.append(f"\n{example}")
        if synonym:
            lines.append(f"~{synonym}")

        await message.answer(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="Сохранить", callback_data="save_word"),
                    InlineKeyboardButton(text="Изменить", callback_data="edit_word"),
                    InlineKeyboardButton(text="Отмена", callback_data="cancel_word"),
                ]
            ])
        )

@dp.callback_query(F.data == "save_word")
async def save_word_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    eng = data.get("pending_eng")
    rus = data.get("pending_rus")
    if eng and rus:
        await db.execute(
            "INSERT INTO vocabulary (user_id, english, translation) VALUES(?, ?, ?)",
            (callback.from_user.id, eng, rus)
        )
        await db.commit()
        await callback.message.edit_text(callback.message.text + "\n\n(сохранено)")
    else:
        await callback.message.edit_text("Нет данных для сохранения.")
    await state.update_data(pending_eng=None, pending_rus=None)
    await callback.answer()

@dp.callback_query(F.data == "edit_word")
async def edit_word_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введи в формате: `word - перевод`")
    await callback.answer()

@dp.callback_query(F.data == "cancel_word")
async def cancel_word_callback(callback: CallbackQuery, state: FSMContext):
    await state.update_data(pending_eng=None, pending_rus=None)
    await callback.message.edit_text("Отменено.")
    await callback.answer()

# =====================================================================
#  HANDLERS: Dictionary (with delete)
# =====================================================================

@dp.message(StateFilter(default_state), F.text == BTN_DICT)
async def show_dictionary(message: Message):
    try:
        cursor = await db.execute(
            "SELECT id, english, translation FROM vocabulary WHERE user_id = ? ORDER BY added_at DESC LIMIT 50",
            (message.from_user.id,)
        )
        rows = await cursor.fetchall()
    except Exception as e:
        print("DB select error:", repr(e))
        await message.answer("Ошибка загрузки словаря.")
        return

    if not rows:
        await message.answer("Словарь пуст.", reply_markup=MAIN_KB)
        return

    lines = [f"{i+1}. {r['english']} — {r['translation']}" for i, r in enumerate(rows)]
    text = "Словарь\n\n" + "\n".join(lines)

    dict_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Удалить слово")],
            [KeyboardButton(text=BTN_BACK)]
        ],
        resize_keyboard=True
    )

    for part in _chunk(text):
        await message.answer(part, reply_markup=dict_kb)

@dp.message(StateFilter(default_state), F.text == "Удалить слово")
async def start_delete_word(message: Message, state: FSMContext):
    await message.answer(
        "Введи номер слова из словаря или само слово.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=BTN_BACK)]],
            resize_keyboard=True
        )
    )
    await state.set_state(DictDelete.waiting_input)

@dp.message(DictDelete.waiting_input)
async def process_delete_word(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    if _is_back_text(text):
        await state.clear()
        await message.answer("Главное меню.", reply_markup=MAIN_KB)
        return

    user_id = message.from_user.id

    if text.isdigit():
        idx = int(text) - 1
        cursor = await db.execute(
            "SELECT id, english, translation FROM vocabulary WHERE user_id = ? ORDER BY added_at DESC LIMIT 50",
            (user_id,)
        )
        rows = await cursor.fetchall()
        if 0 <= idx < len(rows):
            row = rows[idx]
            await db.execute("DELETE FROM vocabulary WHERE id = ?", (row['id'],))
            await db.commit()
            await state.clear()
            await message.answer(f"Удалено: *{row['english']}* — {row['translation']}", reply_markup=MAIN_KB)
            return
        else:
            await message.answer("Нет слова с таким номером.")
            return

    cursor = await db.execute(
        "SELECT id, english, translation FROM vocabulary WHERE user_id = ? AND (LOWER(english) = ? OR LOWER(translation) = ?) LIMIT 1",
        (user_id, text.lower(), text.lower())
    )
    row = await cursor.fetchone()
    if row:
        await db.execute("DELETE FROM vocabulary WHERE id = ?", (row['id'],))
        await db.commit()
        await state.clear()
        await message.answer(f"Удалено: *{row['english']}* — {row['translation']}", reply_markup=MAIN_KB)
    else:
        await message.answer("Слово не найдено.")

# =====================================================================
#  HANDLERS: Smart Learn (one-button learning)
# =====================================================================

@dp.message(StateFilter(default_state), F.text == BTN_LEARN)
async def smart_learn_handler(message: Message, state: FSMContext):
    await _start_smart_learn(message.chat.id, message.from_user.id, state)

@dp.callback_query(F.data == "smart_learn_start")
async def smart_learn_callback(callback: CallbackQuery, state: FSMContext):
    await _start_smart_learn(callback.message.chat.id, callback.from_user.id, state)
    await callback.answer()

async def _start_smart_learn(chat_id: int, user_id: int, state: FSMContext):
    """Smart learn: SR words first, then random quiz."""
    # 1. Check for SR-due words
    cursor = await db.execute(
        """
        SELECT id, english, translation, sr_interval, sr_ease
        FROM vocabulary
        WHERE user_id = ? AND sr_next_review <= datetime('now')
        ORDER BY sr_next_review ASC
        LIMIT 10
        """,
        (user_id,)
    )
    sr_rows = await cursor.fetchall()

    if sr_rows:
        # Start flashcard mode
        cards = [dict(r) for r in sr_rows]
        await state.update_data(cards=cards, card_idx=0, known=0, unknown=0)
        await state.set_state(Flashcard.reviewing)

        card = cards[0]
        if random.random() < 0.5:
            shown = card['english']
            hidden_label = "перевод"
        else:
            shown = card['translation']
            hidden_label = "english"
        await state.update_data(current_shown=shown, current_hidden_label=hidden_label)

        await bot.send_message(
            chat_id,
            f"Повторение  {len(cards)} сл.\n\n[1/{len(cards)}] *{shown}*",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Показать", callback_data="fc_show")]
            ])
        )
        return

    # 2. No SR words — random quiz (5 words, mixed, quick)
    cursor = await db.execute(
        "SELECT id, english, translation FROM vocabulary WHERE user_id = ? ORDER BY random() LIMIT 5",
        (user_id,)
    )
    rows = await cursor.fetchall()
    if not rows:
        await bot.send_message(chat_id, "Словарь пуст. Добавь слова.", reply_markup=MAIN_KB)
        return

    tasks = []
    for r in rows:
        direction = random.choice(["en_ru", "ru_en"])
        if direction == "en_ru":
            tasks.append((f"Переведи на русский: *{r['english']}*", r['translation'], "en_ru", r["id"]))
        else:
            tasks.append((f"Переведи на английский: *{r['translation']}*", r['english'], "ru_en", r["id"]))

    await state.update_data(tasks=tasks, idx=0, score=0, train_type="quick")
    await state.set_state(Train.in_quiz)
    await _send_quick_quiz_question(chat_id, user_id, state, tasks, 0)

# =====================================================================
#  HANDLERS: Training (input mode + quick quiz)
# =====================================================================

@dp.message(StateFilter(default_state), F.text == BTN_TRAIN)
async def train_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Количество слов:", reply_markup=KB_COUNT)
    await state.set_state(Train.selecting_count)

@dp.message(Train.selecting_count)
async def train_pick_count(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if _is_back_text(text):
        await state.clear()
        await message.answer("Главное меню.", reply_markup=MAIN_KB)
        return
    if text not in {"5", "10", "20"}:
        await message.answer("Выбери 5, 10 или 20.", reply_markup=KB_COUNT)
        return
    await state.update_data(count=int(text))
    await message.answer("Направление:", reply_markup=KB_MODE)
    await state.set_state(Train.selecting_mode)

@dp.message(Train.selecting_mode)
async def train_pick_mode(message: Message, state: FSMContext):
    mode_text = (message.text or "").strip().lower()
    if _is_back_text(mode_text):
        await state.clear()
        await message.answer("Главное меню.", reply_markup=MAIN_KB)
        return

    modes = {"en → ru": "en_ru", "ru → en": "ru_en", "смешанный": "mix"}
    mode_key = None
    for k, v in modes.items():
        if mode_text == k.lower():
            mode_key = v
            break
    if not mode_key:
        await message.answer("Выбери: EN → RU, RU → EN или Смешанный.", reply_markup=KB_MODE)
        return

    await state.update_data(mode=mode_key)
    await message.answer("Тип тренировки:", reply_markup=KB_TRAIN_TYPE)
    await state.set_state(Train.selecting_type)

@dp.message(Train.selecting_type)
async def train_pick_type(message: Message, state: FSMContext):
    text = (message.text or "").strip().lower()
    if _is_back_text(text):
        await state.clear()
        await message.answer("Главное меню.", reply_markup=MAIN_KB)
        return

    if text not in {"ввод", "выбор из 4"}:
        await message.answer("Выбери: Ввод или Выбор из 4.", reply_markup=KB_TRAIN_TYPE)
        return

    data = await state.get_data()
    count = data.get("count", 5)
    mode_key = data.get("mode", "mix")
    train_type = "input" if text == "ввод" else "quick"

    cursor = await db.execute(
        "SELECT id, english, translation FROM vocabulary WHERE user_id = ? ORDER BY random() LIMIT ?",
        (message.from_user.id, count)
    )
    rows = await cursor.fetchall()
    if not rows:
        await message.answer("Словарь пуст.", reply_markup=MAIN_KB)
        await state.clear()
        return

    tasks = []
    for r in rows:
        en = r["english"]
        ru = r["translation"]
        direction = mode_key if mode_key != "mix" else random.choice(["en_ru", "ru_en"])
        if direction == "en_ru":
            tasks.append((f"Переведи на русский: *{en}*", ru, "en_ru", r["id"]))
        else:
            tasks.append((f"Переведи на английский: *{ru}*", en, "ru_en", r["id"]))

    await state.update_data(tasks=tasks, idx=0, score=0, train_type=train_type)

    if train_type == "quick":
        await _send_quick_quiz_question(message.chat.id, message.from_user.id, state, tasks, 0)
    else:
        q, _, _, _ = tasks[0]
        await message.answer(f"[1/{len(tasks)}] {q}", reply_markup=QUIZ_EXIT_KB)
    await state.set_state(Train.in_quiz)


async def _send_quick_quiz_question(chat_id: int, user_id: int, state: FSMContext, tasks: list, idx: int):
    """Send a multiple-choice question with 4 inline buttons. FIX: uses explicit user_id."""
    q, correct_answer, direction, word_id = tasks[idx]

    col = "translation" if direction == "en_ru" else "english"

    cursor = await db.execute(
        f"SELECT {col} FROM vocabulary WHERE user_id = ? AND {col} != ? ORDER BY random() LIMIT 3",
        (user_id, correct_answer)
    )
    wrong_rows = await cursor.fetchall()
    options = [correct_answer] + [r[col] for r in wrong_rows]

    # Pad with placeholder if not enough
    while len(options) < 4:
        options.append("...")
    random.shuffle(options)

    buttons = []
    for i, opt in enumerate(options):
        cb_data = f"qq_{idx}_{i}_{'1' if opt == correct_answer else '0'}"
        buttons.append([InlineKeyboardButton(text=opt, callback_data=cb_data)])

    await bot.send_message(
        chat_id,
        f"[{idx+1}/{len(tasks)}] {q}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data.startswith("qq_"))
async def quick_quiz_answer(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    idx = int(parts[1])
    is_correct = parts[3] == "1"

    data = await state.get_data()
    tasks = data.get("tasks", [])
    score = data.get("score", 0)

    if idx >= len(tasks):
        await callback.answer()
        return

    _, correct_answer, direction, word_id = tasks[idx]
    user_id = callback.from_user.id

    if is_correct:
        score += 1
        await callback.message.edit_text(callback.message.text + "\n\nВерно")
    else:
        await callback.message.edit_text(callback.message.text + f"\n\nНеверно — *{correct_answer}*")

    goal_msg = await update_streak_and_goal(user_id, is_correct)

    await db.execute(
        "INSERT INTO quiz_stats (user_id, word_id, is_correct, attempts_count) VALUES (?,?,?,1)",
        (user_id, word_id, is_correct)
    )
    await db.commit()

    idx += 1
    await state.update_data(idx=idx, score=score)

    if idx < len(tasks):
        await _send_quick_quiz_question(callback.message.chat.id, user_id, state, tasks, idx)
    else:
        total = len(tasks)
        pct = score / total * 100 if total > 0 else 0
        result_text = f"Результат\n\n{score}/{total} ({pct:.0f}%)"
        if goal_msg:
            result_text += f"\n\n{goal_msg}"
        await callback.message.answer(result_text, reply_markup=MAIN_KB)
        await state.clear()

    await callback.answer()


@dp.message(Train.in_quiz)
async def train_in_quiz(message: Message, state: FSMContext):
    data = await state.get_data()
    train_type = data.get("train_type", "input")

    if train_type == "quick":
        if _is_done_text((message.text or "").strip()):
            score = data.get("score", 0)
            total = len(data.get("tasks", []))
            await message.answer(f"Результат\n\n{score}/{total}", reply_markup=MAIN_KB)
            await state.clear()
        return

    # Input mode
    if message.voice:
        try:
            voice_file = await bot.get_file(message.voice.file_id)
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".ogg")
            await bot.download_file(voice_file.file_path, temp_file.name)
            temp_file.close()
            text = await transcribe_voice(temp_file.name)
            os.unlink(temp_file.name)
            if not text:
                await message.answer("Не удалось распознать. Попробуй текстом.")
                return
            await message.answer(f"Распознано: *{text}*")
        except Exception as e:
            print(f"Voice error: {e}")
            await message.answer("Ошибка голоса. Напиши текстом.")
            return
    else:
        text = (message.text or "").strip()

    if _is_done_text(text):
        score = data.get("score", 0)
        total = len(data.get("tasks", []))
        await message.answer(f"Результат\n\n{score}/{total}", reply_markup=MAIN_KB)
        await state.clear()
        return

    tasks = data.get("tasks", [])
    idx = data.get("idx", 0)
    score = data.get("score", 0)

    if idx >= len(tasks):
        await message.answer(f"Результат\n\n{score}/{len(tasks)}", reply_markup=MAIN_KB)
        await state.clear()
        return

    question, expected, direction, word_id = tasks[idx]
    correct, feedback = await judge_semantic(direction, question, expected, text)

    goal_msg = await update_streak_and_goal(message.from_user.id, correct)

    await db.execute(
        "INSERT INTO quiz_stats (user_id, word_id, is_correct, attempts_count) VALUES (?,?,?,1)",
        (message.from_user.id, word_id, correct)
    )
    await db.commit()

    if correct:
        score += 1
        msg = "Верно" + (f"  {feedback}" if feedback else "")
    else:
        msg = f"Неверно — *{expected}*" + (f"\n{feedback}" if feedback else "")

    await message.answer(msg)
    if goal_msg:
        await message.answer(goal_msg)

    idx += 1
    if idx < len(tasks):
        next_q, _, _, _ = tasks[idx]
        await message.answer(f"[{idx+1}/{len(tasks)}] {next_q}")
        await state.update_data(idx=idx, score=score)
    else:
        total = len(tasks)
        pct = score / total * 100 if total > 0 else 0
        await message.answer(f"Результат\n\n{score}/{total} ({pct:.0f}%)", reply_markup=MAIN_KB)
        await state.clear()

# =====================================================================
#  HANDLERS: Flashcards (Spaced Repetition) — FIXED
# =====================================================================

@dp.callback_query(F.data == "fc_show")
async def flashcard_show(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cards = data.get("cards", [])
    idx = data.get("card_idx", 0)

    if idx >= len(cards):
        await callback.answer()
        return

    card = cards[idx]
    hidden_label = data.get("current_hidden_label", "перевод")

    if hidden_label == "перевод":
        answer = card['translation']
    else:
        answer = card['english']

    shown = data.get("current_shown", "")

    try:
        await callback.message.edit_text(
            f"*{shown}* — {answer}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="Знаю", callback_data="fc_know"),
                    InlineKeyboardButton(text="Не знаю", callback_data="fc_dont"),
                ]
            ])
        )
    except Exception:
        pass
    await callback.answer()

@dp.callback_query(F.data == "fc_know")
async def flashcard_know(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _fc_process(callback, state, knew=True)
    await callback.answer()

@dp.callback_query(F.data == "fc_dont")
async def flashcard_dont_know(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _fc_process(callback, state, knew=False)
    await callback.answer()

async def _fc_process(callback: CallbackQuery, state: FSMContext, knew: bool):
    """Process flashcard answer and advance to next card. FIXED: explicit chat_id/user_id."""
    data = await state.get_data()
    cards = data.get("cards", [])
    idx = data.get("card_idx", 0)
    known = data.get("known", 0)
    unknown = data.get("unknown", 0)

    chat_id = callback.message.chat.id
    user_id = callback.from_user.id

    if idx < len(cards):
        card = cards[idx]
        word_id = card['id']
        sr_interval = card['sr_interval'] or 0
        sr_ease = float(card['sr_ease'] or 2.5)

        if knew:
            known += 1
            new_interval = max(1, int(sr_interval * sr_ease)) if sr_interval > 0 else 1
            sr_ease = min(3.0, sr_ease * 1.1)
        else:
            unknown += 1
            new_interval = 1
            sr_ease = max(1.3, sr_ease * 0.8)

        try:
            next_review = (datetime.now() + timedelta(days=new_interval)).isoformat()
            await db.execute(
                """
                UPDATE vocabulary
                SET sr_interval = ?, sr_ease = ?, sr_next_review = ?
                WHERE id = ?
                """,
                (new_interval, sr_ease, next_review, word_id)
            )
            await db.commit()
        except Exception as e:
            print(f"SR update error: {e}")

        await update_streak_and_goal(user_id, knew)

    idx += 1
    await state.update_data(card_idx=idx, known=known, unknown=unknown)

    if idx < len(cards):
        card = cards[idx]
        if random.random() < 0.5:
            shown = card['english']
            hidden_label = "перевод"
        else:
            shown = card['translation']
            hidden_label = "english"
        await state.update_data(current_shown=shown, current_hidden_label=hidden_label)

        await bot.send_message(
            chat_id,
            f"[{idx+1}/{len(cards)}] *{shown}*",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Показать", callback_data="fc_show")]
            ])
        )
    else:
        await bot.send_message(
            chat_id,
            f"Повторение\n\n"
            f"Знал: {known}  |  не знал: {unknown}  |  всего: {known + unknown}",
            reply_markup=MAIN_KB
        )
        await state.clear()

# =====================================================================
#  HANDLERS: Auto-quiz answers
# =====================================================================

@dp.message(StateFilter(default_state), F.text == "Пропустить")
async def skip_auto_quiz(message: Message):
    await db.execute("DELETE FROM active_quizzes WHERE user_id = ?", (message.from_user.id,))
    await db.commit()
    await message.answer("Пропущено.", reply_markup=MAIN_KB)

@dp.message(F.voice)
async def handle_voice_in_auto_quiz(message: Message):
    cursor = await db.execute(
        "SELECT * FROM active_quizzes WHERE user_id = ? ORDER BY id LIMIT 1",
        (message.from_user.id,)
    )
    active_quiz = await cursor.fetchone()
    if not active_quiz:
        return

    try:
        voice_file = await bot.get_file(message.voice.file_id)
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".ogg")
        await bot.download_file(voice_file.file_path, temp_file.name)
        temp_file.close()
        text = await transcribe_voice(temp_file.name)
        os.unlink(temp_file.name)
        if not text:
            await message.answer("Не удалось распознать.")
            return
        await message.answer(f"Распознано: *{text}*")
        await process_auto_quiz_answer(message, text, active_quiz)
    except Exception as e:
        print(f"Voice error: {e}")
        await message.answer("Ошибка голоса.")

async def process_auto_quiz_answer(message: Message, answer: str, quiz_data):
    user_id = message.from_user.id
    expected = quiz_data['expected_answer']
    question = quiz_data['question']
    direction = quiz_data['direction']
    word_id = quiz_data['word_id']
    attempts = quiz_data['attempts']

    is_correct, feedback = await judge_semantic(direction, question, expected, answer)

    if is_correct:
        await db.execute(
            "INSERT INTO quiz_stats (user_id, word_id, is_correct, attempts_count) VALUES (?,?,?,?)",
            (user_id, word_id, True, attempts + 1)
        )
        await db.execute("DELETE FROM active_quizzes WHERE id = ?", (quiz_data['id'],))
        await db.commit()
        await update_streak_and_goal(user_id, True)
        await message.answer("Верно" + (f"  {feedback}" if feedback else ""))

        cursor = await db.execute(
            "SELECT * FROM active_quizzes WHERE user_id = ? ORDER BY id LIMIT 1", (user_id,)
        )
        next_quiz = await cursor.fetchone()
        if next_quiz:
            await message.answer(next_quiz['question'])
        else:
            cursor = await db.execute(
                """
                SELECT COUNT(*) as total, SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) as correct
                FROM quiz_stats WHERE user_id = ? AND answered_at > datetime('now', '-1 hour')
                """, (user_id,)
            )
            stats = await cursor.fetchall()
            total = stats[0]['total'] if stats else 0
            correct = stats[0]['correct'] if stats else 0
            await message.answer(f"Квиз завершен\n\nРезультат: {correct}/{total}", reply_markup=MAIN_KB)
    else:
        attempts += 1
        await db.execute(
            "UPDATE active_quizzes SET attempts = ? WHERE id = ?", (attempts, quiz_data['id'])
        )
        await db.commit()
        await update_streak_and_goal(user_id, False)

        if attempts >= 3:
            await db.execute(
                "INSERT INTO quiz_stats (user_id, word_id, is_correct, attempts_count) VALUES (?,?,?,?)",
                (user_id, word_id, False, attempts)
            )
            await db.execute("DELETE FROM active_quizzes WHERE id = ?", (quiz_data['id'],))
            await db.commit()
            await message.answer(f"Неверно — *{expected}*" + (f"\n{feedback}" if feedback else ""))

            cursor = await db.execute(
                "SELECT * FROM active_quizzes WHERE user_id = ? ORDER BY id LIMIT 1", (user_id,)
            )
            next_quiz = await cursor.fetchone()
            if next_quiz:
                await message.answer(next_quiz['question'])
            else:
                cursor = await db.execute(
                    """
                    SELECT COUNT(*) as total, SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) as correct
                    FROM quiz_stats WHERE user_id = ? AND answered_at > datetime('now', '-1 hour')
                    """, (user_id,)
                )
                stats = await cursor.fetchall()
                total = stats[0]['total'] if stats else 0
                correct = stats[0]['correct'] if stats else 0
                await message.answer(f"Квиз завершен\n\nРезультат: {correct}/{total}", reply_markup=MAIN_KB)
        else:
            hint = expected[:len(expected)//2] + "..."
            await message.answer(f"Неверно. Подсказка: {hint}  ({attempts}/3)")

# =====================================================================
#  HANDLERS: Settings (FSM-based)
# =====================================================================

@dp.message(StateFilter(default_state), F.text == BTN_SETTINGS)
async def show_settings(message: Message, state: FSMContext):
    await _show_settings_menu(message, state)

async def _show_settings_menu(message: Message, state: FSMContext):
    user_id = message.from_user.id
    await ensure_user_settings(user_id)
    cursor = await db.execute(
        "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
    )
    settings = await cursor.fetchone()

    status = "вкл" if settings['quiz_enabled'] else "выкл"
    times = settings['quiz_times']
    count = settings['quiz_count']
    daily_goal = settings['daily_goal'] or 5

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Уведомления вкл/выкл")],
            [KeyboardButton(text="Слов в квизе"), KeyboardButton(text="Цель дня")],
            [KeyboardButton(text=BTN_BACK)]
        ],
        resize_keyboard=True
    )

    await message.answer(
        f"Настройки\n\n"
        f"Автоквизы: {status}  ({times})\n"
        f"Слов в квизе: {count}  |  цель дня: {daily_goal}",
        reply_markup=kb
    )
    await state.set_state(SettingsMenu.main)

@dp.message(SettingsMenu.main, F.text == "Уведомления вкл/выкл")
async def toggle_notifications(message: Message, state: FSMContext):
    await ensure_user_settings(message.from_user.id)
    cursor = await db.execute(
        "SELECT quiz_enabled FROM user_settings WHERE user_id = ?", (message.from_user.id,)
    )
    current = await cursor.fetchone()
    new_status = not current['quiz_enabled'] if current else True
    await db.execute(
        "UPDATE user_settings SET quiz_enabled = ? WHERE user_id = ?",
        (new_status, message.from_user.id)
    )
    await db.commit()
    await message.answer(f"Автоквизы: {'вкл' if new_status else 'выкл'}")
    await _show_settings_menu(message, state)

@dp.message(SettingsMenu.main, F.text == "Слов в квизе")
async def change_quiz_count_start(message: Message, state: FSMContext):
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="3"), KeyboardButton(text="5"), KeyboardButton(text="10")],
            [KeyboardButton(text="15"), KeyboardButton(text="20")],
            [KeyboardButton(text=BTN_BACK)]
        ],
        resize_keyboard=True
    )
    await message.answer("Слов в квизе:", reply_markup=kb)
    await state.set_state(SettingsMenu.selecting_quiz_count)

@dp.message(SettingsMenu.selecting_quiz_count)
async def change_quiz_count_process(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if _is_back_text(text):
        await _show_settings_menu(message, state)
        return
    if text not in {"3", "5", "10", "15", "20"}:
        await message.answer("Выбери: 3, 5, 10, 15 или 20.")
        return
    count = int(text)
    await db.execute(
        "UPDATE user_settings SET quiz_count = ? WHERE user_id = ?",
        (count, message.from_user.id)
    )
    await db.commit()
    await message.answer(f"Установлено: {count}")
    await _show_settings_menu(message, state)

@dp.message(SettingsMenu.main, F.text == "Цель дня")
async def change_daily_goal_start(message: Message, state: FSMContext):
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="3"), KeyboardButton(text="5"), KeyboardButton(text="10")],
            [KeyboardButton(text="15"), KeyboardButton(text="20")],
            [KeyboardButton(text=BTN_BACK)]
        ],
        resize_keyboard=True
    )
    await message.answer("Ответов в день для цели:", reply_markup=kb)
    await state.set_state(SettingsMenu.selecting_daily_goal)

@dp.message(SettingsMenu.selecting_daily_goal)
async def change_daily_goal_process(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if _is_back_text(text):
        await _show_settings_menu(message, state)
        return
    if text not in {"3", "5", "10", "15", "20"}:
        await message.answer("Выбери: 3, 5, 10, 15 или 20.")
        return
    goal = int(text)
    await db.execute(
        "UPDATE user_settings SET daily_goal = ? WHERE user_id = ?",
        (goal, message.from_user.id)
    )
    await db.commit()
    await message.answer(f"Цель дня: {goal}")
    await _show_settings_menu(message, state)

@dp.message(SettingsMenu.main)
async def settings_back(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if _is_back_text(text):
        await state.clear()
        await message.answer("Главное меню.", reply_markup=MAIN_KB)
    else:
        await message.answer("Выбери действие из меню.")

# Back button from default state
@dp.message(StateFilter(default_state), F.text.in_([BTN_BACK, "Назад", "◀️ Назад"]))
async def back_to_main(message: Message):
    await message.answer("Главное меню.", reply_markup=MAIN_KB)

# =====================================================================
#  HANDLERS: Statistics (streak, rank, daily goal, weekly progress)
# =====================================================================

@dp.message(StateFilter(default_state), F.text == BTN_STATS)
async def show_statistics(message: Message):
    user_id = message.from_user.id
    await ensure_user_settings(user_id)

    cursor = await db.execute(
        "SELECT COUNT(*) FROM vocabulary WHERE user_id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    total_words = row[0] if row else 0

    cursor = await db.execute(
        "SELECT streak, total_correct, daily_goal, today_answers, last_practice_date FROM user_settings WHERE user_id = ?", (user_id,)
    )
    settings = await cursor.fetchone()
    streak = settings['streak'] or 0
    total_correct = settings['total_correct'] or 0
    daily_goal = settings['daily_goal'] or 5
    today_answers = settings['today_answers'] or 0
    last_date = settings['last_practice_date']

    # Reset today_answers display if not today
    if last_date != date.today().isoformat():
        today_answers = 0

    rank_name, next_threshold = get_rank(total_correct)

    cursor = await db.execute(
        "SELECT COUNT(*) FROM vocabulary WHERE user_id = ? AND sr_next_review <= datetime('now')", (user_id,)
    )
    row = await cursor.fetchone()
    due_count = row[0] if row else 0

    cursor = await db.execute(
        """
        SELECT COUNT(*) as total_attempts,
               SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) as correct_answers
        FROM quiz_stats
        WHERE user_id = ? AND answered_at > datetime('now', '-7 days')
        """, (user_id,)
    )
    quiz_stats = await cursor.fetchone()
    total_attempts = quiz_stats['total_attempts'] if quiz_stats else 0
    correct_7d = quiz_stats['correct_answers'] if quiz_stats else 0
    accuracy = (correct_7d / total_attempts * 100) if total_attempts > 0 else 0

    cursor = await db.execute(
        """
        SELECT
            strftime('%Y-%W', answered_at) as week,
            COUNT(*) as total,
            SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) as correct
        FROM quiz_stats
        WHERE user_id = ? AND answered_at > datetime('now', '-28 days')
        GROUP BY week
        ORDER BY week ASC
        """, (user_id,)
    )
    weekly_rows = await cursor.fetchall()

    weekly_lines = []
    for row in weekly_rows:
        w_total = row['total']
        w_correct = row['correct'] or 0
        w_pct = (w_correct / w_total * 100) if w_total > 0 else 0
        bar = progress_bar(w_pct)
        weekly_lines.append(f"{bar}  {w_pct:.0f}% ({w_correct}/{w_total})")

    # Daily goal progress bar
    goal_pct = min(100, today_answers / daily_goal * 100) if daily_goal > 0 else 0
    goal_bar = progress_bar(goal_pct)

    text = (
        f"Статистика\n\n"
        f"Слов: {total_words}  |  к повторению: {due_count}\n"
        f"Серия: {streak} д.  |  ранг: {rank_name} ({total_correct}/{next_threshold})\n\n"
        f"Сегодня  {goal_bar}  {today_answers}/{daily_goal}\n\n"
        f"7 дней: {correct_7d}/{total_attempts} ({accuracy:.0f}%)"
    )

    if weekly_lines:
        text += "\n\n" + "\n".join(weekly_lines)

    await message.answer(text, reply_markup=MAIN_KB)

# =====================================================================
#  HANDLERS: Main menu buttons from any state (reset & redirect)
# =====================================================================

MAIN_MENU_BUTTONS = {BTN_ADD, BTN_DICT, BTN_LEARN, BTN_TRAIN, BTN_SETTINGS, BTN_STATS}

@dp.message(F.text.in_(MAIN_MENU_BUTTONS))
async def menu_from_any_state(message: Message, state: FSMContext):
    """Catch main menu button presses when user is stuck in another state."""
    await state.clear()
    text = message.text

    if text == BTN_ADD:
        await enter_learning_mode(message, state)
    elif text == BTN_DICT:
        await show_dictionary(message)
    elif text == BTN_LEARN:
        await smart_learn_handler(message, state)
    elif text == BTN_TRAIN:
        await train_start(message, state)
    elif text == BTN_SETTINGS:
        await show_settings(message, state)
    elif text == BTN_STATS:
        await show_statistics(message)

# =====================================================================
#  HANDLERS: AI chat (default state fallback)
# =====================================================================

@dp.message(StateFilter(default_state))
async def chat_with_ai(message: Message):
    text = (message.text or "").strip()

    cursor = await db.execute(
        "SELECT * FROM active_quizzes WHERE user_id = ? ORDER BY id LIMIT 1",
        (message.from_user.id,)
    )
    active_quiz = await cursor.fetchone()
    if active_quiz and text:
        await process_auto_quiz_answer(message, text, active_quiz)
        return

    if not text:
        return

    intent = detect_intent(text)

    if intent == "TRANSLATE":
        phrase, direction = parse_translate_input(text)
        msgs = build_messages_translate(phrase, direction)
        raw = await generate_chat(msgs, max_tokens=260, ensure_json=True)

        data = extract_json(raw)
        if not data:
            trans = None
            m = re.search(r'(?i)(?:translation|перевод)\s*[:\-]\s*(.+)', strip_code_fences_only(raw))
            if m:
                trans = m.group(1).strip().splitlines()[0]
            examples = re.findall(r'EN:\s*.+?\s+—\s*RU:\s*.+?(?=\n|$)', raw)
            if trans or examples:
                out_lines = []
                if trans:
                    out_lines.append(f"*Перевод:* {trans}")
                if examples:
                    out_lines.append("*Примеры:*")
                    for ex in examples[:3]:
                        out_lines.append(f"  {ex}")
                await message.answer("\n".join(out_lines))
                return

        if data:
            translation = (data.get("translation") or "").strip()
            level = (data.get("level") or "").strip().upper()
            examples = data.get("examples") or []
            note = (data.get("note") or "").strip()

            out_lines = []
            if translation:
                out_lines.append(f"*Перевод:* {translation}")
            if level:
                out_lines.append(f"*Уровень:* {level}")
            if examples:
                out_lines.append("*Примеры:*")
                for ex in examples[:3]:
                    out_lines.append(f"  {ex}")
            if note:
                out_lines.append(f"*Заметка:* {note}")
            await message.answer("\n".join(out_lines) if out_lines else translation or "Нет данных")
        else:
            cleaned = strip_llm_artifacts(raw)
            await message.answer(cleaned or "Не удалось перевести.")

    elif intent == "EXPLAIN":
        msgs = build_messages_explain(text)
        raw = await generate_chat(msgs, max_tokens=220)
        await message.answer(strip_llm_artifacts(raw) or "Не удалось объяснить.")

    else:
        msgs = build_messages_qa(text)
        raw = await generate_chat(msgs, max_tokens=160)
        await message.answer(strip_llm_artifacts(raw) or "Не удалось ответить.")

# =====================================================================
#  Database init
# =====================================================================

async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA journal_mode = WAL")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS vocabulary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            english TEXT,
            translation TEXT,
            added_at TEXT DEFAULT (datetime('now')),
            sr_interval INTEGER DEFAULT 0,
            sr_ease REAL DEFAULT 2.5,
            sr_next_review TEXT DEFAULT (datetime('now'))
        );
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            quiz_enabled INTEGER DEFAULT 1,
            quiz_times TEXT DEFAULT '10:00,14:00,18:00',
            quiz_count INTEGER DEFAULT 5,
            created_at TEXT DEFAULT (datetime('now')),
            streak INTEGER DEFAULT 0,
            last_practice_date TEXT,
            total_correct INTEGER DEFAULT 0,
            daily_goal INTEGER DEFAULT 5,
            today_answers INTEGER DEFAULT 0
        );
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS quiz_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            word_id INTEGER REFERENCES vocabulary(id) ON DELETE CASCADE,
            is_correct INTEGER,
            attempts_count INTEGER DEFAULT 1,
            answered_at TEXT DEFAULT (datetime('now'))
        );
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS active_quizzes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            question TEXT,
            expected_answer TEXT,
            direction TEXT,
            word_id INTEGER REFERENCES vocabulary(id) ON DELETE CASCADE,
            attempts INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)

    # Safe migration for existing tables
    migrations = [
        "ALTER TABLE vocabulary ADD COLUMN sr_interval INTEGER DEFAULT 0",
        "ALTER TABLE vocabulary ADD COLUMN sr_ease REAL DEFAULT 2.5",
        "ALTER TABLE vocabulary ADD COLUMN sr_next_review TEXT DEFAULT (datetime('now'))",
        "ALTER TABLE user_settings ADD COLUMN streak INTEGER DEFAULT 0",
        "ALTER TABLE user_settings ADD COLUMN last_practice_date TEXT",
        "ALTER TABLE user_settings ADD COLUMN total_correct INTEGER DEFAULT 0",
        "ALTER TABLE user_settings ADD COLUMN daily_goal INTEGER DEFAULT 5",
        "ALTER TABLE user_settings ADD COLUMN today_answers INTEGER DEFAULT 0",
    ]
    for migration in migrations:
        try:
            await db.execute(migration)
        except Exception:
            pass  # Column already exists

    await db.commit()

# =====================================================================
#  Main
# =====================================================================

async def main():
    await init_db()
    print("DB initialized.")
    setup_scheduler()
    print("Bot starting...")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()
        if db:
            await db.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped")
