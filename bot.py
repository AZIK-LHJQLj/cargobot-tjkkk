import asyncio
import sqlite3
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cargoline")

# --- ТОКЕНЫ БОТОВ (не менять) ---
CLIENT_BOT_TOKEN = "8062523385:AAEiT_k4GgnruspjJ8NpR89fLLpYo3DWd6I"
ADMIN_BOT_TOKEN = "8863411087:AAEyzpsG9-S__O1ys9-7S9VSH8vF4gOtP60"

# Впиши свой Telegram ID (@userinfobot), чтобы только ты пользовался админкой.
# Пустой список = доступ открыт всем, кто пишет админ-боту.
ADMIN_IDS = []

# Часовой пояс Таджикистана
TZ = ZoneInfo("Asia/Dushanbe")

# Лимит массовой загрузки за раз
MAX_BULK = 3000

# Через сколько дней «В Китае» → «В пути»
AUTO_TRANSIT_DAYS = 3

DB_PATH = os.environ.get("DB_PATH", "cargo_database.db")

STATUS_CHINA = "В Китае"
STATUS_TRANSIT = "В пути"
STATUS_KHUJAND = "На складе в Худжанде"

client_bot = Bot(token=CLIENT_BOT_TOKEN)
admin_bot = Bot(token=ADMIN_BOT_TOKEN)

dp_client = Dispatcher(storage=MemoryStorage())
dp_admin = Dispatcher(storage=MemoryStorage())


# --- БАЗА ДАННЫХ ---
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def now_str():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS tracks (
            track_code TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            china_at TEXT,
            transit_at TEXT,
            khujand_at TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            language TEXT DEFAULT 'RU'
        )
        """
    )

    # Миграция со старой схемы (если была только created_at)
    cols = {row[1] for row in cursor.execute("PRAGMA table_info(tracks)").fetchall()}
    for col in ("china_at", "transit_at", "khujand_at"):
        if col not in cols:
            cursor.execute(f"ALTER TABLE tracks ADD COLUMN {col} TEXT")

    # Старым записям без дат — проставить created_at в нужное поле
    cursor.execute(
        f"""
        UPDATE tracks
        SET china_at = COALESCE(china_at, created_at)
        WHERE status = ? AND china_at IS NULL
        """,
        (STATUS_CHINA,),
    )
    cursor.execute(
        f"""
        UPDATE tracks
        SET transit_at = COALESCE(transit_at, created_at)
        WHERE status = ? AND transit_at IS NULL
        """,
        (STATUS_TRANSIT,),
    )
    cursor.execute(
        f"""
        UPDATE tracks
        SET khujand_at = COALESCE(khujand_at, created_at)
        WHERE status = ? AND khujand_at IS NULL
        """,
        (STATUS_KHUJAND,),
    )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracks_code ON tracks(track_code)"
    )
    conn.commit()
    conn.close()


def get_user_lang(user_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return res["language"] if res else "RU"


def set_user_lang(user_id, lang):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO users (user_id, language) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET language = excluded.language
        """,
        (user_id, lang),
    )
    conn.commit()
    conn.close()


def parse_track_list(raw_text: str) -> list[str]:
    """Разбивает текст на трек-коды (до MAX_BULK). Быстро и без дублей в одном пакете."""
    parts = re.split(r"[\s,;]+", raw_text.strip())
    seen = set()
    result = []
    for p in parts:
        code = p.strip().upper()
        if not code:
            continue
        if code in seen:
            continue
        seen.add(code)
        result.append(code)
        if len(result) >= MAX_BULK:
            break
    return result


