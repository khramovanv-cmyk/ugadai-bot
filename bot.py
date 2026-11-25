import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.storage.memory import MemoryStorage

# ================== НАСТРОЙКИ ==================
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

PRICE = int(os.getenv("PRICE", "10"))
RESERVE_MINUTES = int(os.getenv("RESERVE_MINUTES", "12"))
NUMBERS = list(range(1, 6))

SBP_PHONE = os.getenv("SBP_PHONE", "+79038784256")
SBP_NAME = os.getenv("SBP_NAME", "Наталья")
SBP_COMMENT = os.getenv("SBP_COMMENT", "подарок")

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

MSK = ZoneInfo("Europe/Moscow")
# ===============================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

lock = asyncio.Lock()
BOT_USERNAME = None


def now_msk():
    return datetime.now(MSK)


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "free": NUMBERS.copy(),
        "reserved": {},
        "sold": {},
        "seen_files": [],
        "group_id": None,
        "status_message_id": None,
    }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_user_name(user) -> str:
    return user.full_name or user.username or str(user.id)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def format_until(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    return dt.strftime("%H:%M")


def build_status_text(state) -> str:
    free = sorted(state["free"])
    free_str = ", ".join(map(str, free)) if free else "нет"
    return (
        "<b>Угадай число. Получи! Бабло!</b>\n"
        "Улучшили условия для вас.\n\n"
        f"Номера 1–5, цена {PRICE} ₽\n"
        f"Бронь {RESERVE_MINUTES} минут\n\n"
        f"<b>Свободные:</b> {free_str}\n\n"
        "Напиши номер в чат или боту в личку.\n"
        "Чек — только в личку."
    )


def payment_text(amount: int, numbers: list) -> str:
    nums = ", ".join(map(str, sorted(numbers)))
    return (
        f"Забронированы номера: <b>{nums}</b>\n"
        f"Сумма: <b>{amount} ₽</b>\n"
        f"Время на оплату: {RESERVE_MINUTES} минут\n\n"
        f"Реквизиты:\n"
        f"СБП: {SBP_PHONE}\n"
        f"Получатель: {SBP_NAME}\n"
        f"Подпись к переводу: {SBP_COMMENT}\n\n"
        "После оплаты пришли сюда фото чека или PDF."
    )


async def update_status_message(state):
    if not state.get("group_id") or not state.get("status_message_id"):
        return
    text = build_status_text(state)
    try:
        await bot.edit_message_text(
            text,
            chat_id=state["group_id"],
            message_id=state["status_message_id"],
            parse_mode="HTML",
        )
    except Exception as e:
        logging.warning(f"Не смог обновить сообщение: {e}")


async def clear_expired():
    while True:
        async with lock:
            state = load_state()
            now = now_msk()
            to_free = []
            for num, data in list(state["reserved"].items()):
                until = datetime.fromisoformat(data["until"])
                if until < now and not data.get("check_received"):
                    to_free.append(num)
            if to_free:
                for num in to_free:
                    state["free"].append(int(num))
                    del state["reserved"][num]
                state["free"] = sorted(set(state["free"]))
                save_state(state)
                await update_status_message(state)
                logging.info(f"Сняты брони: {to_free}")
        await asyncio.sleep(30)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    args = message.text.split(maxsplit=1)
    payload = args[1] if len(args) > 1 else None

    if payload == "pay":
        async with lock:
            state = load_state()
            user_reserves = [
                int(n) for n, d in state["reserved"].items()
                if d["user_id"] == message.from_user.id
            ]
        if not user_reserves:
            await message.answer("У тебя сейчас нет активной брони.")
            return
        amount = len(user_reserves) * PRICE
        await message.answer(payment_text(amount, user_reserves), parse_mode="HTML")
        return

    await message.answer(
        "Привет! Это бот конкурса «Угадай число. Получи! Бабло!»\n\n"
        f"Номера 1–5, цена {PRICE} ₽\n"
        f"Бронь {RESERVE_MINUTES} минут\n\n"
        "Напиши номер, например:\n"
        "<code>3</code>\n"
        "<code>1 4</code>\n\n"
        "Чек присылай сюда, в личку.",
        parse_mode="HTML",
    )


@dp.message(Command("status"))
async def cmd_status(message: Message):
    async with lock:
        state = load_state()
    free = sorted(state["free"])
    res = sorted(int(n) for n in state["reserved"])
    sold = sorted(int(n) for n in state["sold"])
    await message.answer(
        f"<b>Свободные:</b> {', '.join(map(str, free)) or 'нет'}\n"
        f"<b>Забронированы:</b> {', '.join(map(str, res)) or 'нет'}\n"
        f"<b>Чек принят:</b> {', '.join(map(str, sold)) or 'нет'}",
        parse_mode="HTML",
    )


@dp.message(Command("list"))
async def cmd_list(message: Message):
    if not is_admin(message.from_user.id):
        return
    async with lock:
        state = load_state()
    lines = ["<b>Полный список</b>\n"]
    for num in sorted(int(n) for n in state["sold"]):
        d = state["sold"][str(num)]
        lines.append(f"{num}: {d['username']} (чек принят)")
    for num in sorted(int(n) for n in state["reserved"]):
        d = state["reserved"][str(num)]
        lines.append(f"{num}: {d['username']} до {format_until(d['until'])}")
    if len(lines) == 1:
        lines.append("Пока пусто.")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"chat_id: <code>{message.chat.id}</code>", parse_mode="HTML")


