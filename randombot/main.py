import asyncio
import logging
import random
import json
import os
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    PreCheckoutQuery, LabeledPrice, WebAppInfo
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from dotenv import load_dotenv
import aiosqlite

try:
    import asyncpg
except ImportError:  # pragma: no cover - dependency may be absent in some environments
    asyncpg = None

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = os.getenv("DB_PATH") or os.getenv("DATABASE_PATH") or "bot.db"
DB_BACKEND = "postgres" if DATABASE_URL and asyncpg else "sqlite"
DB_DIR = str(Path(DB_PATH).parent)
if DB_BACKEND == "sqlite" and DB_DIR and DB_DIR != ".":
    os.makedirs(DB_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

if not BOT_TOKEN:
    logging.warning("BOT_TOKEN is not set. The bot will run in a disabled mode until a token is provided.")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML")) if BOT_TOKEN else None
dp = Dispatcher()

RATE_LIMITS = defaultdict(deque)
PORT = int(os.getenv("PORT", "8000"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook").strip()
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
RAILWAY_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
if not WEBHOOK_URL and RAILWAY_DOMAIN:
    WEBHOOK_URL = f"https://{RAILWAY_DOMAIN}{WEBHOOK_PATH}"
if not WEBHOOK_URL:
    WEBHOOK_URL = f"https://example.com{WEBHOOK_PATH}"
STATIC_DIR = Path(__file__).parent / "miniapp"
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
if not MINI_APP_URL and RAILWAY_DOMAIN:
    MINI_APP_URL = f"https://{RAILWAY_DOMAIN}/"
if not MINI_APP_URL:
    MINI_APP_URL = "https://your-domain.example.com/"
PAYMENTS_PROVIDER = os.getenv("PAYMENTS_PROVIDER", "telegram_stars").strip().lower()
PAYMENTS_PROVIDER_TOKEN = os.getenv("PAYMENTS_PROVIDER_TOKEN", "").strip() or os.getenv("CRYPTO_PROVIDER_TOKEN", "").strip()
PAYMENTS_CURRENCY = os.getenv("PAYMENTS_CURRENCY", "XTR").strip().upper()
CRYPTO_BOT_URL = os.getenv("CRYPTO_BOT_URL", "https://t.me/CryptoBot").strip()

# ====================== БАЗА ДАННЫХ ======================
class SQLiteCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    async def fetchone(self):
        return await self._cursor.fetchone()

    async def fetchall(self):
        return await self._cursor.fetchall()


class PostgresCursor:
    def __init__(self, rows, last_row_id=None):
        self._rows = list(rows)
        self._index = 0
        self.lastrowid = last_row_id

    async def fetchone(self):
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    async def fetchall(self):
        rows = self._rows[self._index:]
        self._index = len(self._rows)
        return rows


class DbConnection:
    def __init__(self, conn, backend):
        self._conn = conn
        self._backend = backend

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def execute(self, query, params=()):
        if self._backend == "sqlite":
            cursor = await self._conn.execute(query, params)
            return SQLiteCursor(cursor)
        if self._backend == "postgres":
            if query.lstrip().upper().startswith("SELECT"):
                rows = await self._conn.fetch(query, *params)
                return PostgresCursor(rows)
            await self._conn.execute(query, *params)
            return PostgresCursor([], None)

    async def commit(self):
        if self._backend == "sqlite":
            await self._conn.commit()
        elif self._backend == "postgres":
            await self._conn.commit()

    async def close(self):
        if self._backend == "sqlite":
            await self._conn.close()
        elif self._backend == "postgres":
            await self._conn.close()

    async def insert_and_get_id(self, query, params=()):
        if self._backend == "sqlite":
            cursor = await self._conn.execute(query, params)
            await self._conn.commit()
            return cursor.lastrowid
        if self._backend == "postgres":
            return await self._conn.fetchval(query, *params)


async def get_db():
    if DB_BACKEND == "postgres":
        conn = await asyncpg.connect(DATABASE_URL)
        return DbConnection(conn, "postgres")
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    return DbConnection(conn, "sqlite")


async def ensure_column(table_name, column_name, column_def):
    async with await get_db() as db:
        if DB_BACKEND == "sqlite":
            cursor = await db.execute(f"PRAGMA table_info({table_name})")
            columns = {row[1] for row in await cursor.fetchall()}
            if column_name not in columns:
                await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")
                await db.commit()
        else:
            cursor = await db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
                (table_name,),
            )
            columns = {row[0] for row in await cursor.fetchall()}
            if column_name not in columns:
                await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")


async def init_db():
    async with await get_db() as db:
        if DB_BACKEND == "sqlite":
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("PRAGMA journal_mode = WAL")
            users_sql = """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                photo_url TEXT,
                is_premium INTEGER DEFAULT 0,
                free_giveaways_today INTEGER DEFAULT 0,
                last_free_date TEXT,
                is_banned INTEGER DEFAULT 0,
                security_flags INTEGER DEFAULT 0,
                creator_level INTEGER DEFAULT 0,
                registered_at TEXT
            )"""
            giveaways_sql = """
            CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER,
                text TEXT,
                entities_json TEXT,
                media_type TEXT,
                file_id TEXT,
                buttons_json TEXT,
                channel_id INTEGER,
                entry_fee_stars INTEGER DEFAULT 0,
                winners_count INTEGER DEFAULT 1,
                is_active INTEGER DEFAULT 1,
                created_at TEXT,
                participation_mode TEXT DEFAULT 'captcha',
                captcha_mode TEXT DEFAULT 'basic',
                fast_click_enabled INTEGER DEFAULT 0,
                fast_click_target INTEGER DEFAULT 10,
                fast_click_seconds INTEGER DEFAULT 10,
                is_featured INTEGER DEFAULT 0,
                boosted_until TEXT
            )"""
            participants_sql = """
            CREATE TABLE IF NOT EXISTS participants (
                giveaway_id INTEGER,
                user_id INTEGER,
                payment_id TEXT,
                joined_at TEXT,
                PRIMARY KEY (giveaway_id, user_id)
            )"""
            payments_sql = """
            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                user_id INTEGER,
                amount INTEGER,
                description TEXT,
                status TEXT,
                created_at TEXT
            )"""
            creator_features_sql = """
            CREATE TABLE IF NOT EXISTS creator_features (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                feature_code TEXT,
                purchased_at TEXT,
                expires_at TEXT,
                status TEXT DEFAULT 'active'
            )"""
            settings_sql = """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )"""
        else:
            users_sql = """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                photo_url TEXT,
                is_premium INTEGER DEFAULT 0,
                free_giveaways_today INTEGER DEFAULT 0,
                last_free_date TEXT,
                is_banned INTEGER DEFAULT 0,
                security_flags INTEGER DEFAULT 0,
                creator_level INTEGER DEFAULT 0,
                registered_at TEXT
            )"""
            giveaways_sql = """
            CREATE TABLE IF NOT EXISTS giveaways (
                id BIGSERIAL PRIMARY KEY,
                creator_id BIGINT,
                text TEXT,
                entities_json TEXT,
                media_type TEXT,
                file_id TEXT,
                buttons_json TEXT,
                channel_id BIGINT,
                entry_fee_stars INTEGER DEFAULT 0,
                winners_count INTEGER DEFAULT 1,
                is_active INTEGER DEFAULT 1,
                created_at TEXT,
                participation_mode TEXT DEFAULT 'captcha',
                captcha_mode TEXT DEFAULT 'basic',
                fast_click_enabled INTEGER DEFAULT 0,
                fast_click_target INTEGER DEFAULT 10,
                fast_click_seconds INTEGER DEFAULT 10,
                is_featured INTEGER DEFAULT 0,
                boosted_until TEXT
            )"""
            participants_sql = """
            CREATE TABLE IF NOT EXISTS participants (
                giveaway_id BIGINT,
                user_id BIGINT,
                payment_id TEXT,
                joined_at TEXT,
                PRIMARY KEY (giveaway_id, user_id)
            )"""
            payments_sql = """
            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                user_id BIGINT,
                amount INTEGER,
                description TEXT,
                status TEXT,
                created_at TEXT
            )"""
            creator_features_sql = """
            CREATE TABLE IF NOT EXISTS creator_features (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT,
                feature_code TEXT,
                purchased_at TEXT,
                expires_at TEXT,
                status TEXT DEFAULT 'active'
            )"""
            settings_sql = """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )"""

        await db.execute(users_sql)
        await db.execute(giveaways_sql)
        await db.execute(participants_sql)
        await db.execute(payments_sql)
        await db.execute(creator_features_sql)
        await db.execute(settings_sql)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_giveaways_creator ON giveaways(creator_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_participants_user ON participants(user_id)")
        await db.execute("INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING", ("commission_percent", "10"))
        await db.execute("INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING", ("premium_price_stars", "100"))
        await db.execute("INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING", ("security_mode", "strict"))
        await db.commit()

    await ensure_column("users", "full_name", "TEXT")
    await ensure_column("users", "photo_url", "TEXT")
    await ensure_column("users", "registered_at", "TEXT")
    await ensure_column("users", "is_banned", "INTEGER DEFAULT 0")
    await ensure_column("users", "security_flags", "INTEGER DEFAULT 0")
    await ensure_column("users", "creator_level", "INTEGER DEFAULT 0")
    await ensure_column("giveaways", "participation_mode", "TEXT DEFAULT 'captcha'")
    await ensure_column("giveaways", "captcha_mode", "TEXT DEFAULT 'basic'")
    await ensure_column("giveaways", "fast_click_enabled", "INTEGER DEFAULT 0")
    await ensure_column("giveaways", "fast_click_target", "INTEGER DEFAULT 10")
    await ensure_column("giveaways", "fast_click_seconds", "INTEGER DEFAULT 10")
    await ensure_column("giveaways", "is_featured", "INTEGER DEFAULT 0")
    await ensure_column("giveaways", "boosted_until", "TEXT")

# ====================== FSM ======================
class GiveawayCreation(StatesGroup):
    text = State()
    media = State()
    add_buttons_question = State()
    button_label = State()
    button_url_or_type = State()
    button_color = State()
    another_button = State()
    channel = State()
    fee = State()
    captcha_type = State()
    confirm = State()

class Captcha(StatesGroup):
    waiting_answer = State()

class AdminMenu(StatesGroup):
    main = State()
    broadcast = State()
    settings_commission = State()
    settings_premium_price = State()

# ====================== КЛАВИАТУРЫ ======================
def simple_keyboard(buttons: list):
    builder = InlineKeyboardBuilder()
    for text, callback in buttons:
        builder.add(InlineKeyboardButton(text=text, callback_data=callback))
    return builder.as_markup()

def cancel_back_kb(back_cb=None):
    kb = InlineKeyboardBuilder()
    if back_cb:
        kb.add(InlineKeyboardButton(text="🔙 Назад", callback_data=back_cb))
    kb.add(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel"))
    return kb.as_markup()


def build_admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🧑‍💻 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="🛡 Защита", callback_data="admin_security")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin_settings")],
        [InlineKeyboardButton(text="🔚 Закрыть розыгрыш", callback_data="admin_close_giveaway")],
    ])


color_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="⬜ По умолчанию", callback_data="color_default")],
    [InlineKeyboardButton(text="🔵 Primary", callback_data="color_primary")],
    [InlineKeyboardButton(text="🟢 Positive", callback_data="color_positive")],
    [InlineKeyboardButton(text="🔴 Negative", callback_data="color_negative")],
])

# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
async def check_user_limit(user_id):
    """Бесплатные розыгрыши: 3 в день."""
    async with await get_db() as db:
        row = await db.execute("SELECT free_giveaways_today, last_free_date FROM users WHERE user_id=?", (user_id,))
        user = await row.fetchone()
        today = str(date.today())
        if not user:
            await db.execute("INSERT INTO users (user_id, free_giveaways_today, last_free_date) VALUES (?, 0, ?)",
                             (user_id, today))
            await db.commit()
            return True
        if user[1] != today:
            await db.execute("UPDATE users SET free_giveaways_today=0, last_free_date=? WHERE user_id=?",
                             (today, user_id))
            await db.commit()
            return True
        return user[0] < 3  # лимит 3 бесплатных

async def increment_user_limit(user_id):
    async with await get_db() as db:
        await db.execute("UPDATE users SET free_giveaways_today = free_giveaways_today + 1 WHERE user_id=?", (user_id,))
        await db.commit()


async def ensure_user_profile(user_id: int, username: Optional[str] = None, full_name: Optional[str] = None, photo_url: Optional[str] = None):
    async with await get_db() as db:
        row = await db.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
        exists = await row.fetchone()
        now = datetime.now().isoformat()
        if exists:
            await db.execute(
                "UPDATE users SET username=?, full_name=?, photo_url=?, registered_at=COALESCE(registered_at, ?) WHERE user_id=?",
                (username or None, full_name or None, photo_url or None, now, user_id),
            )
        else:
            await db.execute(
                "INSERT INTO users (user_id, username, full_name, photo_url, registered_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, username or None, full_name or None, photo_url or None, now),
            )
        await db.commit()


async def get_commission():
    async with await get_db() as db:
        row = await db.execute("SELECT value FROM settings WHERE key='commission_percent'")
        return int((await row.fetchone())[0])


async def set_setting(key: str, value: str):
    async with await get_db() as db:
        await db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await db.commit()


async def get_setting(key: str, default: str = "") -> str:
    async with await get_db() as db:
        row = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        value = await row.fetchone()
        return (value[0] if value else default).strip()


async def save_payment(payment_id: str, user_id: int, amount: int, description: str, status: str = "completed"):
    async with await get_db() as db:
        await db.execute(
            "INSERT INTO payments (payment_id, user_id, amount, description, status, created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(payment_id) DO UPDATE SET user_id=excluded.user_id, amount=excluded.amount, description=excluded.description, status=excluded.status",
            (payment_id, user_id, amount, description, status, datetime.now().isoformat()),
        )
        await db.commit()


async def store_participant(giveaway_id: int, user_id: int, payment_id: Optional[str] = None) -> bool:
    async with await get_db() as db:
        existing = await db.execute("SELECT 1 FROM participants WHERE giveaway_id=? AND user_id=?", (giveaway_id, user_id))
        if await existing.fetchone():
            return False
        await db.execute(
            "INSERT INTO participants (giveaway_id, user_id, payment_id, joined_at) VALUES (?, ?, ?, ?)",
            (giveaway_id, user_id, payment_id, datetime.now().isoformat()),
        )
        await db.commit()
        return True