def add_or_update_tracks(tracks: list[str], status: str) -> dict:
    """Массово добавляет/обновляет треки. Сохраняет дату/время статуса."""
    conn = get_conn()
    cursor = conn.cursor()
    ts = now_str()
    added = 0
    updated = 0

    china_at = ts if status == STATUS_CHINA else None
    transit_at = ts if status == STATUS_TRANSIT else None
    khujand_at = ts if status == STATUS_KHUJAND else None

    for track in tracks:
        cursor.execute("SELECT status FROM tracks WHERE track_code = ?", (track,))
        existing = cursor.fetchone()

        if existing is None:
            cursor.execute(
                """
                INSERT INTO tracks (
                    track_code, status, created_at, china_at, transit_at, khujand_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (track, status, ts, china_at, transit_at, khujand_at),
            )
            added += 1
        else:
            # Обновляем статус и дату только этого статуса
            if status == STATUS_CHINA:
                cursor.execute(
                    """
                    UPDATE tracks
                    SET status = ?, china_at = COALESCE(china_at, ?)
                    WHERE track_code = ?
                    """,
                    (status, ts, track),
                )
            elif status == STATUS_TRANSIT:
                cursor.execute(
                    """
                    UPDATE tracks
                    SET status = ?, transit_at = ?
                    WHERE track_code = ?
                    """,
                    (status, ts, track),
                )
            else:
                cursor.execute(
                    """
                    UPDATE tracks
                    SET status = ?, khujand_at = ?
                    WHERE track_code = ?
                    """,
                    (status, ts, track),
                )
            updated += 1

    conn.commit()
    conn.close()
    return {"added": added, "updated": updated, "total": added + updated, "at": ts}


def get_track(track_code: str):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT track_code, status, created_at, china_at, transit_at, khujand_at
        FROM tracks WHERE track_code = ?
        """,
        (track_code.strip().upper(),),
    )
    res = cursor.fetchone()
    conn.close()
    return dict(res) if res else None


def get_stats():
    conn = get_conn()
    cursor = conn.cursor()
    total = cursor.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    china = cursor.execute(
        "SELECT COUNT(*) FROM tracks WHERE status = ?", (STATUS_CHINA,)
    ).fetchone()[0]
    transit = cursor.execute(
        "SELECT COUNT(*) FROM tracks WHERE status = ?", (STATUS_TRANSIT,)
    ).fetchone()[0]
    khujand = cursor.execute(
        "SELECT COUNT(*) FROM tracks WHERE status = ?", (STATUS_KHUJAND,)
    ).fetchone()[0]
    conn.close()
    return total, china, transit, khujand


