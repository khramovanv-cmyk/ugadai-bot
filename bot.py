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
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

MSK = ZoneInfo("Europe/Moscow")

def now_msk():
    return datetime.now(MSK)

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

lock = asyncio.Lock()
BOT_USERNAME = None
user_variant = {}  # user_id -> variant index


class NewContest(StatesGroup):
    title = State()
    subtitle = State()
    sbp_phone = State()
    sbp_name = State()
    sbp_comment = State()
    var_name = State()
    var_from = State()
    var_to = State()
    var_price = State()
    var_minutes = State()
    var_prizes = State()
    more = State()
    confirm = State()


ADMIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Новый конкурс")],
        [KeyboardButton(text="Статус"), KeyboardButton(text="Список")],
    ],
    resize_keyboard=True,
)


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "title": "Угадай число. Получи! Бабло!",
        "subtitle": "Улучшили условия для вас.",
        "sbp_phone": "+79038784256",
        "sbp_name": "Наталья",
        "sbp_comment": "подарок",
        "variants": [],
        "seen_files": [],
        "group_id": None,
        "status_message_id": None,
        "status_is_photo": False,
    }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_user_name(user) -> str:
    return user.full_name or user.username or str(user.id)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def build_status_text(state) -> str:
    lines = [f"<b>{state['title']}</b>", state["subtitle"], ""]
    for i, v in enumerate(state["variants"], 1):
        free = sorted(v["free"])
        free_str = ", ".join(map(str, free)) if free else "нет"
        prizes = f" | {v['prizes']}" if v.get("prizes") else ""
        lines.append(
            f"<b>{i}. {v['name']}</b>\n"
            f"Номера {v['from_n']}–{v['to_n']}, {v['price']} ₽, бронь {v['minutes']} мин{prizes}\n"
            f"Свободные: {free_str}\n"
        )
    lines.append("👇 Нажми кнопку ниже, потом напиши номер.\nЧек — только в личку.")
    return "\n".join(lines)


def variant_keyboard(state):
    buttons = []
    for i, v in enumerate(state["variants"]):
        buttons.append([InlineKeyboardButton(
            text=f"🎯 {v['name']} — {v['price']} ₽",
            callback_data=f"var_{i}",
        )])
    if not buttons:
        buttons.append([InlineKeyboardButton(text="🎯 Выбрать номер", callback_data="var_0")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def payment_text(state, variant, amount, numbers):
    nums = ", ".join(map(str, sorted(numbers)))
    return (
        f"Вариант: <b>{variant['name']}</b>\n"
        f"Номера: <b>{nums}</b>\n"
        f"Сумма: <b>{amount} ₽</b>\n"
        f"Время: {variant['minutes']} минут\n\n"
        f"СБП: {state['sbp_phone']}\n"
        f"Получатель: {state['sbp_name']}\n"
        f"Подпись: {state['sbp_comment']}\n\n"
        "Пришли сюда фото чека или PDF."
    )


async def update_status_message(state):
    if not state.get("group_id") or not state.get("status_message_id"):
        return
    text = build_status_text(state)
    kb = variant_keyboard(state)
    try:
        if state.get("status_is_photo"):
            await bot.edit_message_caption(
                caption=text,
                chat_id=state["group_id"],
                message_id=state["status_message_id"],
                parse_mode="HTML",
                reply_markup=kb,
            )
        else:
            await bot.edit_message_text(
                text,
                chat_id=state["group_id"],
                message_id=state["status_message_id"],
                parse_mode="HTML",
                reply_markup=kb,
            )
    except Exception as e:
        logging.warning(f"Не смог обновить: {e}")


async def clear_expired():
    while True:
        async with lock:
            state = load_state()
            now = now_msk()
            changed = False
            for v in state["variants"]:
                to_free = []
                for num, data in list(v["reserved"].items()):
                    until = datetime.fromisoformat(data["until"])
                    if until < now and not data.get("check_received"):
                        to_free.append(num)
                for num in to_free:
                    v["free"].append(int(num))
                    del v["reserved"][num]
                    changed = True
                v["free"] = sorted(set(v["free"]))
            if changed:
                save_state(state)
                await update_status_message(state)
        await asyncio.sleep(30)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    args = message.text.split(maxsplit=1)
    payload = args[1] if len(args) > 1 else None

    if payload and payload.startswith("pay_"):
        try:
            idx = int(payload.split("_")[1])
        except Exception:
            idx = -1
        async with lock:
            state = load_state()
        if idx < 0 or idx >= len(state["variants"]):
            await message.answer("Вариант не найден.")
            return
        v = state["variants"][idx]
        user_reserves = [
            int(n) for n, d in v["reserved"].items()
            if d["user_id"] == message.from_user.id
        ]
        if not user_reserves:
            user_variant[message.from_user.id] = idx
            await message.answer(
                f"Вариант «{v['name']}».\n"
                f"Номера {v['from_n']}–{v['to_n']}, цена {v['price']} ₽.\n"
                "Напиши номер, например 3 или 1 4."
            )
            return
        amount = len(user_reserves) * v["price"]
        await message.answer(payment_text(state, v, amount, user_reserves), parse_mode="HTML")
        return

    async with lock:
        state = load_state()
    kb = ADMIN_KB if is_admin(message.from_user.id) and message.chat.type == "private" else None
    if not state["variants"]:
        await message.answer("Конкурс ещё не запущен.", reply_markup=kb)
        return
    await message.answer(build_status_text(state), parse_mode="HTML", reply_markup=kb or variant_keyboard(state))


@dp.callback_query(F.data.startswith("var_"))
async def on_variant(callback: CallbackQuery):
    idx = int(callback.data.split("_")[1])
    async with lock:
        state = load_state()
    if idx >= len(state["variants"]):
        await callback.answer("Вариант не найден", show_alert=True)
        return
    v = state["variants"][idx]
    url = f"https://t.me/{BOT_USERNAME}?start=pay_{idx}"
    await callback.message.answer(
        f"Выбран: <b>{v['name']}</b>\n"
        f"Номера {v['from_n']}–{v['to_n']}, {v['price']} ₽\n\n"
        "Напиши номер здесь или перейди в личку по кнопке.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="В личку", url=url)
        ]]),
    )
    await callback.answer()