async def get_security_mode() -> str:
    async with await get_db() as db:
        row = await db.execute("SELECT value FROM settings WHERE key='security_mode'")
        value = await row.fetchone()
        return (value[0] if value else "strict").strip()


async def generate_captcha():
    a = random.randint(1, 10)
    b = random.randint(1, 10)
    op = random.choice(["+", "-", "*"])
    if op == "+":
        answer = a + b
    elif op == "-":
        answer = a - b
    else:
        answer = a * b
    question = f"{a} {op} {b} = ?"
    return question, answer


async def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    bucket = RATE_LIMITS[user_id]
    while bucket and now - bucket[0] > 5:
        bucket.popleft()
    if len(bucket) >= 8:
        return True
    bucket.append(now)
    return False


async def register_security_event(user_id: int, reason: str):
    async with await get_db() as db:
        await db.execute("UPDATE users SET security_flags = security_flags + 1 WHERE user_id=?", (user_id,))
        await db.commit()
    logging.warning("Security event for %s: %s", user_id, reason)


async def is_user_suspended(user_id: int) -> bool:
    async with await get_db() as db:
        row = await db.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,))
        user = await row.fetchone()
        return bool(user and user[0])


async def get_creator_feature(user_id: int, feature_code: str) -> bool:
    async with await get_db() as db:
        row = await db.execute(
            "SELECT 1 FROM creator_features WHERE user_id=? AND feature_code=? AND status='active' AND (expires_at IS NULL OR expires_at > ?)",
            (user_id, feature_code, datetime.now().isoformat()),
        )
        return bool(await row.fetchone())


def get_invoice_config() -> tuple[str, str]:
    provider_token = ""
    currency = PAYMENTS_CURRENCY
    if PAYMENTS_PROVIDER in {"crypto_bot", "crypto", "cryptobot"}:
        provider_token = PAYMENTS_PROVIDER_TOKEN
        if not provider_token:
            currency = "XTR"
    elif PAYMENTS_PROVIDER in {"telegram_stars", "stars", "telegram"}:
        provider_token = ""
        currency = "XTR"
    else:
        provider_token = PAYMENTS_PROVIDER_TOKEN
    return provider_token, currency


async def buy_creator_feature(message: Message, feature_code: str, price: int, title: str, description: str):
    if not bot:
        await message.answer("⚠️ Платежи временно недоступны: токен бота не настроен.")
        return
    provider_token, currency = get_invoice_config()
    if PAYMENTS_PROVIDER in {"crypto_bot", "crypto", "cryptobot"} and not provider_token:
        await message.answer(
            "🪙 Для крипто-платежей задайте CRYPTO_PROVIDER_TOKEN или PAYMENTS_PROVIDER_TOKEN и повторите попытку."
        )
        return
    await bot.send_invoice(
        chat_id=message.chat.id,
        title=title,
        description=description,
        payload=f"feature_{feature_code}",
        provider_token=provider_token,
        currency=currency,
        prices=[LabeledPrice(label=title, amount=price)],
    )


async def send_purchase_invoice(chat_id: int, payload: str, amount: int, title: str, description: str):
    if not bot:
        return {"ok": False, "reason": "bot_not_configured"}
    provider_token, currency = get_invoice_config()
    if PAYMENTS_PROVIDER in {"crypto_bot", "crypto", "cryptobot"} and not provider_token:
        return {"ok": False, "reason": "provider_token_missing"}
    await bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=payload,
        provider_token=provider_token,
        currency=currency,
        prices=[LabeledPrice(label=title, amount=amount)],
    )
    return {"ok": True}


# ====================== ОБРАБОТЧИКИ ПОЛЬЗОВАТЕЛЯ ======================
@dp.message(Command("start"))
async def start(message: Message):
    if await is_user_suspended(message.from_user.id):
        await message.answer("🚫 Ваш доступ ограничен.")
        return
    await ensure_user_profile(
        message.from_user.id,
        username=message.from_user.username,
        full_name=" ".join(filter(None, [message.from_user.first_name, message.from_user.last_name])),
        photo_url=getattr(message.from_user, "photo_url", None),
    )
    await message.answer(
        "🎉 Привет! Я бот для ярких и защищённых розыгрышей ✨\n"
        "🛡️ Здесь есть премиум-капча, Fast Click, Boost и красивое мини-приложение.\n"
        "🟢 /new_giveaway — создать розыгрыш\n"
        "💎 /buy_premium — открыть премиум-возможности\n"
        "🧰 /creator_tools — расширенные функции для создателей"
    )


@dp.message(Command("creator_tools"))
async def creator_tools(message: Message):
    if await is_user_suspended(message.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Премиум функции", callback_data="creator_premium")],
        [InlineKeyboardButton(text="⚡ Fast Click", callback_data="creator_fast_click")],
        [InlineKeyboardButton(text="💳 Оплата", callback_data="creator_payments")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="creator_stats")],
        [InlineKeyboardButton(text="📱 Мини-приложение", web_app=WebAppInfo(url=MINI_APP_URL))],
    ])
    await message.answer("🛠 Инструменты для создателей розыгрышей ✨", reply_markup=kb)


@dp.callback_query(F.data == "creator_premium")
async def creator_premium(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Boost розыгрыша (100 ⭐)", callback_data="buy_boost")],
        [InlineKeyboardButton(text="🌟 Featured публикация (150 ⭐)", callback_data="buy_featured")],
        [InlineKeyboardButton(text="🟢 Премиум-капча (120 ⭐)", callback_data="buy_premium_captcha")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="creator_tools")],
    ])
    await callback.message.edit_text("💎 Выберите платную функцию для создателя ✨", reply_markup=kb)


@dp.callback_query(F.data == "creator_fast_click")
async def creator_fast_click(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Включить Fast Click (80 ⭐)", callback_data="buy_fast_click")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="creator_tools")],
    ])
    await callback.message.edit_text("⚡ Fast Click — режим молниеносного участия для самых активных ✨", reply_markup=kb)


@dp.callback_query(F.data == "creator_payments")
async def creator_payments(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="pay_stars")],
        [InlineKeyboardButton(text="🪙 CryptoBot", callback_data="pay_crypto")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="creator_tools")],
    ])
    await callback.message.edit_text("💳 Выберите способ оплаты для премиум-функций ✨", reply_markup=kb)


@dp.callback_query(F.data == "creator_stats")
async def creator_stats(callback: CallbackQuery):
    async with await get_db() as db:
        users = await db.execute("SELECT COUNT(*) FROM users")
        giveaways = await db.execute("SELECT COUNT(*) FROM giveaways")
        payments = await db.execute("SELECT COUNT(*) FROM payments")
        u = (await users.fetchone())[0]
        g = (await giveaways.fetchone())[0]
        p = (await payments.fetchone())[0]
    await callback.message.edit_text(
        f"📊 Статистика платформы\n\n👥 Пользователи: {u}\n🎁 Розыгрыши: {g}\n💳 Платежи: {p}\n\n✨ Всё готово к росту и монетизации",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="creator_tools")]]),
    )


@dp.callback_query(F.data == "pay_stars")
async def pay_stars(callback: CallbackQuery):
    await buy_creator_feature(callback.message, "stars", 120, "Telegram Stars", "Оплата через Telegram Stars")


@dp.callback_query(F.data == "buy_premium_captcha")
async def buy_premium_captcha(callback: CallbackQuery):
    await buy_creator_feature(callback.message, "premium_captcha", 120, "Premium капча", "Премиум-защита для участия")


