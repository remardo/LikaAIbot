import asyncio
import logging
import os
import re
import socket
from datetime import date, datetime

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lika_bot")

# Some hosting networks filter Telegram IP ranges. Pin known-good IPs first,
# system DNS stays as fallback.
_TG_IPS = ["149.154.167.220"]
_orig_getaddrinfo = socket.getaddrinfo


def _tg_getaddrinfo(host, port, *args, **kwargs):
    if host == "api.telegram.org":
        fixed = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port)) for ip in _TG_IPS]
        try:
            rest = [r for r in _orig_getaddrinfo(host, port, *args, **kwargs) if r[4][0] not in _TG_IPS]
            return fixed + rest
        except socket.gaierror:
            return fixed
    return _orig_getaddrinfo(host, port, *args, **kwargs)


socket.getaddrinfo = _tg_getaddrinfo

MONTHS_RU = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]

BLOCKS = {
    "1": "Психопортрет: MBTI, Enneagram, Big Five",
    "2": "Решения и ловушки выбора",
    "3": "Отношения: сильные стороны и зоны роста",
    "4": "Персональный план роста",
    "5": "Эмоциональные триггеры и самопомощь",
    "6": "Идеальный день: фокус и восстановление",
}

ARCHETYPES = {
    1: ("Стартер", "быстрый старт и ответственность", "перегруз контролем"),
    2: ("Дипломат", "чувство людей и такт", "застревание в сомнениях"),
    3: ("Коммуникатор", "энергия и идеи", "расфокус"),
    4: ("Системный", "порядок и доведение до результата", "жесткость к себе"),
    5: ("Исследователь", "гибкость и скорость", "импульсивность"),
    6: ("Наставник", "забота и надежность", "гиперответственность"),
    7: ("Аналитик", "глубина и независимость", "уход в переанализ"),
    8: ("Практик", "цель и результат", "работа на истощение"),
    9: ("Гуманист", "смысл и эмпатия", "переоценка своих сил"),
    11: ("Вдохновляющий", "интуиция и влияние", "нервное перенапряжение"),
    22: ("Строитель", "масштаб и стратегия", "перфекционизм"),
    33: ("Целитель", "поддержка и тепло", "забывание про себя"),
}

user_dob: dict[int, date] = {}

kb_menu = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=f"{k}. {v}")] for k, v in BLOCKS.items()],
    resize_keyboard=True,
)

CONSENT = (
    "Продолжая диалог с ассистентом, вы соглашаетесь на обработку "
    "персональных данных исключительно для персонального разбора, "
    "помощи в использовании бота и обратной связи.\n\n"
    "Напишите дату рождения в формате ДД.ММ.ГГГГ, и я помогу выбрать "
    "тот разбор, с которого лучше начать именно вам!"
)

MENU_TEXT = (
    "Выберите интересующий блок разбора:\n\n"
    + "\n".join(f"{k}. {v}" for k, v in BLOCKS.items())
    + "\n\nНапишите номер или название. Выводы ознакомительные и не заменяют психотерапию."
)


def parse_dob(s: str) -> date | None:
    m = re.search(r"(\d{1,2})[.\-/\s](\d{1,2})[.\-/\s](\d{4})", s.strip())
    if not m:
        return None
    try:
        d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if d > date.today() or d.year < 1900:
            return None
        return d
    except ValueError:
        return None


def fmt_dob(d: date) -> str:
    return f"{d.day} {MONTHS_RU[d.month]} {d.year} года"


def reduce_num(n: int) -> int:
    while n > 9 and n not in (11, 22, 33):
        n = sum(int(c) for c in str(n))
    return n


def calc_profile(d: date) -> dict:
    digits = f"{d.day:02d}{d.month:02d}{d.year:04d}"
    life = reduce_num(sum(int(c) for c in digits))
    name, strength, trap = ARCHETYPES.get(life, ARCHETYPES[1])
    seasons = {12: "зима", 1: "зима", 2: "зима", 3: "весна", 4: "весна",
               5: "весна", 6: "лето", 7: "лето", 8: "лето",
               9: "осень", 10: "осень", 11: "осень"}
    return {
        "dob": fmt_dob(d),
        "day": d.day, "month": d.month, "year": d.year,
        "weekday": d.weekday(),
        "season": seasons[d.month],
        "life": life, "arch": name,
        "strength": strength, "trap": trap,
        "day_root": reduce_num(d.day),
        "month_root": reduce_num(d.month),
        "year_root": reduce_num(sum(int(c) for c in str(d.year))),
    }


