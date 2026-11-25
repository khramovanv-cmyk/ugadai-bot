import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Set

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.storage.memory import MemoryStorage

# ================== НАСТРОЙКИ ==================


TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

PRICE = 10
RESERVE_MINUTES = 12
NUMBERS = list(range(1, 6))

PAYMENT_TEXT = """Реквизиты для оплаты:

СБП: +79038784256
Получатель: Наталья
Подпись к переводу: подарок

Сумма: {amount} ₽

После оплаты просто пришли сюда фото чека или PDF."""
# ===============================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Хранилище в памяти (для теста хватит)
free_numbers: Set[int] = set(NUMBERS)
reserved: Dict[int, dict] = {}  # number -> {user_id, until, username}
sold: Dict[int, dict] = {}      # number -> {user_id, username, paid_at}

def get_user_name(user: types.User) -> str:
    return user.full_name or user.username or str(user.id)

async def clear_expired_reserves():
    """Фоновая задача — снимает просроченные брони"""
    while True:
        now = datetime.now()
        to_remove = []
        for num, data in reserved.items():
            if data["until"] < now:
                to_remove.append(num)
        
        for num in to_remove:
            free_numbers.add(num)
            del reserved[num]
            logging.info(f"Снята бронь с номера {num}")
        
        await asyncio.sleep(30)

@dp.message(Command("start"))
async def cmd_start(message: Message):
    text = (
        "Привет! Это тестовый бот конкурса «Угадай число. Получи! Бабло!»\n\n"
        f"Доступны номера: 1–5\n"
        f"Цена одного номера: {PRICE} ₽\n\n"
        "Просто напиши номер (или несколько через пробел), например:\n"
        "<code>3</code>\n"
        "<code>1 4</code>\n\n"
        "Я забронирую их на 12 минут и пришлю реквизиты."
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("status"))
async def cmd_status(message: Message):
    free = sorted(free_numbers)
    res = sorted(reserved.keys())
    sold_list = sorted(sold.keys())
    
    text = (
        f"<b>Свободные:</b> {', '.join(map(str, free)) or 'нет'}\n"
        f"<b>Забронированы:</b> {', '.join(map(str, res)) or 'нет'}\n"
        f"<b>Проданы:</b> {', '.join(map(str, sold_list)) or 'нет'}"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(F.photo | F.document)
async def handle_check(message: Message):
    user_id = message.from_user.id
    
    # Ищем, есть ли у этого пользователя активные брони
    user_reserves = [num for num, data in reserved.items() if data["user_id"] == user_id]
    
    if not user_reserves:
        await message.answer("У тебя сейчас нет забронированных номеров.")
        return
    
    # Засчитываем все его брони как оплаченные
    for num in user_reserves:
        data = reserved.pop(num)
        sold[num] = {
            "user_id": user_id,
            "username": data["username"],
            "paid_at": datetime.now()
        }
    
    nums_str = ", ".join(map(str, sorted(user_reserves)))
    await message.answer(
        f"Чек получен!\n"
        f"Номера <b>{nums_str}</b> успешно закреплены за тобой.\n\n"
        f"Спасибо за участие!"
    , parse_mode="HTML")

@dp.message(F.text)
async def handle_numbers(message: Message):
    text = message.text.strip()
    
    # Пытаемся вытащить номера
    try:
        numbers = [int(x) for x in text.replace(",", " ").split() if x.strip().isdigit()]
    except:
        await message.answer("Напиши просто номера, например: 2 или 1 3 5")
        return
    
    if not numbers:
        await message.answer("Не вижу номеров. Напиши, например: <code>4</code>", parse_mode="HTML")
        return
    
    # Убираем дубли
    numbers = list(dict.fromkeys(numbers))
    
    # Проверяем диапазон
    invalid = [n for n in numbers if n not in NUMBERS]
    if invalid:
        await message.answer(f"Номера {invalid} не существуют. Доступны только 1–5.")
        return
    
    # Проверяем доступность
    already_taken = []
    available = []
    
    for n in numbers:
        if n in free_numbers:
            available.append(n)
        else:
            already_taken.append(n)
    
    if not available:
        await message.answer("Эти номера уже заняты или забронированы.")
        return
    
    # Бронируем
    user_id = message.from_user.id
    username = get_user_name(message.from_user)
    until = datetime.now() + timedelta(minutes=RESERVE_MINUTES)
    
    for n in available:
        free_numbers.discard(n)
        reserved[n] = {
            "user_id": user_id,
            "username": username,
            "until": until
        }
    
    amount = len(available) * PRICE
    nums_str = ", ".join(map(str, sorted(available)))
    
    await message.answer(
        f"Забронировал номера: <b>{nums_str}</b>\n"
        f"Время на оплату: {RESERVE_MINUTES} минут\n\n"
        + PAYMENT_TEXT.format(amount=amount),
        parse_mode="HTML"
    )
    
    if already_taken:
        await message.answer(f"Номера {already_taken} уже были заняты, их не забронировал.")

async def main():
    asyncio.create_task(clear_expired_reserves())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())