@dp.callback_query(F.data == "pay_crypto")
async def pay_crypto(callback: CallbackQuery):
    await callback.message.answer("🪙 Для крипто-платежей откройте мини-приложение и выберите CryptoBot в панели оплаты ✨")


@dp.callback_query(F.data == "cancel")
async def cancel_flow(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("❌ Действие отменено.")

@dp.message(Command("new_giveaway"))
async def new_giveaway(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if await is_user_suspended(user_id):
        await message.answer("🚫 Ваш доступ ограничен.")
        return
    if await is_rate_limited(user_id):
        await register_security_event(user_id, "rate_limit")
        await message.answer("⏳ Слишком много действий. Подождите немного.")
        return
    if not await check_user_limit(user_id):
        await message.answer("❌ Вы исчерпали лимит бесплатных розыгрышей на сегодня (3). "
                             "Купите премиум или подождите завтра.")
        return
    await state.update_data(creator_id=user_id, entities=[], buttons=[])
    await state.set_state(GiveawayCreation.text)
    await message.answer(
        "📝 <b>Шаг 1/6:</b> Введите текст розыгрыша. Можно использовать <b>премиум-эмодзи</b>.\n"
        "Бот сохранит все оформление.",
        reply_markup=cancel_back_kb()
    )

@dp.message(GiveawayCreation.text)
async def text_step(message: Message, state: FSMContext):
    if await is_rate_limited(message.from_user.id):
        await register_security_event(message.from_user.id, "text_rate_limit")
        await message.answer("⏳ Слишком много действий. Подождите немного.")
        return
    entities = message.entities or []
    await state.update_data(text=message.text, entities=entities)
    await state.set_state(GiveawayCreation.media)
    await message.answer(
        "🖼 <b>Шаг 2/6:</b> Пришлите 1 фото/видео/GIF или нажмите «Пропустить».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить ➡️", callback_data="skip_media")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_text")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
        ])
    )

@dp.callback_query(F.data == "skip_media")
async def skip_media(callback: CallbackQuery, state: FSMContext):
    await state.update_data(media_type="text_only", file_id=None)
    await add_buttons_question(callback.message, state)

@dp.callback_query(F.data == "back_to_text")
async def back_to_text(callback: CallbackQuery, state: FSMContext):
    await state.set_state(GiveawayCreation.text)
    await callback.message.edit_text("Введите текст заново:", reply_markup=cancel_back_kb())


@dp.callback_query(F.data == "back_to_buttons_question")
async def back_to_buttons_question(callback: CallbackQuery, state: FSMContext):
    await state.set_state(GiveawayCreation.add_buttons_question)
    await callback.message.edit_text(
        "🔘 <b>Шаг 3/6:</b> Хотите добавить кнопки в пост?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="add_button")],
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_buttons")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_media")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
        ])
    )


@dp.callback_query(F.data == "back_to_media")
async def back_to_media(callback: CallbackQuery, state: FSMContext):
    await state.set_state(GiveawayCreation.media)
    await callback.message.edit_text(
        "🖼 <b>Шаг 2/6:</b> Пришлите 1 фото/видео/GIF или нажмите «Пропустить».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить ➡️", callback_data="skip_media")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_text")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
        ])
    )


@dp.callback_query(F.data == "back_to_channel")
async def back_to_channel(callback: CallbackQuery, state: FSMContext):
    await state.set_state(GiveawayCreation.channel)
    await callback.message.edit_text("📢 <b>Шаг 4/6:</b> Выберите канал:", reply_markup=cancel_back_kb("back_to_buttons_question"))


@dp.callback_query(F.data == "back_to_fee")
async def back_to_fee(callback: CallbackQuery, state: FSMContext):
    await state.set_state(GiveawayCreation.fee)
    await callback.message.edit_text(
        "💰 <b>Шаг 5/6:</b> Установите плату за вход (в звёздах). 0 — бесплатно.",
        reply_markup=cancel_back_kb("back_to_channel")
    )

@dp.message(GiveawayCreation.media, F.photo)
async def get_photo(message: Message, state: FSMContext):
    await state.update_data(media_type="photo", file_id=message.photo[-1].file_id)
    await add_buttons_question(message, state)

@dp.message(GiveawayCreation.media, F.video)
async def get_video(message: Message, state: FSMContext):
    await state.update_data(media_type="video", file_id=message.video.file_id)
    await add_buttons_question(message, state)

@dp.message(GiveawayCreation.media, F.animation)
async def get_animation(message: Message, state: FSMContext):
    await state.update_data(media_type="animation", file_id=message.animation.file_id)
    await add_buttons_question(message, state)

@dp.message(GiveawayCreation.media)
async def media_invalid(message: Message):
    await message.answer("❌ Отправьте ровно 1 медиафайл или пропустите.")


@dp.message(F.web_app_data)
async def mini_app_data(message: Message):
    try:
        payload = json.loads(message.web_app_data.data)
    except Exception:
        await message.answer("📱 Мини-приложение отправило данные, но формат был некорректен.")
        return

    action = payload.get("action")
    if action == "register_profile":
        user = payload.get("user", {})
        await ensure_user_profile(
            message.from_user.id,
            username=user.get("username"),
            full_name=" ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or None,
            photo_url=user.get("photo_url"),
        )
        await message.answer("✅ Профиль успешно зарегистрирован в мини-приложении ✨")
        return

    if action == "buy_boost":
        await buy_creator_feature(message, "boost", 100, "Boost розыгрыша", "Поднятие розыгрыша в топ")
        return

    if action == "buy_featured":
        await buy_creator_feature(message, "featured", 150, "Featured публикация", "Премиум-выделение вашего розыгрыша")
        return

    if action in {"pay_stars", "pay_crypto"}:
        plan = payload.get("plan") or "premium"
        source = payload.get("source") or "miniapp"
        if action == "pay_stars":
            await send_purchase_invoice(
                message.chat.id,
                f"feature_{plan}",
                120 if plan == "premium_captcha" else 100 if plan == "boost" else 150,
                "Telegram Stars",
                f"Премиум-функция: {plan} • Источник: {source}",
            )
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🪙 Открыть CryptoBot", url=CRYPTO_BOT_URL)],
            ])
            if PAYMENTS_PROVIDER in {"crypto_bot", "crypto", "cryptobot"} and PAYMENTS_PROVIDER_TOKEN:
                await send_purchase_invoice(
                    message.chat.id,
                    f"feature_{plan}",
                    120 if plan == "premium_captcha" else 100 if plan == "boost" else 150,
                    "CryptoBot payment",
                    f"Премиум-функция: {plan} • Источник: {source}",
                )
                return
            await message.answer(
                f"🪙 Готово к оплате через CryptoBot для плана {plan}. Откройте бота и завершите оплату.",
                reply_markup=kb,
            )
        return

    if action == "request_stats":
        async with await get_db() as db:
            users = await db.execute("SELECT COUNT(*) FROM users")
            giveaways = await db.execute("SELECT COUNT(*) FROM giveaways WHERE is_active=1")
            payments = await db.execute("SELECT COUNT(*) FROM payments")
            u = (await users.fetchone())[0]
            g = (await giveaways.fetchone())[0]
            p = (await payments.fetchone())[0]
        await message.answer(f"📊 Статистика:\nПользователи: {u}\nАктивных розыгрышей: {g}\nПлатежей: {p}")
        return

    await message.answer("📱 Мини-приложение готово. Откройте его для быстрого управления розыгрышами и функциями создателя ✨")