@dp.message(F.text == "Новый конкурс")
async def btn_new(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await cmd_newcontest(message, state)


@dp.message(F.text == "Статус")
async def btn_status(message: Message):
    await cmd_status(message)


@dp.message(F.text == "Список")
async def btn_list(message: Message):
    await cmd_list(message)


@dp.message(Command("status"))
async def cmd_status(message: Message):
    async with lock:
        state = load_state()
    await message.answer(build_status_text(state), parse_mode="HTML")


@dp.message(Command("list"))
async def cmd_list(message: Message):
    if not is_admin(message.from_user.id):
        return
    async with lock:
        state = load_state()
    lines = ["<b>Список</b>\n"]
    for v in state["variants"]:
        lines.append(f"\n<b>{v['name']}</b>")
        for num in sorted(int(n) for n in v["sold"]):
            d = v["sold"][str(num)]
            lines.append(f"  {num}: {d['username']}")
        for num in sorted(int(n) for n in v["reserved"]):
            d = v["reserved"][str(num)]
            lines.append(f"  {num}: {d['username']} до {datetime.fromisoformat(d['until']).strftime('%H:%M')}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("setgroup"))
async def cmd_setgroup(message: Message):
    if not is_admin(message.from_user.id):
        return
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Пиши в группе.")
        return
    async with lock:
        state = load_state()
        state["group_id"] = message.chat.id
        save_state(state)
    text = build_status_text(state)
    kb = variant_keyboard(state)
    sent = await message.answer(text, parse_mode="HTML", reply_markup=kb)
    async with lock:
        state = load_state()
        state["status_message_id"] = sent.message_id
        state["status_is_photo"] = False
        save_state(state)
    await message.answer("Группа привязана. Можешь прислать картинку — я поставлю её как основное сообщение.")


@dp.message(F.photo, lambda m: is_admin(m.from_user.id))
async def admin_photo(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Кидай картинку в группу.")
        return
    async with lock:
        state = load_state()
        if not state.get("group_id"):
            await message.answer("Сначала напиши /setgroup в группе.")
            return
        text = build_status_text(state)
        kb = variant_keyboard(state)
        file_id = message.photo[-1].file_id
        sent = await bot.send_photo(
            state["group_id"],
            file_id,
            caption=text,
            parse_mode="HTML",
            reply_markup=kb,
        )
        state["status_message_id"] = sent.message_id
        state["status_is_photo"] = True
        save_state(state)
    await message.answer("Картинка поставлена. Буду обновлять подпись под ней.")


@dp.message(Command("newcontest"))
async def cmd_newcontest(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.chat.type != "private":
        await message.answer("Пиши в личку.")
        return
    await state.set_state(NewContest.title)
    await state.update_data(variants=[])
    await message.answer("Название конкурса?")


@dp.message(NewContest.title)
async def s_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(NewContest.subtitle)
    await message.answer("Подзаголовок?")


@dp.message(NewContest.subtitle)
async def s_sub(message: Message, state: FSMContext):
    await state.update_data(subtitle=message.text.strip())
    await state.set_state(NewContest.sbp_phone)
    await message.answer("Телефон СБП?")


@dp.message(NewContest.sbp_phone)
async def s_phone(message: Message, state: FSMContext):
    await state.update_data(sbp_phone=message.text.strip())
    await state.set_state(NewContest.sbp_name)
    await message.answer("Имя получателя?")


@dp.message(NewContest.sbp_name)
async def s_name(message: Message, state: FSMContext):
    await state.update_data(sbp_name=message.text.strip())
    await state.set_state(NewContest.sbp_comment)
    await message.answer("Подпись к переводу?")


@dp.message(NewContest.sbp_comment)
async def s_comment(message: Message, state: FSMContext):
    await state.update_data(sbp_comment=message.text.strip())
    await state.set_state(NewContest.var_name)
    await message.answer("Название первого варианта?\n(например: 5 участников, 1 приз)")


@dp.message(NewContest.var_name)
async def s_vname(message: Message, state: FSMContext):
    await state.update_data(var_name=message.text.strip())
    await state.set_state(NewContest.var_from)
    await message.answer("Номера от?")


@dp.message(NewContest.var_from)
async def s_vfrom(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("Число.")
        return
    await state.update_data(var_from=int(message.text.strip()))
    await state.set_state(NewContest.var_to)
    await message.answer("До?")


@dp.message(NewContest.var_to)
async def s_vto(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("Число.")
        return
    await state.update_data(var_to=int(message.text.strip()))
    await state.set_state(NewContest.var_price)
    await message.answer("Цена номера?")


@dp.message(NewContest.var_price)
async def s_vprice(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("Число.")
        return
    await state.update_data(var_price=int(message.text.strip()))
    await state.set_state(NewContest.var_minutes)
    await message.answer("Минут на оплату?")


@dp.message(NewContest.var_minutes)
async def s_vmin(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("Число.")
        return
    await state.update_data(var_minutes=int(message.text.strip()))
    await state.set_state(NewContest.var_prizes)
    await message.answer("Текст про призы (или «нет»)")


@dp.message(NewContest.var_prizes)
async def s_vprizes(message: Message, state: FSMContext):
    prizes = "" if message.text.strip().lower() == "нет" else message.text.strip()
    data = await state.get_data()
    variants = data.get("variants", [])
    variants.append({
        "name": data["var_name"],
        "from_n": data["var_from"],
        "to_n": data["var_to"],
        "price": data["var_price"],
        "minutes": data["var_minutes"],
        "prizes": prizes,
        "free": list(range(data["var_from"], data["var_to"] + 1)),
        "reserved": {},
        "sold": {},
    })
    await state.update_data(variants=variants)
    await state.set_state(NewContest.more)
    await message.answer("Добавить ещё вариант? Напиши «да» или «нет».")


@dp.message(NewContest.more)
async def s_more(message: Message, state: FSMContext):
    if message.text.strip().lower() == "да":
        await state.set_state(NewContest.var_name)
        await message.answer("Название следующего варианта?")
        return
    data = await state.get_data()
    await state.set_state(NewContest.confirm)
    lines = [f"<b>{data['title']}</b>", data["subtitle"], ""]
    for i, v in enumerate(data["variants"], 1):
        lines.append(f"{i}. {v['name']}: {v['from_n']}–{v['to_n']}, {v['price']} ₽, {v['prizes'] or 'без призов'}")
    lines.append("\nНапиши <b>да</b> чтобы запустить.")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(NewContest.confirm)
async def s_confirm(message: Message, state: FSMContext):
    if message.text.strip().lower() != "да":
        await state.clear()
        await message.answer("Отменено.")
        return
    data = await state.get_data()
    await state.clear()
    async with lock:
        st = load_state()
        st["title"] = data["title"]
        st["subtitle"] = data["subtitle"]
        st["sbp_phone"] = data["sbp_phone"]
        st["sbp_name"] = data["sbp_name"]
        st["sbp_comment"] = data["sbp_comment"]
        st["variants"] = data["variants"]
        st["seen_files"] = []
        save_state(st)
        if st.get("group_id"):
            text = build_status_text(st)
            kb = variant_keyboard(st)
            try:
                sent = await bot.send_message(st["group_id"], text, parse_mode="HTML", reply_markup=kb)
                st["status_message_id"] = sent.message_id
                save_state(st)
            except Exception as e:
                logging.warning(e)
    await message.answer("Конкурс запущен.")


def parse_numbers(text: str):
    clean = text.lower().replace("беру", " ").replace(",", " ")
    return [int(p) for p in clean.split() if p.isdigit()]


@dp.message(F.photo | F.document)
async def handle_check(message: Message):
    try:
        if message.chat.type != "private":
            await message.answer("Чек в личку.")
            return
        file_id = message.photo[-1].file_unique_id if message.photo else (
            message.document.file_unique_id if message.document else None
        )
        if not file_id:
            return
        async with lock:
            state = load_state()
            if file_id in state["seen_files"]:
                await message.answer("Этот файл уже был.")
                return
            found = []
            for vi, v in enumerate(state["variants"]):
                nums = [int(n) for n, d in v["reserved"].items() if d["user_id"] == message.from_user.id]
                if nums:
                    found.append((vi, nums))
            if not found:
                await message.answer("Нет активной брони.")
                return
            for vi, nums in found:
                v = state["variants"][vi]
                for num in nums:
                    data = v["reserved"].pop(str(num))
                    v["sold"][str(num)] = {
                        "user_id": message.from_user.id,
                        "username": data["username"],
                        "paid_at": now_msk().isoformat(),
                    }
            state["seen_files"].append(file_id)
            save_state(state)
            await update_status_message(state)
        all_nums = []
        for _, nums in found:
            all_nums.extend(nums)
        await message.answer(f"Чек получен. Номера {', '.join(map(str, sorted(all_nums)))} закреплены.")
    except Exception as e:
        logging.exception("Ошибка в handle_check")
        await message.answer("Не смог принять чек. Попробуй ещё раз.")


@dp.message(F.text)
async def handle_numbers(message: Message):
    try:
        if message.text.startswith("/"):
            return
        numbers = parse_numbers(message.text)
        if not numbers:
            return
        numbers = list(dict.fromkeys(numbers))

        async with lock:
            state = load_state()
            if not state["variants"]:
                await message.answer("Конкурс ещё не запущен.")
                return

            # Prefer the variant the user selected via button
            target = user_variant.get(message.from_user.id)
            if target is None or target >= len(state["variants"]):
                target = None
                for vi, v in enumerate(state["variants"]):
                    valid = set(range(v["from_n"], v["to_n"] + 1))
                    if any(n in valid for n in numbers):
                        target = vi
                        break
            if target is None:
                await message.answer("Таких номеров нет ни в одном варианте.")
                return

            v = state["variants"][target]
            valid = set(range(v["from_n"], v["to_n"] + 1))
            available, taken, own = [], [], []
            for n in numbers:
                if n not in valid:
                    continue
                if n in v["free"]:
                    available.append(n)
                else:
                    # already reserved?
                    res = v["reserved"].get(str(n))
                    if res and res["user_id"] == message.from_user.id:
                        own.append(n)
                    else:
                        taken.append(n)

            if not available and own:
                # user already has these numbers reserved — just show payment
                amount = len(own) * v["price"]
                await message.answer(payment_text(state, v, amount, own), parse_mode="HTML")
                return

            if not available:
                free = ", ".join(map(str, sorted(v["free"]))) or "нет"
                await message.answer(f"Занято. Свободные в «{v['name']}»: {free}")
                return

            until = (now_msk() + timedelta(minutes=v["minutes"])).isoformat()
            for n in available:
                v["free"].remove(n)
                v["reserved"][str(n)] = {
                    "user_id": message.from_user.id,
                    "username": get_user_name(message.from_user),
                    "until": until,
                    "check_received": False,
                }
            save_state(state)
            await update_status_message(state)

        amount = len(available) * v["price"]
        nums_str = ", ".join(map(str, sorted(available)))

        if message.chat.type == "private":
            await message.answer(payment_text(state, v, amount, available), parse_mode="HTML")
        else:
            url = f"https://t.me/{BOT_USERNAME}?start=pay_{target}"
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Перейти к оплате", url=url)
            ]])
            await message.answer(
                f"«{v['name']}»: номера <b>{nums_str}</b> на {v['minutes']} мин.\nНажми кнопку.",
                reply_markup=kb,
                parse_mode="HTML",
            )
        if taken:
            await message.answer(f"Уже заняты: {taken}")
    except Exception as e:
        logging.exception("Ошибка в handle_numbers")
        await message.answer("Что-то пошло не так. Попробуй ещё раз или напиши админу.")


async def main():
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать"),
        BotCommand(command="newcontest", description="Новый конкурс"),
        BotCommand(command="status", description="Свободные номера"),
        BotCommand(command="list", description="Полный список"),
        BotCommand(command="setgroup", description="Привязать группу"),
    ])
    logging.info(f"Бот @{BOT_USERNAME} запущен")
    asyncio.create_task(clear_expired())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())o.run(main())