@dp.message(Command("setgroup"))
async def cmd_setgroup(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только для админа.")
        return
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Эту команду нужно писать в группе.")
        return

    async with lock:
        state = load_state()
        state["group_id"] = message.chat.id
        save_state(state)

    text = build_status_text(state)
    sent = await message.answer(text, parse_mode="HTML")

    async with lock:
        state = load_state()
        state["status_message_id"] = sent.message_id
        save_state(state)

    await message.answer("Группа привязана. Это сообщение со свободными номерами я буду обновлять.")


def parse_numbers(text: str):
    clean = text.lower().replace("беру", " ").replace(",", " ")
    parts = [p for p in clean.split() if p.isdigit()]
    return [int(p) for p in parts]


@dp.message(F.photo | F.document)
async def handle_check(message: Message):
    if message.chat.type != "private":
        await message.answer("Чек присылай в личку боту.")
        return

    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_unique_id
    elif message.document:
        file_id = message.document.file_unique_id

    if not file_id:
        return

    async with lock:
        state = load_state()
        if file_id in state["seen_files"]:
            await message.answer("Этот файл уже использован. Пришли другой чек.")
            return

        user_reserves = [
            int(n) for n, d in state["reserved"].items()
            if d["user_id"] == message.from_user.id
        ]
        if not user_reserves:
            await message.answer("У тебя сейчас нет активной брони.")
            return

        for num in user_reserves:
            data = state["reserved"].pop(str(num))
            state["sold"][str(num)] = {
                "user_id": message.from_user.id,
                "username": data["username"],
                "paid_at": now_msk().isoformat(),
                "file_id": file_id,
            }
        state["seen_files"].append(file_id)
        save_state(state)
        await update_status_message(state)

    nums = ", ".join(map(str, sorted(user_reserves)))
    await message.answer(
        f"Чек получен.\nНомера <b>{nums}</b> закреплены за тобой.\nСпасибо!",
        parse_mode="HTML",
    )


@dp.message(F.text)
async def handle_numbers(message: Message):
    if message.text.startswith("/"):
        return

    numbers = parse_numbers(message.text)
    if not numbers:
        return

    numbers = list(dict.fromkeys(numbers))
    invalid = [n for n in numbers if n not in NUMBERS]
    if invalid:
        await message.answer(f"Номера {invalid} не существуют. Доступны только 1–5.")
        return

    async with lock:
        state = load_state()
        available = []
        taken = []
        for n in numbers:
            if n in state["free"]:
                available.append(n)
            else:
                taken.append(n)

        if not available:
            free = sorted(state["free"])
            free_str = ", ".join(map(str, free)) if free else "нет"
            await message.answer(f"Эти номера заняты.\nСвободные: {free_str}")
            return

        user_id = message.from_user.id
        username = get_user_name(message.from_user)
        until = (now_msk() + timedelta(minutes=RESERVE_MINUTES)).isoformat()

        for n in available:
            state["free"].remove(n)
            state["reserved"][str(n)] = {
                "user_id": user_id,
                "username": username,
                "until": until,
                "check_received": False,
            }
        save_state(state)
        await update_status_message(state)

    amount = len(available) * PRICE
    nums_str = ", ".join(map(str, sorted(available)))

    if message.chat.type == "private":
        await message.answer(payment_text(amount, available), parse_mode="HTML")
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Перейти к оплате",
                url=f"https://t.me/{BOT_USERNAME}?start=pay",
            )
        ]])
        await message.answer(
            f"Забронированы номера: <b>{nums_str}</b>\n"
            f"На {RESERVE_MINUTES} минут.\n"
            "Нажми кнопку и пришли чек в личку.",
            reply_markup=kb,
            parse_mode="HTML",
        )

    if taken:
        await message.answer(f"Номера {taken} уже заняты, их не забронировал.")


async def main():
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username
    logging.info(f"Бот @{BOT_USERNAME} запущен")
    asyncio.create_task(clear_expired())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