async def add_buttons_question(message: Message, state: FSMContext):
    await state.set_state(GiveawayCreation.add_buttons_question)
    await message.answer(
        "🔘 <b>Шаг 3/6:</b> Хотите добавить кнопки в пост?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="add_button")],
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_buttons")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_media")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
        ])
    )

@dp.callback_query(F.data == "skip_buttons")
async def skip_buttons(callback: CallbackQuery, state: FSMContext):
    await choose_channel_step(callback.message, state)


@dp.callback_query(F.data == "buy_boost")
async def buy_boost(callback: CallbackQuery):
    await buy_creator_feature(callback.message, "boost", 100, "Boost розыгрыша", "Поднятие розыгрыша в топ")


@dp.callback_query(F.data == "buy_featured")
async def buy_featured(callback: CallbackQuery):
    await buy_creator_feature(callback.message, "featured", 150, "Featured публикация", "Премиум-выделение вашего розыгрыша")


@dp.callback_query(F.data == "buy_fast_click")
async def buy_fast_click(callback: CallbackQuery):
    await buy_creator_feature(callback.message, "fast_click", 80, "Fast Click", "Подключение быстрого режима участия")

@dp.callback_query(F.data == "add_button")
async def add_button_label(callback: CallbackQuery, state: FSMContext):
    await state.set_state(GiveawayCreation.button_label)
    await callback.message.answer(
        "Введите текст кнопки (можно премиум-эмодзи).",
        reply_markup=cancel_back_kb("back_to_buttons_question")
    )

@dp.message(GiveawayCreation.button_label)
async def button_label_input(message: Message, state: FSMContext):
    # Сохраняем текст и entities кнопки
    entities = message.entities or []
    label_text = message.text
    await state.update_data(btn_label=label_text, btn_entities=entities)
    await state.set_state(GiveawayCreation.button_url_or_type)
    await message.answer(
        "Тип кнопки:\n"
        "- <b>URL</b> (внешняя ссылка)\n"
        "- <b>Участвовать</b> (запуск капчи)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌐 URL", callback_data="btn_type_url")],
            [InlineKeyboardButton(text="🎟 Участвовать", callback_data="btn_type_participate")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_label")],
        ])
    )

@dp.callback_query(F.data == "btn_type_url")
async def btn_url_ask(callback: CallbackQuery, state: FSMContext):
    await state.set_state(GiveawayCreation.button_url_or_type)
    await callback.message.answer("Введите ссылку (https://...):", reply_markup=cancel_back_kb())

@dp.callback_query(F.data == "btn_type_participate")
async def btn_participate_set(callback: CallbackQuery, state: FSMContext):
    # Сохраняем тип как participate
    await state.update_data(btn_type="participate", btn_url=None)
    await choose_color(callback.message, state)

@dp.message(GiveawayCreation.button_url_or_type)
async def button_url_input(message: Message, state: FSMContext):
    url = message.text.strip()
    if not url.startswith("http"):
        await message.answer("Некорректная ссылка. Попробуйте ещё.")
        return
    await state.update_data(btn_type="url", btn_url=url)
    await choose_color(message, state)

async def choose_color(message: Message, state: FSMContext):
    await state.set_state(GiveawayCreation.button_color)
    await message.answer("🎨 Выберите цвет кнопки:", reply_markup=color_kb)

@dp.callback_query(F.data.startswith("color_"))
async def color_chosen(callback: CallbackQuery, state: FSMContext):
    color = callback.data.split("_")[1]  # default, primary, positive, negative
    data = await state.get_data()
    btn_label = data["btn_label"]
    btn_entities = data.get("btn_entities", [])
    btn_type = data["btn_type"]
    btn_url = data.get("btn_url")
    buttons = data.get("buttons", [])
    buttons.append({
        "label": btn_label,
        "entities": btn_entities,
        "type": btn_type,
        "url": btn_url,
        "color": color
    })
    await state.update_data(buttons=buttons)
    # Очищаем временные поля кнопки
    for key in ["btn_label", "btn_entities", "btn_type", "btn_url"]:
        await state.update_data({key: None})
    # Спрашиваем, добавить ещё?
    await state.set_state(GiveawayCreation.another_button)
    await callback.message.answer("Кнопка добавлена! Ещё одну?",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                      [InlineKeyboardButton(text="➕ Да", callback_data="add_button")],
                                      [InlineKeyboardButton(text="➡️ Завершить", callback_data="finish_buttons")],
                                      [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_buttons_question")],
                                  ]))

@dp.callback_query(F.data == "finish_buttons")
async def finish_buttons(callback: CallbackQuery, state: FSMContext):
    await choose_channel_step(callback.message, state)