def auto_move_china_to_transit() -> int:
    """Все треки «В Китае» старше 3 дней → «В пути»."""
    conn = get_conn()
    cursor = conn.cursor()
    cutoff = (datetime.now(TZ) - timedelta(days=AUTO_TRANSIT_DAYS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    ts = now_str()

    cursor.execute(
        """
        SELECT track_code FROM tracks
        WHERE status = ?
          AND COALESCE(china_at, created_at) <= ?
        """,
        (STATUS_CHINA, cutoff),
    )
    rows = cursor.fetchall()
    count = 0
    for row in rows:
        cursor.execute(
            """
            UPDATE tracks
            SET status = ?, transit_at = ?
            WHERE track_code = ?
            """,
            (STATUS_TRANSIT, ts, row["track_code"]),
        )
        count += 1

    conn.commit()
    conn.close()
    return count


def format_dt(value: str | None) -> str:
    if not value:
        return "—"
    # "2026-03-20 14:30:00" → "20.03.2026 14:30"
    try:
        dt = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def format_track_card(row: dict) -> str:
    status = row["status"]
    icon = (
        "🇨🇳"
        if status == STATUS_CHINA
        else ("🚛" if status == STATUS_TRANSIT else "🏢")
    )
    return (
        f"📦 <b>Трек-код:</b> <code>{row['track_code']}</code>\n"
        f"{icon} <b>Текущий статус:</b> <b>{status}</b>\n\n"
        f"📅 <b>История:</b>\n"
        f"🇨🇳 На складе в Китае: <b>{format_dt(row.get('china_at'))}</b>\n"
        f"🚛 В пути: <b>{format_dt(row.get('transit_at'))}</b>\n"
        f"🏢 На складе в Худжанде: <b>{format_dt(row.get('khujand_at'))}</b>\n\n"
        f"➕ Добавлен в базу: <b>{format_dt(row.get('created_at'))}</b>"
    )


# --- КЛАВИАТУРЫ КЛИЕНТА ---
def get_main_keyboard(lang="RU"):
    if lang == "TJ":
        kb = [
            [
                KeyboardButton(text="🔍 Пайгирии бор (Отследить)"),
                KeyboardButton(text="📍 Султони Чин (Адрес)"),
            ],
            [KeyboardButton(text="🧮 Калькулятор"), KeyboardButton(text="🚫 Молҳои манъшуда")],
            [KeyboardButton(text="📞 Контактҳо"), KeyboardButton(text="🌐 Тағири забон")],
            [KeyboardButton(text="🔄 Тағири карго / Тариф")],
        ]
    else:
        kb = [
            [
                KeyboardButton(text="🔍 Отследить трек-код"),
                KeyboardButton(text="📍 Адрес склада в Китае"),
            ],
            [KeyboardButton(text="🧮 Калькулятор"), KeyboardButton(text="🚫 Запрещенные товары")],
            [KeyboardButton(text="📞 Контакты"), KeyboardButton(text="🌐 Сменить язык")],
            [KeyboardButton(text="🔄 Переключение тарифа / Карго")],
        ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


class AdminStates(StatesGroup):
    waiting_for_china_tracks = State()
    waiting_for_transit_tracks = State()
    waiting_for_khujand_tracks = State()


def is_admin(user_id: int) -> bool:
    if not ADMIN_IDS:
        return True
    return user_id in ADMIN_IDS


# ==========================================
#          КЛИЕНТСКИЙ БОТ
# ==========================================


@dp_client.message(CommandStart())
async def client_start(message: types.Message):
    lang = get_user_lang(message.from_user.id)
    text = (
        "✨ <b>АС САЛАМУ АЛЕЙКУМ!</b> ✨\n"
        "Вас приветствует компания <b>Cargo Line TJ</b>! 🚚💨\n\n"
        "Мы обеспечиваем быструю и надежную доставку ваших грузов из Китая в Таджикистан.\n"
        "Выберите нужное действие в меню ниже 👇"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard(lang))


@dp_client.message(F.text.in_(["📍 Адрес склада в Китае", "📍 Султони Чин (Адрес)"]))
async def show_address(message: types.Message):
    address_text = (
        "<b>📍 Адрес склада в Китае (копируйте полностью):</b>\n\n"
        "<code>收货人：MA \n"
        "手机号码：13311198266\n"
        "所在地区： 浙江省金华市义乌市 北苑街道\n"
        "详细地址: 浙江省金华市义乌市 北苑街道凌云三区9栋2单元扎娜供应链Z178仓库 МА-(ваш номер)</code>\n\n"
        "⚠️ <b>Обязательно вместо <code>МА-(ваш номер)</code> укажите ваш личный код!</b>"
    )
    await message.answer(address_text, parse_mode="HTML")


@dp_client.message(F.text.in_(["🧮 Калькулятор"]))
async def show_calc(message: types.Message):
    text = (
        "💎 <b>Удобный калькулятор Cargo Line TJ</b> 💎\n\n"
        "💰 <b>Тарифы:</b>\n"
        "• <b>1 кг</b> = 25 сомони\n"
        "• <b>1 куб ($m^3$)</b> = $260\n\n"
        "Для расчета отправьте сообщение в формате:\n"
        "👉 <code>кг 5.5</code> — для расчета по весу\n"
        "👉 <code>куб 0.5</code> — для расчета по объему"
    )
    await message.answer(text, parse_mode="HTML")


@dp_client.message(F.text.in_(["🚫 Запрещенные товары", "🚫 Молҳои манъшуда"]))
async def show_prohibited(message: types.Message):
    text = (
        "🛑 <b>КАТЕГОРИЧЕСКИ ЗАПРЕЩЕННЫЕ К ПЕРЕВОЗКЕ ТОВАРЫ:</b>\n\n"
        "❌ <b>Электронные сигареты</b> (вейпы, жидкости, POD-системы)\n"
        "❌ <b>Химические товары</b> (опасные реагенты, кислоты, яды)\n"
        "❌ <b>Холодное оружие</b> (ножи, кастеты, спецсредства)\n"
        "❌ <b>Медицинские препараты</b> (лекарства без сертификации, шприцы)\n"
        "❌ <b>Живые растения</b> (семена, саженцы, цветы)\n"
        "❌ <b>Пищевые товары</b> (скоропортящиеся продукты)\n"
        "❌ <b>Взрывчатые вещества</b> (пиротехника, салюты, баллоны)"
    )
    await message.answer(text, parse_mode="HTML")


@dp_client.message(F.text.in_(["📞 Контакты", "📞 Контактҳо"]))
async def show_contacts(message: types.Message):
    text = (
        "📞 <b>НАШИ КОНТАКТЫ И АДРЕС:</b>\n\n"
        "📱 <b>Telegram:</b> +992926277667\n"
        "💬 <b>WhatsApp:</b> +992719277667\n"
        "📸 <b>Instagram:</b> <a href='https://www.instagram.com/cargoline.tj?igsh=dmM3aDViMHV3aHI4'>cargoline.tj</a>\n\n"
        "📍 <b>Адрес склада в Таджикистане:</b>\n"
        "трасса Худжанд — Гафуров (прямо рядом с рестораном <b>ДИДОР</b>)"
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


@dp_client.message(F.text.in_(["🌐 Сменить язык", "🌐 Тағири забон"]))
async def change_lang(message: types.Message):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🇹🇯 Тоҷикӣ", callback_data="set_lang_TJ")],
            [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="set_lang_RU")],
        ]
    )
    await message.answer("Интихоби забон / Выберите язык:", reply_markup=kb)


@dp_client.callback_query(F.data.startswith("set_lang_"))
async def set_lang_callback(call: types.CallbackQuery):
    lang = call.data.split("_")[2]
    set_user_lang(call.from_user.id, lang)
    msg = (
        "Забон ба тоҷикӣ иваз карда шуд! 🇹🇯"
        if lang == "TJ"
        else "Язык успешно изменен на русский! 🇷🇺"
    )
    await call.message.answer(msg, reply_markup=get_main_keyboard(lang))
    await call.answer()


@dp_client.message(F.text.in_(["🔄 Переключение тарифа / Карго", "🔄 Тағири карго / Тариф"]))
async def change_cargo_info(message: types.Message):
    text = (
        "🏢 <b>Филиал и Тариф Cargo Line TJ:</b>\n\n"
        "📍 <b>Филиал:</b> г. Худжанд (трасса Худжанд-Гафуров, ориентир: ресторан ДИДОР)\n"
        "⚡️ <b>Текущий тариф:</b> Стандартный (Авто / Экспресс)"
    )
    await message.answer(text, parse_mode="HTML")


@dp_client.message(F.text.in_(["🔍 Отследить трек-код", "🔍 Пайгирии бор (Отследить)"]))
async def track_prompt(message: types.Message):
    await message.answer("🔍 Отправьте ваш трек-код в чат для проверки статуса:")


@dp_client.message()
async def process_client_text(message: types.Message):
    text = message.text.strip()

    if text.lower().startswith("кг"):
        try:
            val = float(text.split()[1].replace(",", "."))
            total_som = val * 25
            await message.answer(
                f"⚖️ Вес: <b>{val} кг</b>\n💵 Итого к оплате: <b>{total_som:.2f} сомони</b>",
                parse_mode="HTML",
            )
            return
        except Exception:
            pass

    if text.lower().startswith("куб"):
        try:
            val = float(text.split()[1].replace(",", "."))
            total_usd = val * 260
            await message.answer(
                f"📦 Объем: <b>{val} м³</b>\n💵 Итого к оплате: <b>${total_usd:.2f} (USD)</b>",
                parse_mode="HTML",
            )
            return
        except Exception:
            pass

    row = get_track(text)
    if row:
        await message.answer(format_track_card(row), parse_mode="HTML")
    else:
        await message.answer(
            f"❌ Трек-код <code>{text.upper()}</code> пока не зарегистрирован в системе.",
            parse_mode="HTML",
        )


# ==========================================
#           АДМИН-БОТ
# ==========================================


def admin_keyboard():
    kb = [
        [KeyboardButton(text="🇨🇳 На складе в Китае")],
        [KeyboardButton(text="🚛 В пути")],
        [KeyboardButton(text="🏢 На складе в Худжанде")],
        [KeyboardButton(text="📊 Статистика и База")],
        [KeyboardButton(text="⏱ Обновить статусы сейчас")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


async def extract_tracks_from_message(message: types.Message) -> list[str]:
    """Текст или .txt файл — до 3000 треков."""
    if message.document:
        file = await admin_bot.get_file(message.document.file_id)
        raw = await admin_bot.download_file(file.file_path)
        content = raw.read().decode("utf-8", errors="ignore")
        return parse_track_list(content)

    if message.text:
        return parse_track_list(message.text)

    return []


async def process_bulk(message: types.Message, state: FSMContext, status: str):
    wait = await message.answer("⏳ Обрабатываю трек-коды...")
    tracks = await extract_tracks_from_message(message)

    if not tracks:
        await wait.edit_text("❌ Не найдено ни одного трек-кода. Пришлите список текстом или .txt файлом.")
        return

    truncated = len(tracks) >= MAX_BULK
    result = add_or_update_tracks(tracks, status)

    extra = ""
    if truncated:
        extra = f"\n⚠️ Принят максимум <b>{MAX_BULK}</b> кодов за раз."

    await wait.edit_text(
        f"✅ Готово!\n\n"
        f"📌 Статус: <b>{status}</b>\n"
        f"🆕 Новых: <b>{result['added']}</b>\n"
        f"🔄 Обновлено: <b>{result['updated']}</b>\n"
        f"📦 Всего в пакете: <b>{result['total']}</b>\n"
        f"🕒 Дата/время: <b>{format_dt(result['at'])}</b>"
        f"{extra}\n\n"
        f"<i>Можно снова выбрать статус и загрузить ещё до {MAX_BULK} кодов.</i>",
        parse_mode="HTML",
    )
    await state.clear()


@dp_admin.message(CommandStart())
async def admin_start(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    await message.answer(
        "⚙️ <b>Панель управления Cargo Line TJ</b>\n\n"
        "Выберите статус и пришлите список трек-кодов:\n"
        "• текстом (каждый с новой строки / через пробел / запятую)\n"
        f"• или файлом <b>.txt</b> — до <b>{MAX_BULK}</b> кодов за раз\n\n"
        f"⏱ Треки «В Китае» автоматически станут «В пути» через <b>{AUTO_TRANSIT_DAYS} дня</b>.",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )


@dp_admin.message(F.text == "📊 Статистика и База")
async def show_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    total, china, transit, khujand = get_stats()
    text = (
        "📊 <b>СТАТИСТИКА БАЗЫ:</b>\n\n"
        f"📦 Всего трек-кодов: <b>{total}</b>\n"
        f"🇨🇳 На складе в Китае: <b>{china}</b>\n"
        f"🚛 В пути: <b>{transit}</b>\n"
        f"🏢 На складе в Худжанде: <b>{khujand}</b>\n\n"
        f"⏱ Авто: «В Китае» → «В пути» через {AUTO_TRANSIT_DAYS} дня"
    )
    await message.answer(text, parse_mode="HTML")


@dp_admin.message(F.text == "⏱ Обновить статусы сейчас")
async def force_auto_transit(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    moved = auto_move_china_to_transit()
    await message.answer(
        f"✅ Перенесено в «В пути»: <b>{moved}</b> трек-кодов "
        f"(которым уже прошло {AUTO_TRANSIT_DAYS} дня на складе в Китае).",
        parse_mode="HTML",
    )


@dp_admin.message(F.text == "🇨🇳 На складе в Китае")
async def admin_china(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_for_china_tracks)
    await message.answer(
        f"📥 Пришлите трек-коды для статуса <b>«В Китае»</b>\n"
        f"(текст или .txt, до {MAX_BULK} шт).\n\n"
        f"Через {AUTO_TRANSIT_DAYS} дня они сами станут «В пути».",
        parse_mode="HTML",
    )


@dp_admin.message(F.text == "🚛 В пути")
async def admin_transit(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_for_transit_tracks)
    await message.answer(
        f"📥 Пришлите трек-коды для статуса <b>«В пути»</b>\n"
        f"(текст или .txt, до {MAX_BULK} шт).",
        parse_mode="HTML",
    )


@dp_admin.message(F.text == "🏢 На складе в Худжанде")
async def admin_khujand(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_for_khujand_tracks)
    await message.answer(
        f"📥 Пришлите трек-коды для статуса <b>«На складе в Худжанде»</b>\n"
        f"(текст или .txt, до {MAX_BULK} шт).",
        parse_mode="HTML",
    )


@dp_admin.message(AdminStates.waiting_for_china_tracks, F.text | F.document)
async def process_china_tracks(message: types.Message, state: FSMContext):
    await process_bulk(message, state, STATUS_CHINA)


@dp_admin.message(AdminStates.waiting_for_transit_tracks, F.text | F.document)
async def process_transit_tracks(message: types.Message, state: FSMContext):
    await process_bulk(message, state, STATUS_TRANSIT)


@dp_admin.message(AdminStates.waiting_for_khujand_tracks, F.text | F.document)
async def process_khujand_tracks(message: types.Message, state: FSMContext):
    await process_bulk(message, state, STATUS_KHUJAND)


# --- ФОНОВАЯ ЗАДАЧА: авто «В Китае» → «В пути» каждые 30 минут ---
async def auto_transit_loop():
    while True:
        try:
            moved = auto_move_china_to_transit()
            if moved:
                logger.info("Auto-moved %s tracks to transit", moved)
        except Exception:
            logger.exception("auto_transit_loop error")
        await asyncio.sleep(30 * 60)


async def main():
    init_db()
    # Сразу прогнать автопереход при старте
    moved = auto_move_china_to_transit()
    if moved:
        logger.info("Startup auto-move: %s tracks", moved)

    print("🚀 Боты Cargo Line TJ запущены!")
    asyncio.create_task(auto_transit_loop())
    await asyncio.gather(
        dp_client.start_polling(client_bot),
        dp_admin.start_polling(admin_bot),
    )


if __name__ == "__main__":
    asyncio.run(main())
