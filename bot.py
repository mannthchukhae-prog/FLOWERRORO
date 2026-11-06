"""
Telegram-бот «Калькулятор цены букета» для цветочной мастерской на УСН + НДС.

Команды:
  /start      — приветствие и меню
  /price      — рассчитать цену букета
  /quick      — быстрый расчёт по общей закупочной стоимости
  /flowers    — справочник цветов (список, удаление)
  /addflower  — добавить цветок в справочник
  /settings   — параметры мастерской (налоги, наценки, расходы)
"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

from db import DB
from pricing import BouquetItem, Settings, calc_price, compare_modes, raw_cogs

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
db = DB(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ---------------------------------------------------------------- helpers

def fmt(n: float) -> str:
    return f"{n:,.0f}".replace(",", " ") + " ₽"


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💐 Рассчитать букет", callback_data="price")],
        [InlineKeyboardButton(text="⚡ Быстрый расчёт", callback_data="quick")],
        [InlineKeyboardButton(text="🌸 Справочник цветов", callback_data="flowers")],
        [InlineKeyboardButton(text="⚙️ Настройки мастерской", callback_data="settings")],
    ])


SETTING_FIELDS = {
    "tax_mode":         ("Налоговый режим", "choice", ["usn6", "usn15"]),
    "vat_rate":         ("Ставка НДС, %", "num", None),
    "acquiring_pct":    ("Эквайринг, % от цены", "num", None),
    "marketplace_pct":  ("Комиссия маркетплейса, % от цены", "num", None),
    "waste_pct":        ("Убыль/списания, % от закупки", "num", None),
    "logistics_pct":    ("Логистика от поставщика, % к закупке", "num", None),
    "florist_mode":     ("Оплата флориста", "choice", ["percent", "fixed"]),
    "florist_pct":      ("Флорист, % от цены", "num", None),
    "florist_fixed":    ("Флорист, фикс ₽ за букет", "num", None),
    "fixed_monthly":    ("Аренда + постоянные, ₽/мес", "num", None),
    "planned_bouquets": ("План продаж, букетов/мес", "int", None),
    "target_margin_pct":("Целевая чистая маржа, % от цены", "num", None),
}

CHOICE_LABELS = {
    "usn6": "УСН «Доходы» 6%",
    "usn15": "УСН «Доходы−расходы» 15%",
    "percent": "% от цены букета",
    "fixed": "фикс ₽ за букет",
}


def settings_text(s: Settings) -> str:
    lines = ["⚙️ <b>Настройки мастерской</b>\n"]
    for key, (label, kind, _) in SETTING_FIELDS.items():
        val = getattr(s, key)
        if kind == "choice":
            val = CHOICE_LABELS.get(str(val), val)
        lines.append(f"• {label}: <b>{val}</b>")
    lines.append("\nНажмите на параметр, чтобы изменить его.")
    return "\n".join(lines)


def settings_kb() -> InlineKeyboardMarkup:
    rows = []
    for key, (label, _, _) in SETTING_FIELDS.items():
        rows.append([InlineKeyboardButton(text=label, callback_data=f"set:{key}")])
    rows.append([InlineKeyboardButton(text="« В меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def result_text(title: str, items: list, extra_pack: float, s: Settings) -> str:
    comp = compare_modes(items, extra_pack, s)
    active = comp[s.tax_mode]
    other_mode = "usn15" if s.tax_mode == "usn6" else "usn6"
    other = comp[other_mode]

    if not active.ok:
        return f"⚠️ {active.error}"

    out = [f"🧾 <b>{title}</b>\n"]
    out.append("<b>Состав:</b>")
    for i in items:
        out.append(f"• {i.name} × {i.qty} — закупка {fmt(i.purchase_price)}/шт")
    out.append(f"\nСырая закупка букета: <b>{fmt(raw_cogs(items))}</b>")
    out.append(f"Полная себестоимость: <b>{fmt(active.cost_total)}</b>\n")

    out.append(f"<b>Цена продажи ({CHOICE_LABELS[s.tax_mode]}):</b>")
    out.append(f"<b>💰 {fmt(active.price)}</b>\n")
    out.append("<b>Что зашито в цену:</b>")
    for label, val in active.lines:
        out.append(f"• {label}: {fmt(val)}")
    out.append(f"\nМаржа: {active.margin_pct:.0f}% · наценка на закупку: ×{1 + active.markup_on_cogs/100:.1f}")
    out.append(f"<i>(на рынке обычно ×2–×3,5 — вы в пределах нормы)</i>")

    if other.ok:
        out.append(f"\nДля сравнения, при {CHOICE_LABELS[other_mode]}: {fmt(other.price)} "
                   f"(прибыль {fmt(other.profit)})")
    return "\n".join(out)


# ---------------------------------------------------------------- states

class AddFlower(StatesGroup):
    name = State()
    price = State()
    packaging = State()


class EditSetting(StatesGroup):
    value = State()


class Calc(StatesGroup):
    qty = State()
    packaging = State()


class Quick(StatesGroup):
    amount = State()


# ---------------------------------------------------------------- handlers

@dp.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    db.get_settings(m.chat.id)  # создаст запись с дефолтами при первом запуске
    await m.answer(
        "🌷 <b>Калькулятор цены букета</b>\n\n"
        "Считаю продажную цену так, чтобы в ней уже сидели закупка, логистика, "
        "убыль, работа флориста, аренда, эквайринг, НДС 5% и налог УСН — "
        "а сверху оставалась ваша целевая маржа.\n\n"
        "Сначала загляните в ⚙️ Настройки и заполните справочник цветов.",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("Главное меню:", reply_markup=main_menu())
    await c.answer()


# ---------- настройки ----------

@dp.message(Command("settings"))
@dp.callback_query(F.data == "settings")
async def show_settings(event, state: FSMContext):
    await state.clear()
    chat_id = event.chat.id if isinstance(event, Message) else event.message.chat.id
    s = db.get_settings(chat_id)
    text, kb = settings_text(s), settings_kb()
    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb)
    else:
        await event.message.edit_text(text, reply_markup=kb)
        await event.answer()


@dp.callback_query(F.data.startswith("set:"))
async def edit_setting(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":", 1)[1]
    label, kind, choices = SETTING_FIELDS[key]
    if kind == "choice":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=CHOICE_LABELS[v], callback_data=f"setval:{key}:{v}")]
            for v in choices
        ])
        await c.message.edit_text(f"Выберите значение для «{label}»:", reply_markup=kb)
        await c.answer()
        return
    await state.set_state(EditSetting.value)
    await state.update_data(field=key, kind=kind)
    await c.message.edit_text(
        f"Введите новое значение для «{label}» (число):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data="settings")]
        ]),
    )
    await c.answer()


@dp.callback_query(F.data.startswith("setval:"))
async def set_choice(c: CallbackQuery, state: FSMContext):
    _, key, val = c.data.split(":", 2)
    db.update_setting(c.message.chat.id, key, val)
    s = db.get_settings(c.message.chat.id)
    await c.message.edit_text(settings_text(s), reply_markup=settings_kb())
    await c.answer("Сохранено ✅")


@dp.message(EditSetting.value)
async def save_setting(m: Message, state: FSMContext):
    data = await state.get_data()
    try:
        val = float(m.text.replace(",", ".").replace(" ", ""))
        if data["kind"] == "int":
            val = int(val)
    except ValueError:
        await m.answer("Нужно число. Попробуйте ещё раз:")
        return
    db.update_setting(m.chat.id, data["field"], val)
    await state.clear()
    s = db.get_settings(m.chat.id)
    await m.answer("Сохранено ✅\n\n" + settings_text(s), reply_markup=settings_kb())


# ---------- справочник цветов ----------

@dp.message(Command("flowers"))
@dp.callback_query(F.data == "flowers")
async def show_flowers(event, state: FSMContext):
    await state.clear()
    chat_id = event.chat.id if isinstance(event, Message) else event.message.chat.id
    flowers = db.list_flowers(chat_id)
    if not flowers:
        text = "🌸 Справочник пуст. Добавьте первый цветок командой /addflower"
        kb_rows = []
    else:
        text = "🌸 <b>Справочник цветов</b> (закупка за стебель):\n" + "\n".join(
            f"• {f['name']} — {fmt(f['purchase_price'])}"
            + (f" + упаковка {fmt(f['packaging'])}" if f["packaging"] else "")
            for f in flowers
        )
        kb_rows = [[InlineKeyboardButton(text=f"🗑 {f['name']}", callback_data=f"delf:{f['id']}")]
                   for f in flowers]
    kb_rows.append([InlineKeyboardButton(text="➕ Добавить цветок", callback_data="addflower")])
    kb_rows.append([InlineKeyboardButton(text="« В меню", callback_data="menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb)
    else:
        await event.message.edit_text(text, reply_markup=kb)
        await event.answer()


@dp.callback_query(F.data.startswith("delf:"))
async def del_flower(c: CallbackQuery):
    db.delete_flower(int(c.data.split(":")[1]))
    await show_flowers(c, None)
    await c.answer("Удалено")


# ---------- добавление цветка ----------

@dp.message(Command("addflower"))
@dp.callback_query(F.data == "addflower")
async def add_flower_start(event, state: FSMContext):
    await state.set_state(AddFlower.name)
    text = "Название цветка (например, «Роза Эквадор 60 см»):"
    if isinstance(event, Message):
        await event.answer(text)
    else:
        await event.message.answer(text)
        await event.answer()


@dp.message(AddFlower.name)
async def add_flower_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await state.set_state(AddFlower.price)
    await m.answer("Закупочная цена за стебель, ₽:")


@dp.message(AddFlower.price)
async def add_flower_price(m: Message, state: FSMContext):
    try:
        price = float(m.text.replace(",", "."))
    except ValueError:
        await m.answer("Нужно число. Попробуйте ещё раз:")
        return
    await state.update_data(price=price)
    await state.set_state(AddFlower.packaging)
    await m.answer("Упаковка на один стебель, ₽ (0 — если не нужна):")


@dp.message(AddFlower.packaging)
async def add_flower_pack(m: Message, state: FSMContext):
    try:
        pack = float(m.text.replace(",", "."))
    except ValueError:
        await m.answer("Нужно число. Попробуйте ещё раз:")
        return
    data = await state.get_data()
    db.add_flower(m.chat.id, data["name"], data["price"], pack)
    await state.clear()
    await m.answer(f"✅ «{data['name']}» добавлен в справочник.", reply_markup=main_menu())


# ---------- расчёт букета ----------

@dp.message(Command("price"))
@dp.callback_query(F.data == "price")
async def price_start(event, state: FSMContext):
    chat_id = event.chat.id if isinstance(event, Message) else event.message.chat.id
    flowers = db.list_flowers(chat_id)
    if not flowers:
        text = "Справочник пуст — сначала добавьте цветы через /addflower"
        if isinstance(event, Message):
            await event.answer(text)
        else:
            await event.answer(text, show_alert=True)
        return
    await state.update_data(items=[])
    await _offer_flowers(event, flowers)


async def _offer_flowers(event, flowers):
    rows = [[InlineKeyboardButton(
                text=f"{f['name']} ({fmt(f['purchase_price'])})",
                callback_data=f"pick:{f['id']}")]
            for f in flowers]
    rows.append([InlineKeyboardButton(text="✅ Готово, считать", callback_data="calc_done")])
    rows.append([InlineKeyboardButton(text="« Отмена", callback_data="menu")])
    text = "Выберите цветок для добавления в букет:"
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb)
    else:
        await event.message.edit_text(text, reply_markup=kb)
        await event.answer()


@dp.callback_query(F.data.startswith("pick:"))
async def pick_flower(c: CallbackQuery, state: FSMContext):
    fid = int(c.data.split(":")[1])
    flowers = {f["id"]: f for f in db.list_flowers(c.message.chat.id)}
    f = flowers.get(fid)
    if not f:
        await c.answer("Цветок не найден", show_alert=True)
        return
    await state.set_state(Calc.qty)
    await state.update_data(picked=f)
    await c.message.edit_text(f"«{f['name']}»: сколько стеблей в букете?")
    await c.answer()


@dp.message(Calc.qty)
async def set_qty(m: Message, state: FSMContext):
    try:
        qty = int(m.text)
        assert qty > 0
    except (ValueError, AssertionError):
        await m.answer("Нужно целое положительное число:")
        return
    data = await state.get_data()
    f = data["picked"]
    items = data["items"]
    items.append({"name": f["name"], "qty": qty,
                  "purchase_price": float(f["purchase_price"]),
                  "packaging": float(f["packaging"])})
    await state.update_data(items=items)
    await state.set_state(None)
    await m.answer(f"Добавлено: {f['name']} × {qty}. В букете позиций: {len(items)}.")
    await _offer_flowers(m, db.list_flowers(m.chat.id))


@dp.callback_query(F.data == "calc_done")
async def calc_done(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("items"):
        await c.answer("Букет пуст — добавьте хотя бы один цветок", show_alert=True)
        return
    await state.set_state(Calc.packaging)
    await c.message.edit_text(
        "Стоимость общей упаковки букета (бумага, ленты, открытка), ₽ — или 0:"
    )
    await c.answer()


@dp.message(Calc.packaging)
async def calc_packaging(m: Message, state: FSMContext):
    try:
        pack = float(m.text.replace(",", "."))
    except ValueError:
        await m.answer("Нужно число. Попробуйте ещё раз:")
        return
    data = await state.get_data()
    items = [BouquetItem(**i) for i in data["items"]]
    s = db.get_settings(m.chat.id)
    await state.clear()
    await m.answer(result_text("Расчёт цены букета", items, pack, s),
                   reply_markup=main_menu())


# ---------- быстрый расчёт ----------

@dp.message(Command("quick"))
@dp.callback_query(F.data == "quick")
async def quick_start(event, state: FSMContext):
    await state.set_state(Quick.amount)
    text = "Введите суммарную закупочную стоимость букета, ₽ (цветы + упаковка):"
    if isinstance(event, Message):
        await event.answer(text)
    else:
        await event.message.answer(text)
        await event.answer()


@dp.message(Quick.amount)
async def quick_calc(m: Message, state: FSMContext):
    try:
        amount = float(m.text.replace(",", ".").replace(" ", ""))
    except ValueError:
        await m.answer("Нужно число. Попробуйте ещё раз:")
        return
    await state.clear()
    s = db.get_settings(m.chat.id)
    items = [BouquetItem(name="Букет (общая закупка)", qty=1, purchase_price=amount)]
    await m.answer(result_text("Быстрый расчёт", items, 0.0, s),
                   reply_markup=main_menu())


# ---------------------------------------------------------------- entry

async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