async def choose_channel_step(message: Message, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_buttons_question"))
    kb.add(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel"))
    kb.adjust(1)
    await state.set_state(GiveawayCreation.channel)
    await message.answer(
        "📢 <b>Шаг 4/6:</b> Введите ID канала, в который хотите опубликовать розыгрыш.\n\n"
        "Канальные ID — отрицательные числа вида <code>-1001234567890</code>.\n"
        "Чтобы узнать ID канала, перешлите любое сообщение из него боту @username_to_id_bot "
        "или добавьте бота в канал как администратора.",
        reply_markup=kb.as_markup()
    )

@dp.callback_query(GiveawayCreation.channel, F.data.startswith("chosen_"))
async def channel_chosen(callback: CallbackQuery, state: FSMContext):
    channel_id = int(callback.data.split("_")[1])
    await state.update_data(channel_id=channel_id)
    await state.set_state(GiveawayCreation.fee)
    await callback.message.answer(
        "💰 <b>Шаг 5/6:</b> Установите плату за вход (в звёздах). 0 — бесплатно.",
        reply_markup=cancel_back_kb("back_to_channel")
    )

@dp.message(GiveawayCreation.channel)
async def channel_id_input(message: Message, state: FSMContext):
    raw = message.text.strip() if message.text else ""
    # Channel IDs are negative integers, typically starting with -100
    if not raw.lstrip("-").isdigit():
        await message.answer(
            "❌ Некорректный ID канала. Введите отрицательное число, например: <code>-1001234567890</code>",
            reply_markup=cancel_back_kb("back_to_buttons_question")
        )
        return
    channel_id = int(raw)
    if channel_id >= 0:
        await message.answer(
            "❌ ID канала должен быть отрицательным числом, например: <code>-1001234567890</code>",
            reply_markup=cancel_back_kb("back_to_buttons_question")
        )
        return
    await state.update_data(channel_id=channel_id)
    await state.set_state(GiveawayCreation.fee)
    await message.answer(
        f"✅ Канал <code>{channel_id}</code> выбран.\n\n"
        "💰 <b>Шаг 5/6:</b> Установите плату за вход (в звёздах). 0 — бесплатно.",
        reply_markup=cancel_back_kb("back_to_channel")
    )



@dp.message(GiveawayCreation.fee)
async def fee_input(message: Message, state: FSMContext):
    try:
        fee = int(message.text)
        if fee < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите целое неотрицательное число.")
        return
    await state.update_data(entry_fee=fee)
    await state.set_state(GiveawayCreation.captcha_type)
    await message.answer(
        "🛡 Выберите тип капчи для участия:\n• Обычная — бесплатно\n• Премиум — платная защита",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Обычная", callback_data="captcha_basic")],
            [InlineKeyboardButton(text="💎 Премиум", callback_data="captcha_premium")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_fee")],
        ])
    )


@dp.callback_query(GiveawayCreation.captcha_type, F.data == "captcha_basic")
async def captcha_basic_selected(callback: CallbackQuery, state: FSMContext):
    await state.update_data(captcha_mode="basic")
    await show_preview(callback.message, state)


@dp.callback_query(GiveawayCreation.captcha_type, F.data == "captcha_premium")
async def captcha_premium_selected(callback: CallbackQuery, state: FSMContext):
    creator_id = (await state.get_data()).get("creator_id")
    feature_ok = await get_creator_feature(creator_id, "premium_captcha")
    if not feature_ok:
        await buy_creator_feature(callback.message, "premium_captcha", 120, "Premium капча", "Премиум-защита для участия")
        return
    await state.update_data(captcha_mode="premium")
    await show_preview(callback.message, state)

async def show_preview(message: Message, state: FSMContext):
    data = await state.get_data()
    text = data["text"]
    entities = data.get("entities", [])
    media_type = data.get("media_type", "text_only")
    file_id = data.get("file_id")
    buttons = data.get("buttons", [])
    channel_id = data.get("channel_id")
    fee = data.get("entry_fee", 0)
    captcha_mode = data.get("captcha_mode", "basic")

    # Собираем клавиатуру для превью
    preview_kb = InlineKeyboardBuilder()
    for btn in buttons:
        label = btn["label"]
        btn_entities = btn.get("entities", [])
        if btn["type"] == "url":
            preview_kb.add(InlineKeyboardButton(text=label, url=btn["url"]))
        else:
            preview_kb.add(InlineKeyboardButton(text=label, callback_data="participate_dummy"))
    # Показываем сообщение пользователю
    caption = f"{text}\n\n<i>Вход: {fee} ⭐</i>\n<i>Капча: {('Премиум' if captcha_mode == 'premium' else 'Обычная')}</i>"
    await state.set_state(GiveawayCreation.confirm)
    if media_type == "photo":
        await bot.send_photo(
            chat_id=message.chat.id,
            photo=file_id,
            caption=caption,
            caption_entities=entities,
            reply_markup=preview_kb.as_markup() if buttons else None,
        )
    elif media_type == "video":
        await bot.send_video(
            chat_id=message.chat.id,
            video=file_id,
            caption=caption,
            caption_entities=entities,
            reply_markup=preview_kb.as_markup() if buttons else None,
        )
    elif media_type == "animation":
        await bot.send_animation(
            chat_id=message.chat.id,
            animation=file_id,
            caption=caption,
            caption_entities=entities,
            reply_markup=preview_kb.as_markup() if buttons else None,
        )
    else:
        await bot.send_message(
            chat_id=message.chat.id,
            text=caption,
            entities=entities,
            reply_markup=preview_kb.as_markup() if buttons else None,
        )
    await message.answer("✅ <b>Шаг 6/6:</b> Подтвердите публикацию.",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="✅ Опубликовать", callback_data="confirm_publish")],
                             [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_fee")],
                         ]))

@dp.callback_query(GiveawayCreation.confirm, F.data == "confirm_publish")
async def publish_giveaway(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = data["creator_id"]
    # Проверка лимита
    if not await check_user_limit(user_id):
        await callback.answer("Лимит исчерпан", show_alert=True)
        return
    # Сохраняем в БД
    async with await get_db() as db:
        cursor = await db.execute("INSERT INTO giveaways (creator_id, text, entities_json, media_type, file_id, "
                                  "buttons_json, channel_id, entry_fee_stars, captcha_mode, is_active, created_at) "
                                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                                  (user_id, data["text"],
                                   json.dumps([e.model_dump() if hasattr(e, "model_dump") else e for e in data.get("entities", [])]),
                                   data["media_type"], data.get("file_id"),
                                   json.dumps([
                                       {**{k: v for k, v in btn.items() if k != "entities"},
                                        "entities": [e.model_dump() if hasattr(e, "model_dump") else e for e in btn.get("entities", [])]}
                                       for btn in data.get("buttons", [])
                                   ]),
                                   data["channel_id"], data["entry_fee"], data.get("captcha_mode", "basic"),
                                   datetime.now().isoformat()))
        giveaway_id = cursor.lastrowid
        await db.commit()

    await post_to_channel(giveaway_id, data["channel_id"], data["text"], data.get("entities", []),
                          data["media_type"], data.get("file_id"), data.get("buttons", []),
                          data["entry_fee"], data.get("captcha_mode", "basic"))
    await increment_user_limit(user_id)
    await callback.message.answer("🎉 Розыгрыш опубликован!")
    await state.clear()

async def post_to_channel(giveaway_id, channel_id, text, entities, media_type, file_id, buttons, fee, captcha_mode="basic"):
    # Строим настоящую клавиатуру с callback_data для участия
    kb = InlineKeyboardBuilder()
    for btn in buttons:
        if btn["type"] == "participate":
            cb_data = f"join_{giveaway_id}"
        else:
            kb.add(InlineKeyboardButton(text=btn["label"], url=btn["url"]))
            continue
        kb.add(InlineKeyboardButton(
            text=btn["label"],
            callback_data=cb_data
        ))
    caption = f"{text}\n\n💰 Вход: {fee} ⭐\n🛡 Капча: {('Премиум' if captcha_mode == 'premium' else 'Обычная')}" if fee else f"{text}\n\n🛡 Капча: {('Премиум' if captcha_mode == 'premium' else 'Обычная')}"
    try:
        if media_type == "photo":
            await bot.send_photo(
                channel_id,
                file_id,
                caption=caption,
                caption_entities=entities,
                reply_markup=kb.as_markup() if buttons else None,
            )
        elif media_type == "video":
            await bot.send_video(
                channel_id,
                file_id,
                caption=caption,
                caption_entities=entities,
                reply_markup=kb.as_markup() if buttons else None,
            )
        elif media_type == "animation":
            await bot.send_animation(
                channel_id,
                file_id,
                caption=caption,
                caption_entities=entities,
                reply_markup=kb.as_markup() if buttons else None,
            )
        else:
            await bot.send_message(
                channel_id,
                caption,
                entities=entities,
                reply_markup=kb.as_markup() if buttons else None,
            )
    except Exception as e:
        logging.error(f"Post to channel {channel_id} failed: {e}")

# ====================== УЧАСТИЕ + КАПЧА ======================
@dp.callback_query(F.data.startswith("join_"))
async def join_giveaway(callback: CallbackQuery, state: FSMContext):
    giveaway_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    if await is_user_suspended(user_id):
        await callback.answer("🚫 Доступ ограничен", show_alert=True)
        return
    if await is_rate_limited(user_id):
        await register_security_event(user_id, "join_rate_limit")
        await callback.answer("⏳ Слишком много попыток. Попробуйте позже.", show_alert=True)
        return
    # Проверяем, не участвовал ли уже
    async with await get_db() as db:
        row = await db.execute("SELECT entry_fee_stars, is_active FROM giveaways WHERE id=?", (giveaway_id,))
        giveaway = await row.fetchone()
        if not giveaway or not giveaway[1]:
            await callback.answer("Розыгрыш завершён.", show_alert=True)
            return
        fee = giveaway[0]
        if fee > 0:
            await send_invoice(callback.message, giveaway_id, fee)
            return
        row = await db.execute("SELECT participation_mode, fast_click_enabled, fast_click_target, fast_click_seconds, captcha_mode FROM giveaways WHERE id=?", (giveaway_id,))
        giveaway_meta = await row.fetchone()
        if giveaway_meta and giveaway_meta[1]:
            await start_fast_click(callback.message, state, giveaway_id, giveaway_meta[2], giveaway_meta[3])
            return
        if giveaway_meta and giveaway_meta[4] == "premium":
            await start_premium_captcha(callback.message, state, giveaway_id)
            return
        await start_captcha(callback.message, state, giveaway_id)

async def send_invoice(message: Message, giveaway_id: int, amount: int):
    provider_token, currency = get_invoice_config()
    if PAYMENTS_PROVIDER in {"crypto_bot", "crypto", "cryptobot"} and not provider_token:
        await message.answer("🪙 Для крипто-платежей задайте CRYPTO_PROVIDER_TOKEN или PAYMENTS_PROVIDER_TOKEN.")
        return
    prices = [LabeledPrice(label="Участие в розыгрыше", amount=amount)]
    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Билет на розыгрыш",
        description=f"Участие в розыгрыше #{giveaway_id}",
        payload=f"giveaway_{giveaway_id}",
        provider_token=provider_token,
        currency=currency,
        prices=prices,
    )

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)