def fallback_text(block: str, p: dict) -> str:
    head = f"{BLOCKS[block]}\n\nДата: {p['dob']}. Число пути: {p['life']} ({p['arch']}).\nОтмечу: дата рождения не определяет личность, ниже — ознакомительные гипотезы для саморефлексии.\n"
    body = {
        "1": f"Возможная опора — {p['strength']}. В MBTI-терминах чаще выглядите как практик-аналитик, в Big Five — выше добросовестность. Зона внимания — {p['trap']}.",
        "2": f"Вы быстро схватываете суть (день {p['day']}, месяц {p['month']}). Ловушка — {p['trap']} и решение на усталости. Правило: важное — после паузы и 3 критерия на бумаге.",
        "3": f"В отношениях даете {p['strength']}. Партнеру важно признание вашего вклада. Зона роста — просить поддержку словами, а не ждать догадок.",
        "4": f"Сильная сторона — {p['strength']}. План на 2 недели: 1 приоритет в день, 2 вечера без обязательств, трекер энергии 1-10, разбор недели. Установка на замену: «отдых — условие результата, а не награда». Слепая зона — {p['trap']}.",
        "5": f"Триггер — потеря контроля и спешка (сезон рождения {p['season']}). Протокол: стоп-пауза 2 мин, дыхание 4-6, назвать чувство, один маленький шаг.",
        "6": f"Идеальный день: утро — 1 главная задача, день — блоки по 50/10, вечер — прогулка без телефона. Восстановление — {p['season']} прогулки и тишина.",
    }[block]
    return head + "\n" + body + "\n\nВсе рекомендации ознакомительные и не заменяют психотерапию.\nЕсли было полезно — напишите отзыв ответным сообщением."


async def deepseek_text(block: str, p: dict) -> str:
    if not DEEPSEEK_KEY:
        return fallback_text(block, p)
    system = (
        "Ты — бережный ассистент по саморефлексии. Пиши по-русски, "
        "гендерно-нейтрально (только «вы»), без эзотерики и диагнозов. "
        "Дата рождения — лишь повод для разговора, прямо скажи что это не диагностика. "
        "Структура: короткий дисклеймер, сильные стороны, 3-5 практичных пунктов, фокус недели. "
        "До 1800 символов."
    )
    user = (
        f"Дата рождения: {p['dob']}. Число пути {p['life']} ({p['arch']}, опора {p['strength']}, "
        f"ловушка {p['trap']}), день {p['day_root']}, месяц {p['month_root']}, год {p['year_root']}, "
        f"сезон {p['season']}. Блок: {BLOCKS[block]}. Дай персонализированный разбор, "
        f"привяжи 2-3 детали к числам выше."
    )
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
            async with s.post(DEEPSEEK_URL, headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
                              json={"model": DEEPSEEK_MODEL, "messages": [
                                  {"role": "system", "content": system},
                                  {"role": "user", "content": user}],
                                    "temperature": 0.7, "max_tokens": 1200}) as r:
                if r.status != 200:
                    log.warning("deepseek status %s", r.status)
                    return fallback_text(block, p)
                j = await r.json()
                return j["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.warning("deepseek fail: %s", e)
        return fallback_text(block, p)


def match_block(text: str) -> str | None:
    t = text.strip().lower()
    if t and t[0] in "123456" and len(t) < 4:
        return t[0]
    for k, v in BLOCKS.items():
        if v.lower() in t or t in v.lower():
            return k
    return None


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN в .env")
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(m: Message):
        await m.answer(CONSENT)

    @dp.message(F.text)
    async def router(m: Message):
        uid = m.from_user.id
        b = match_block(m.text or "")
        if b and uid in user_dob:
            p = calc_profile(user_dob[uid])
            await m.answer("Готовлю разбор…")
            await m.answer(await deepseek_text(b, p))
            return
        d = parse_dob(m.text or "")
        if d:
            user_dob[uid] = d
            await m.answer(f"Спасибо, дата рождения сохранена: {fmt_dob(d)}.\n\n{MENU_TEXT}",
                           reply_markup=kb_menu)
        else:
            await m.answer("Не понял дату. Напишите в формате ДД.ММ.ГГГГ, например 07.04.1981.")

    attempt = 0
    while True:
        try:
            await dp.start_polling(bot)
            return
        except TelegramNetworkError:
            attempt += 1
            log.warning("telegram unreachable, retry %s", attempt)
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
