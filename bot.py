import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

MSK = ZoneInfo("Europe/Moscow")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

lock = asyncio.Lock()
BOT_USERNAME = None


class NewContest(StatesGroup):
    title = State()
    subtitle = State()
    range_from = State()
    range_to = State()
    price = State()
    minutes = State()
    sbp_phone = State()
    sbp_name = State()
    sbp_comment = State()
    prizes = State()
    confirm = State()


def now_msk():
    return datetime.now(MSK)


def default_contest():
    return {
        "title": "Угадай число. Получи! Бабло!",
        "subtitle": "Улучшили условия для вас.",
        "from_n": 1,
        "to_n": 5,
        "price": 10,
        "minutes": 12,
        "sbp_phone": "+79038784256",
        "sbp_name": "Наталья",
        "sbp_comment": "подарок",
        "prizes": "",
    }


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    c = default_contest()
    return {
        "contest": c,
        "free": list(range(c["from_n"], c["to_n"] + 1)),
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


def numbers_range(contest):
    return list(range(contest["from_n"], contest["to_n"] + 1))


def build_status_text(state) -> str:
    c = state["contest"]
    free = sorted(state["free"])
    free_str = ", ".join(map(str, free)) if free else "нет"
    prizes = f"\n\n{c['prizes']}" if c.get("prizes") else ""
    return (
        f"<b>{c['title']}</b>\n"
        f"{c['subtitle']}\n\n"
        f"Номера {c['from_n']}–{c['to_n']}, цена {c['price']} ₽\n"
        f"Бронь {c['minutes']} минут\n\n"
        f"<b>Свободные:</b> {free_str}"
        f"{prizes}\n\n"
        "Напиши номер в чат или боту в личку.\n"
        "Чек — только в личку."
    )


def payment_text(state, amount: int, numbers: list) -> str:
    c = state["contest"]
    nums = ", ".join(map(str, sorted(numbers)))
    return (
        f"Забронированы номера: <b>{nums}</b>\n"
        f"Сумма: <b>{amount} ₽</b>\n"
        f"Время на оплату: {c['minutes']} минут\n\n"
        f"Реквизиты:\n"
        f"СБП: {c['sbp_phone']}\n"
        f"Получатель: {c['sbp_name']}\n"
        f"Подпись к переводу: {c['sbp_comment']}\n\n"
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
        async with lock:
            state = load_state()
        amount = len(user_reserves) * state["contest"]["price"]
        await message.answer(payment_text(state, amount, user_reserves), parse_mode="HTML")
        return

    async with lock:
        state = load_state()
    c = state["contest"]
    await message.answer(
        f"Привет! Это бот конкурса «{c['title']}»\n\n"
        f"Номера {c['from_n']}–{c['to_n']}, цена {c['price']} ₽\n"
        f"Бронь {c['minutes']} минут\n\n"
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
        lines.append(f"{num}: {d['username']} до {datetime.fromisoformat(d['until']).strftime('%H:%M')}")
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


@dp.message(Command("newcontest"))
async def cmd_newcontest(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.chat.type != "private":
        await message.answer("Эту команду пиши мне в личку.")
        return
    await state.set_state(NewContest.title)
    await message.answer("Название конкурса?\n(например: Угадай число. Получи! Бабло!)")


@dp.message(NewContest.title)
async def step_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(NewContest.subtitle)
    await message.answer("Подзаголовок?\n(например: Улучшили условия для вас.)")


@dp.message(NewContest.subtitle)
async def step_subtitle(message: Message, state: FSMContext):
    await state.update_data(subtitle=message.text.strip())
    await state.set_state(NewContest.range_from)
    await message.answer("Номера от какого числа? (только число)")


@dp.message(NewContest.range_from)
async def step_from(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("Напиши число.")
        return
    await state.update_data(from_n=int(message.text.strip()))
    await state.set_state(NewContest.range_to)
    await message.answer("До какого числа?")


@dp.message(NewContest.range_to)
async def step_to(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("Напиши число.")
        return
    await state.update_data(to_n=int(message.text.strip()))
    await state.set_state(NewContest.price)
    await message.answer("Цена одного номера в рублях?")


@dp.message(NewContest.price)
async def step_price(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("Напиши число.")
        return
    await state.update_data(price=int(message.text.strip()))
    await state.set_state(NewContest.minutes)
    await message.answer("Сколько минут на оплату?")


@dp.message(NewContest.minutes)
async def step_minutes(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("Напиши число.")
        return
    await state.update_data(minutes=int(message.text.strip()))
    await state.set_state(NewContest.sbp_phone)
    await message.answer("Номер телефона для СБП?")


@dp.message(NewContest.sbp_phone)
async def step_phone(message: Message, state: FSMContext):
    await state.update_data(sbp_phone=message.text.strip())
    await state.set_state(NewContest.sbp_name)
    await message.answer("Имя получателя?")


@dp.message(NewContest.sbp_name)
async def step_name(message: Message, state: FSMContext):
    await state.update_data(sbp_name=message.text.strip())
    await state.set_state(NewContest.sbp_comment)
    await message.answer("Подпись к переводу?")


@dp.message(NewContest.sbp_comment)
async def step_comment(message: Message, state: FSMContext):
    await state.update_data(sbp_comment=message.text.strip())
    await state.set_state(NewContest.prizes)
    await message.answer("Текст про призы (можно несколько строк). Или напиши «нет», если без призов пока.")


@dp.message(NewContest.prizes)
async def step_prizes(message: Message, state: FSMContext):
    prizes = "" if message.text.strip().lower() == "нет" else message.text.strip()
    await state.update_data(prizes=prizes)
    data = await state.get_data()
    await state.set_state(NewContest.confirm)
    text = (
        f"<b>Проверь:</b>\n"
        f"{data['title']}\n"
        f"{data['subtitle']}\n\n"
        f"Номера: {data['from_n']}–{data['to_n']}\n"
        f"Цена: {data['price']} ₽\n"
        f"Бронь: {data['minutes']} мин\n"
        f"СБП: {data['sbp_phone']}, {data['sbp_name']}, «{data['sbp_comment']}»\n"
        f"Призы: {data['prizes'] or 'нет'}\n\n"
        "Напиши <b>да</b> чтобы запустить, или <b>нет</b> чтобы отменить."
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(NewContest.confirm)
async def step_confirm(message: Message, state: FSMContext):
    if message.text.strip().lower() != "да":
        await state.clear()
        await message.answer("Отменено.")
        return

    data = await state.get_data()
    await state.clear()

    async with lock:
        st = load_state()
        st["contest"] = {
            "title": data["title"],
            "subtitle": data["subtitle"],
            "from_n": data["from_n"],
            "to_n": data["to_n"],
            "price": data["price"],
            "minutes": data["minutes"],
            "sbp_phone": data["sbp_phone"],
            "sbp_name": data["sbp_name"],
            "sbp_comment": data["sbp_comment"],
            "prizes": data["prizes"],
        }
        st["free"] = list(range(data["from_n"], data["to_n"] + 1))
        st["reserved"] = {}
        st["sold"] = {}
        st["seen_files"] = []
        save_state(st)

        if st.get("group_id"):
            text = build_status_text(st)
            try:
                sent = await bot.send_message(st["group_id"], text, parse_mode="HTML")
                st["status_message_id"] = sent.message_id
                save_state(st)
            except Exception as e:
                logging.warning(f"Не смог отправить в группу: {e}")

    await message.answer("Конкурс запущен. Старые брони сброшены.")


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

    async with lock:
        state = load_state()
        valid = numbers_range(state["contest"])
        invalid = [n for n in numbers if n not in valid]
        if invalid:
            await message.answer(f"Номера {invalid} не существуют. Доступны {state['contest']['from_n']}–{state['contest']['to_n']}.")
            return

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
        minutes = state["contest"]["minutes"]
        until = (now_msk() + timedelta(minutes=minutes)).isoformat()

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

    amount = len(available) * state["contest"]["price"]
    nums_str = ", ".join(map(str, sorted(available)))

    if message.chat.type == "private":
        await message.answer(payment_text(state, amount, available), parse_mode="HTML")
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Перейти к оплате",
                url=f"https://t.me/{BOT_USERNAME}?start=pay",
            )
        ]])
        await message.answer(
            f"Забронированы номера: <b>{nums_str}</b>\n"
            f"На {state['contest']['minutes']} минут.\n"
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