@dp.message(F.successful_payment)
async def process_payment(message: Message, state: FSMContext):
    payload = message.successful_payment.invoice_payload
    if payload.startswith("giveaway_"):
        giveaway_id = int(payload.split("_")[1])
        await save_payment(
            message.successful_payment.telegram_payment_charge_id or f"giveaway_{giveaway_id}_{message.from_user.id}",
            message.from_user.id,
            message.successful_payment.total_amount,
            f"Participation #{giveaway_id}",
            "completed",
        )
        await store_participant(giveaway_id, message.from_user.id, message.successful_payment.telegram_payment_charge_id)
        await message.answer("✅ Вы успешно вошли в розыгрыш!")
        return
    if payload == "premium_sub":
        async with await get_db() as db:
            await db.execute("UPDATE users SET is_premium=1 WHERE user_id=?", (message.from_user.id,))
            await db.commit()
        await save_payment(message.successful_payment.telegram_payment_charge_id or f"premium_{message.from_user.id}", message.from_user.id, message.successful_payment.total_amount, "Premium subscription", "completed")
        await message.answer("🌟 Спасибо! Премиум активирован.")
        return
    if payload.startswith("feature_"):
        feature_code = payload.replace("feature_", "")
        async with await get_db() as db:
            await db.execute(
                "INSERT INTO creator_features (user_id, feature_code, purchased_at, expires_at, status) VALUES (?, ?, ?, ?, 'active')",
                (message.from_user.id, feature_code, datetime.now().isoformat(), (datetime.now() + timedelta(days=30)).isoformat()),
            )
            await db.commit()
        await save_payment(message.successful_payment.telegram_payment_charge_id or f"feature_{feature_code}_{message.from_user.id}", message.from_user.id, message.successful_payment.total_amount, f"Creator feature: {feature_code}", "completed")
        await message.answer("✅ Платная функция активирована для вашего аккаунта.")

async def start_captcha(message: Message, state: FSMContext, giveaway_id: int):
    question, answer = await generate_captcha()
    await state.set_state(Captcha.waiting_answer)
    await state.update_data(captcha_answer=answer, giveaway_id=giveaway_id, mode="captcha")
    await message.answer(f"🤖 Реши капчу: <b>{question}</b>")


async def start_premium_captcha(message: Message, state: FSMContext, giveaway_id: int):
    code = f"{random.randint(1000, 9999)}"
    await state.set_state(Captcha.waiting_answer)
    await state.update_data(captcha_answer=code, giveaway_id=giveaway_id, mode="premium")
    await message.answer(f"🛡 Premium капча: введите код <b>{code}</b> в течение 60 секунд.")


async def start_fast_click(message: Message, state: FSMContext, giveaway_id: int, target: int, seconds: int):
    await state.set_state(Captcha.waiting_answer)
    await state.update_data(giveaway_id=giveaway_id, mode="fast_click", fast_click_target=target, fast_click_seconds=seconds, fast_click_count=0)
    await message.answer(f"⚡ Быстрый клик! Нажми кнопку {target} раз за {seconds} секунд.")
    await message.answer(
        "👉 Жми сюда",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚡ Click", callback_data=f"fast_click_{giveaway_id}")]]),
    )


@dp.callback_query(F.data.startswith("fast_click_"))
async def fast_click_handler(callback: CallbackQuery, state: FSMContext):
    giveaway_id = int(callback.data.split("_")[2])
    data = await state.get_data()
    if data.get("mode") != "fast_click" or data.get("giveaway_id") != giveaway_id:
        await callback.answer("Сначала начни режим Fast Click", show_alert=True)
        return
    target = data.get("fast_click_target", 10)
    count = data.get("fast_click_count", 0) + 1
    await state.update_data(fast_click_count=count)
    if count >= target:
        await store_participant(giveaway_id, callback.from_user.id)
        await callback.message.answer("🎉 Fast Click пройден! Ты в розыгрыше.")
        await state.clear()
    else:
        await callback.answer(f"{count}/{target}")


@dp.message(Captcha.waiting_answer)
async def captcha_check(message: Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get("mode", "captcha")
    if mode == "fast_click":
        await message.answer("⚡ Для Fast Click используйте кнопку ниже.")
        return
    answer = data["captcha_answer"]
    giveaway_id = data["giveaway_id"]
    try:
        if mode == "premium":
            if str(message.text).strip() == str(answer):
                await store_participant(giveaway_id, message.from_user.id)
                await message.answer("🎉 Premium капча пройдена! Ты в розыгрыше.")
            else:
                await message.answer("❌ Неправильно. Попробуй ещё раз.")
        elif int(message.text) == answer:
            await store_participant(giveaway_id, message.from_user.id)
            await message.answer("🎉 Правильно! Ты в розыгрыше.")
        else:
            await message.answer("❌ Неправильно. Попробуй ещё раз: /start или нажми кнопку заново.")
    except ValueError:
        await message.answer("Введи число.")
    finally:
        await state.clear()

# ====================== ПРЕМИУМ ДЛЯ СОЗДАТЕЛЕЙ ======================
@dp.message(Command("stats"))
async def show_stats(message: Message):
    async with await get_db() as db:
        users = await db.execute("SELECT COUNT(*) FROM users")
        giveaways = await db.execute("SELECT COUNT(*) FROM giveaways")
        payments = await db.execute("SELECT COUNT(*) FROM payments")
        u = (await users.fetchone())[0]
        g = (await giveaways.fetchone())[0]
        p = (await payments.fetchone())[0]
    await message.answer(f"📊 Статистика\n👥 Пользователи: {u}\n🎁 Розыгрыши: {g}\n💳 Платежи: {p}\n✨ Бот работает в премиум-режиме")


@dp.message(Command("buy_premium"))
async def buy_premium(message: Message):
    async with await get_db() as db:
        row = await db.execute("SELECT value FROM settings WHERE key='premium_price_stars'")
        price = int((await row.fetchone())[0])
    provider_token, currency = get_invoice_config()
    if PAYMENTS_PROVIDER in {"crypto_bot", "crypto", "cryptobot"} and not provider_token:
        await message.answer("🪙 Для крипто-платежей задайте CRYPTO_PROVIDER_TOKEN или PAYMENTS_PROVIDER_TOKEN.")
        return
    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Премиум-подписка",
        description="Безлимитные розыгрыши, приоритетная поддержка",
        payload="premium_sub",
        provider_token=provider_token,
        currency=currency,
        prices=[LabeledPrice(label="Премиум", amount=price)]
    )

@dp.message(F.successful_payment, lambda msg: msg.successful_payment.invoice_payload == "premium_sub")
async def premium_payment(message: Message):
    async with await get_db() as db:
        await db.execute("UPDATE users SET is_premium=1 WHERE user_id=?", (message.from_user.id,))
        await db.commit()
    await message.answer("🌟 Спасибо! Премиум активирован.")

# ====================== АДМИН-ПАНЕЛЬ ======================
@dp.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminMenu.main)
    await message.answer("👑 Админ-панель", reply_markup=build_admin_menu())


@dp.callback_query(F.data == "admin_users")
async def admin_users(callback: CallbackQuery):
    async with await get_db() as db:
        rows = await db.execute("SELECT user_id, username, security_flags, is_banned FROM users ORDER BY security_flags DESC LIMIT 10")
        users = await rows.fetchall()
    if not users:
        await callback.message.edit_text("Нет данных по пользователям.")
        return
    lines = []
    for user_id, username, flags, is_banned in users:
        status = "🚫" if is_banned else "✅"
        lines.append(f"{status} {user_id} | @{username or '—'} | flags={flags}")
    await callback.message.edit_text("🧑‍💻 Пользователи:\n" + "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin")]]))


@dp.callback_query(F.data == "admin_security")
async def admin_security(callback: CallbackQuery):
    mode = await get_security_mode()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛡 Strict" if mode != "strict" else "✅ Strict", callback_data="set_security_strict")],
        [InlineKeyboardButton(text="🧩 Moderate" if mode != "moderate" else "✅ Moderate", callback_data="set_security_moderate")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin")],
    ])
    await callback.message.edit_text(f"🛡 Режим защиты: {mode}", reply_markup=kb)


@dp.callback_query(F.data == "set_security_strict")
async def set_security_strict(callback: CallbackQuery):
    async with await get_db() as db:
        await db.execute("UPDATE settings SET value='strict' WHERE key='security_mode'")
        await db.commit()
    await callback.message.edit_text("🛡 Включён строгий режим защиты.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin")]]))


@dp.callback_query(F.data == "set_security_moderate")
async def set_security_moderate(callback: CallbackQuery):
    async with await get_db() as db:
        await db.execute("UPDATE settings SET value='moderate' WHERE key='security_mode'")
        await db.commit()
    await callback.message.edit_text("🧩 Включён умеренный режим защиты.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin")]]))

@dp.callback_query(AdminMenu.main)
async def admin_actions(callback: CallbackQuery, state: FSMContext):
    action = callback.data
    if action == "back_to_admin":
        await state.set_state(AdminMenu.main)
        await callback.message.edit_text("👑 Админ-панель", reply_markup=build_admin_menu())
    elif action == "admin_stats":
        async with await get_db() as db:
            users = await db.execute("SELECT COUNT(*) FROM users")
            giveaways = await db.execute("SELECT COUNT(*) FROM giveaways WHERE is_active=1")
            payments = await db.execute("SELECT SUM(amount) FROM payments WHERE status='completed'")
            u = (await users.fetchone())[0]
            g = (await giveaways.fetchone())[0]
            p = (await payments.fetchone())[0] or 0
        await callback.message.edit_text(f"👥 Пользователей: {u}\n🎁 Активных розыгрышей: {g}\n💰 Заработано звёзд: {p}",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin")]
                                         ]))
    elif action == "admin_broadcast":
        await state.set_state(AdminMenu.broadcast)
        await callback.message.edit_text("Введите текст рассылки:", reply_markup=cancel_back_kb("back_to_admin"))
    elif action == "admin_settings":
        await callback.message.edit_text("Настройки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💰 Изменить комиссию", callback_data="set_commission")],
            [InlineKeyboardButton(text="💎 Изменить цену премиума", callback_data="set_premium_price")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin")]
        ]))
    elif action == "admin_close_giveaway":
        # Список активных розыгрышей
        async with await get_db() as db:
            rows = await db.execute("SELECT id, text FROM giveaways WHERE is_active=1 LIMIT 10")
            kb = InlineKeyboardBuilder()
            for gid, text in await rows.fetchall():
                kb.add(InlineKeyboardButton(text=f"#{gid} {text[:20]}...", callback_data=f"forceclose_{gid}"))
            kb.add(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin"))
        await callback.message.edit_text("Выберите розыгрыш для завершения:", reply_markup=kb.as_markup())
    await callback.answer()

@dp.message(AdminMenu.broadcast)
async def broadcast_message(message: Message, state: FSMContext):
    async with await get_db() as db:
        users = await db.execute("SELECT user_id FROM users")
        async for (uid,) in users:
            try:
                await bot.send_message(uid, message.text)
            except:
                pass
    await message.answer("✅ Рассылка завершена.")
    await state.clear()

@dp.callback_query(F.data == "set_commission")
async def set_commission(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminMenu.settings_commission)
    await callback.message.answer("Введите новый процент комиссии (0-100):")


@dp.callback_query(F.data == "set_premium_price")
async def set_premium_price(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminMenu.settings_premium_price)
    await callback.message.answer("Введите новую цену премиума в звёздах:")


@dp.message(AdminMenu.settings_commission)
async def set_commission_value(message: Message, state: FSMContext):
    try:
        val = int(message.text)
        if 0 <= val <= 100:
            async with await get_db() as db:
                await db.execute("UPDATE settings SET value=? WHERE key='commission_percent'", (str(val),))
                await db.commit()
            await message.answer(f"✅ Комиссия установлена: {val}%")
        else:
            await message.answer("Введите число от 0 до 100.")
    except ValueError:
        await message.answer("Введите число.")
    await state.clear()

@dp.message(AdminMenu.settings_premium_price)
async def set_premium_price_value(message: Message, state: FSMContext):
    try:
        val = int(message.text)
        if val < 0:
            raise ValueError
        await set_setting("premium_price_stars", str(val))
        await message.answer(f"✅ Цена премиума установлена: {val} ⭐")
    except ValueError:
        await message.answer("Введите неотрицательное число.")
    await state.clear()


@dp.callback_query(F.data.startswith("forceclose_"))
async def forceclose_giveaway(callback: CallbackQuery):
    giveaway_id = int(callback.data.split("_")[1])
    async with await get_db() as db:
        await db.execute("UPDATE giveaways SET is_active=0 WHERE id=?", (giveaway_id,))
        await db.commit()
    await callback.message.edit_text(f"✅ Розыгрыш #{giveaway_id} закрыт.")


async def on_startup(app: web.Application) -> None:
    await init_db()
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN is not set. Configure environment variables before launching the bot.")
        return

    await bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET_TOKEN or None,
        drop_pending_updates=True,
    )
    logging.info("Webhook configured at %s", WEBHOOK_URL)


async def index_handler(request):
    return web.FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    app = web.Application()
    app["bot"] = bot
    app["dp"] = dp
    app.router.add_get("/", index_handler)
    app.router.add_static("/", str(STATIC_DIR), name="static")

    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET_TOKEN or None,
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)

    web.run_app(app, host="0.0.0.0", port=PORT)
