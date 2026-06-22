"""
Kino Bot — to'liq, hatolarsiz, 100 000+ foydalanuvchiga chidamli
Admin: karta raqam qo'shish/o'chirish, kino yuklash, reklama, statistika
"""
import asyncio
import html
import logging
import os
from typing import Callable, List, Optional, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, Message,
    InlineKeyboardMarkup, ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from dotenv import load_dotenv

# ══════════════════════════════════════════════════════════════════════════════
# SOZLAMALAR
# ══════════════════════════════════════════════════════════════════════════════
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID  = int(os.getenv("ADMIN_ID", "0"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN .env faylida topilmadi!")

MOVIE_PRICE   = 200    # har bir kino narxi (so'm)
DB_PATH       = "bot.db"
AD_SEND_DELAY = 0.05   # flood limit: 20 xabar/sek

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# FSM HOLATLARI
# ══════════════════════════════════════════════════════════════════════════════
class TopupStates(StatesGroup):
    waiting_for_receipt = State()

class MovieSearch(StatesGroup):
    waiting_for_query = State()

class AdminStates(StatesGroup):
    waiting_for_movie_code    = State()
    waiting_for_movie_title   = State()
    waiting_for_movie_file    = State()
    waiting_for_ad_text       = State()
    waiting_for_ad_photo      = State()
    waiting_for_ad_video      = State()
    waiting_for_reject_reason = State()
    waiting_for_card_number   = State()   # yangi karta raqam kiritish


# ══════════════════════════════════════════════════════════════════════════════
# MA'LUMOTLAR BAZASI
# ══════════════════════════════════════════════════════════════════════════════
async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA cache_size=-32000")

        # Foydalanuvchilar
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT    DEFAULT '',
                full_name   TEXT    DEFAULT '',
                balance     INTEGER DEFAULT 0,
                total_topup INTEGER DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Eski DB da total_topup yo'q bo'lishi mumkin
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN total_topup INTEGER DEFAULT 0"
            )
        except Exception:
            pass

        # Kinolar
        await db.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                code      TEXT UNIQUE NOT NULL,
                title     TEXT NOT NULL,
                file_id   TEXT NOT NULL,
                file_type TEXT NOT NULL DEFAULT 'video',
                added_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # To'lovlar
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                amount          INTEGER NOT NULL,
                status          TEXT    DEFAULT 'pending',
                receipt_file_id TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        # Tranzaksiyalar
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                amount      INTEGER NOT NULL,
                type        TEXT NOT NULL,
                description TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        # Sozlamalar (karta raqam va boshqalar)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Indekslar
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_pay_user   ON payments(user_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_pay_status ON payments(status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tx_user    ON transactions(user_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_mov_code   ON movies(code)"
        )
        await db.commit()


# ── Sozlamalar (karta raqam) ───────────────────────────────────────────────────
async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else default

async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()

async def get_card_number() -> str:
    """Faol karta raqamini qaytaradi. Yo'q bo'lsa bo'sh satr."""
    return await get_setting("card_number", "")

async def get_card_holder() -> str:
    return await get_setting("card_holder", "")

# ── Foydalanuvchilar ──────────────────────────────────────────────────────────
async def create_user(user_id: int, username: str, full_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, full_name)"
            " VALUES (?, ?, ?)",
            (user_id, username, full_name),
        )
        await db.commit()

async def get_balance(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

async def update_balance(user_id: int, amount: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (amount, user_id),
        )
        await db.commit()

async def deduct_balance(user_id: int, amount: int) -> bool:
    """Atomik yechish — race condition yo'q."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        cur = await db.execute(
            "UPDATE users SET balance = balance - ?"
            " WHERE user_id = ? AND balance >= ?",
            (amount, user_id, amount),
        )
        await db.commit()
        return cur.rowcount > 0

async def get_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users ORDER BY created_at DESC"
        ) as cur:
            return await cur.fetchall()

async def get_all_user_ids() -> List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cur:
            rows = await cur.fetchall()
            return [int(r[0]) for r in rows]

async def get_total_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

async def get_total_balance() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(balance), 0) FROM users"
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

async def get_total_topup() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'approved'"
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


# ── Kinolar ───────────────────────────────────────────────────────────────────
async def add_movie(code: str, title: str, file_id: str,
                    file_type: str = "video") -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "INSERT OR REPLACE INTO movies (code, title, file_id, file_type)"
            " VALUES (?, ?, ?, ?)",
            (code, title, file_id, file_type),
        )
        await db.commit()

async def get_movie(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM movies WHERE code = ?", (code,)
        ) as cur:
            return await cur.fetchone()

async def search_movies(query: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM movies WHERE title LIKE ? OR code LIKE ? LIMIT 10",
            (f"%{query}%", f"%{query}%"),
        ) as cur:
            return await cur.fetchall()

# ── To'lovlar ─────────────────────────────────────────────────────────────────
async def create_payment(user_id: int, amount: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        cur = await db.execute(
            "INSERT INTO payments (user_id, amount, status)"
            " VALUES (?, ?, 'pending')",
            (user_id, amount),
        )
        await db.commit()
        return int(cur.lastrowid) if cur.lastrowid else 0

async def update_payment_receipt(payment_id: int, receipt_file_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "UPDATE payments SET receipt_file_id = ? WHERE id = ?",
            (receipt_file_id, payment_id),
        )
        await db.commit()

async def get_payment(payment_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM payments WHERE id = ?", (payment_id,)
        ) as cur:
            return await cur.fetchone()

async def approve_payment(payment_id: int) -> Optional[Tuple[int, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        async with db.execute(
            "SELECT user_id, amount FROM payments"
            " WHERE id = ? AND status = 'pending'",
            (payment_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            user_id, amount = int(row[0]), int(row[1])
        await db.execute(
            "UPDATE payments SET status = 'approved' WHERE id = ?", (payment_id,)
        )
        await db.execute(
            "UPDATE users SET balance = balance + ?,"
            " total_topup = total_topup + ? WHERE user_id = ?",
            (amount, amount, user_id),
        )
        await db.execute(
            "INSERT INTO transactions (user_id, amount, type, description)"
            " VALUES (?, ?, 'topup', ?)",
            (user_id, amount, f"To'lov #{payment_id} tasdiqlandi"),
        )
        await db.commit()
        return user_id, amount

async def reject_payment(payment_id: int) -> Optional[Tuple[int, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        async with db.execute(
            "SELECT user_id, amount FROM payments"
            " WHERE id = ? AND status = 'pending'",
            (payment_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            user_id, amount = int(row[0]), int(row[1])
        await db.execute(
            "UPDATE payments SET status = 'rejected' WHERE id = ?", (payment_id,)
        )
        await db.commit()
        return user_id, amount

async def add_transaction(user_id: int, amount: int,
                          ttype: str, description: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "INSERT INTO transactions (user_id, amount, type, description)"
            " VALUES (?, ?, ?, ?)",
            (user_id, amount, ttype, description),
        )
        await db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# TUGMALAR
# ══════════════════════════════════════════════════════════════════════════════
def kb_main_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="🎬 Kino qidirish")
    kb.button(text="💰 Balansim")
    kb.button(text="💳 Balansni to'ldirish")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def kb_admin_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="🎬 Kino qo'shish")
    kb.button(text="💳 Karta sozlamalari")
    kb.button(text="📊 Statistika")
    kb.button(text="📢 Reklama yuborish")
    kb.button(text="👥 Foydalanuvchilar")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def kb_topup_amounts() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="5 000 so'm",      callback_data="topup_5000")
    kb.button(text="10 000 so'm",     callback_data="topup_10000")
    kb.button(text="15 000 so'm",     callback_data="topup_15000")
    kb.button(text="20 000 so'm",     callback_data="topup_20000")
    kb.button(text="❌ Bekor qilish", callback_data="cancel")
    kb.adjust(2, 2, 1)
    return kb.as_markup()

def kb_send_receipt(payment_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📸 Chek rasmini yuborish",
              callback_data=f"send_receipt_{payment_id}")
    kb.button(text="❌ Bekor qilish", callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()

def kb_balance_empty() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Balansni to'ldirish", callback_data="topup_start")
    kb.adjust(1)
    return kb.as_markup()

def kb_payment_actions(payment_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Tasdiqlash",   callback_data=f"approve_{payment_id}")
    kb.button(text="❌ Bekor qilish", callback_data=f"reject_{payment_id}")
    kb.adjust(2)
    return kb.as_markup()

def kb_reject_reasons(payment_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Noto'g'ri chek",
              callback_data=f"reason_{payment_id}_nogri_chek")
    kb.button(text="❌ Summa mos emas",
              callback_data=f"reason_{payment_id}_summa_mos_emas")
    kb.button(text="❌ Chek sifati yomon",
              callback_data=f"reason_{payment_id}_sifat_yomon")
    kb.button(text="✏️ Boshqa sabab yozish",
              callback_data=f"reason_{payment_id}_custom")
    kb.adjust(1)
    return kb.as_markup()

def kb_ad_type() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Matn",  callback_data="ad_text")
    kb.button(text="🖼 Rasm",  callback_data="ad_photo")
    kb.button(text="🎥 Video", callback_data="ad_video")
    kb.button(text="❌ Bekor", callback_data="cancel")
    kb.adjust(3, 1)
    return kb.as_markup()

def kb_card_manage(has_card: bool) -> InlineKeyboardMarkup:
    """Karta sozlamalari tugmalari."""
    kb = InlineKeyboardBuilder()
    if has_card:
        kb.button(text="✏️ Karta raqamni o'zgartirish",
                  callback_data="card_change")
        kb.button(text="🗑 Karta raqamni o'chirish",
                  callback_data="card_delete")
    else:
        kb.button(text="➕ Karta raqam qo'shish",
                  callback_data="card_add")
    kb.button(text="❌ Yopish", callback_data="cancel_admin")
    kb.adjust(1)
    return kb.as_markup()

def kb_card_delete_confirm() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Ha, o'chirish", callback_data="card_delete_confirm")
    kb.button(text="❌ Yo'q",          callback_data="cancel_admin")
    kb.adjust(2)
    return kb.as_markup()


# ══════════════════════════════════════════════════════════════════════════════
# YORDAMCHI FUNKSIYALAR
# ══════════════════════════════════════════════════════════════════════════════
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def user_menu(user_id: int) -> ReplyKeyboardMarkup:
    return kb_admin_menu() if is_admin(user_id) else kb_main_menu()

def safe_name(user) -> str:
    """full_name ni HTML injection dan himoyalaydi."""
    return html.escape(user.full_name or "Nomsiz")

async def _broadcast(bot: Bot, user_ids: List[int],
                     send_fn: Callable) -> Tuple[int, int]:
    """
    Hamma foydalanuvchilarga xabar yuborish (flood-safe).
    Qaytaradi: (yuborildi, xato)
    """
    sent = failed = 0
    for uid in user_ids:
        try:
            await send_fn(uid)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(AD_SEND_DELAY)
    return sent, failed


# ══════════════════════════════════════════════════════════════════════════════
# ROUTER
# ══════════════════════════════════════════════════════════════════════════════
router = Router()

# Kanal postlari va from_user=None ni bloklash (message va callback_query uchun)
@router.message.outer_middleware()
async def private_only(handler, event: Message, data: dict):
    if not event.from_user:
        return
    return await handler(event, data)

@router.callback_query.outer_middleware()
async def cb_private_only(handler, event: CallbackQuery, data: dict):
    if not event.from_user:
        return
    return await handler(event, data)

# ─── /start ───────────────────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    await create_user(
        uid,
        message.from_user.username or "",
        message.from_user.full_name or "",
    )
    if is_admin(uid):
        await message.answer(
            "👑 Xush kelibsiz, <b>Admin</b>!\n\n"
            "Quyidagi tugmalardan foydalaning:",
            reply_markup=kb_admin_menu(),
        )
    else:
        await message.answer(
            f"👋 Xush kelibsiz, <b>{safe_name(message.from_user)}</b>!\n\n"
            "🎬 Bu botda siz kinolarni tomosha qilishingiz mumkin.\n"
            f"💰 Har bir kino <b>{MOVIE_PRICE} so'm</b> turadi.\n\n"
            "Quyidagi tugmalardan foydalaning:",
            reply_markup=kb_main_menu(),
        )

# ─── /admin ───────────────────────────────────────────────────────────────────
@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Siz admin emassiz.")
        return
    await state.clear()
    await message.answer(
        "👑 <b>Admin panel</b>",
        reply_markup=kb_admin_menu(),
    )


# ══════════════════════════════════════════════════════════════════════════════
# FOYDALANUVCHI — BALANS
# ══════════════════════════════════════════════════════════════════════════════
@router.message(F.text == "💰 Balansim")
async def show_balance(message: Message):
    uid     = message.from_user.id
    balance = await get_balance(uid)
    await message.answer(
        f"💰 <b>Sizning balansingiz:</b> {balance:,} so'm\n\n"
        f"🎬 Har bir kino uchun: <b>{MOVIE_PRICE} so'm</b> yechiladi.",
        reply_markup=user_menu(uid),
    )

# ─── Balansni to'ldirish (menyu tugmasi) ─────────────────────────────────────
@router.message(F.text == "💳 Balansni to'ldirish")
async def topup_start_msg(message: Message):
    if is_admin(message.from_user.id):
        return
    card = await get_card_number()
    if not card:
        await message.answer(
            "⚠️ Hozircha to'lov qabul qilinmayapti.\n"
            "Iltimos, keyinroq urinib ko'ring.",
            reply_markup=kb_main_menu(),
        )
        return
    await message.answer(
        "💳 <b>Qancha pul to'ldirmoqchisiz?</b>\n"
        "Quyidagi miqdorlardan birini tanlang:",
        reply_markup=kb_topup_amounts(),
    )

# ─── Balansi tugaganda inline tugma ──────────────────────────────────────────
@router.callback_query(F.data == "topup_start")
async def topup_start_cb(call: CallbackQuery):
    await call.answer()
    card = await get_card_number()
    if not card:
        await call.message.edit_text(
            "⚠️ Hozircha to'lov qabul qilinmayapti. Keyinroq urinib ko'ring."
        )
        return
    await call.message.edit_text(
        "💳 <b>Qancha pul to'ldirmoqchisiz?</b>\n"
        "Quyidagi miqdorlardan birini tanlang:",
        reply_markup=kb_topup_amounts(),
    )

# ─── Miqdor tanlandi ─────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("topup_"))
async def topup_amount_chosen(call: CallbackQuery, state: FSMContext):
    raw = call.data.replace("topup_", "")
    if not raw.isdigit():
        await call.answer()
        return
    amount = int(raw)

    card = await get_card_number()
    if not card:
        await call.answer("⚠️ Hozircha to'lov qabul qilinmayapti!", show_alert=True)
        return

    holder     = await get_card_holder()
    payment_id = await create_payment(call.from_user.id, amount)
    if not payment_id:
        await call.answer("⚠️ Xatolik yuz berdi, qayta urinib ko'ring!", show_alert=True)
        return

    # Faqat shu yerda BITTA call.answer() — yuqorida return bo'lganlari o'z answer'ini oldi
    await call.answer()
    await state.update_data(payment_id=payment_id, amount=amount)
    await state.set_state(TopupStates.waiting_for_receipt)

    holder_line = f"👤 Karta egasi: <b>{html.escape(holder)}</b>\n" if holder else ""
    await call.message.edit_text(
        f"💳 <b>To'lov ma'lumotlari:</b>\n\n"
        f"💰 Summa: <b>{amount:,} so'm</b>\n"
        f"🏦 Karta raqami: <code>{card}</code>\n"
        f"{holder_line}\n"
        f"⬆️ Yuqoridagi kartaga <b>{amount:,} so'm</b> o'tkazing,\n"
        "so'ngra <b>Chek rasmini yuborish</b> tugmasini bosing.",
        reply_markup=kb_send_receipt(payment_id),
    )

# ─── Chek yuborish tugmasi ────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("send_receipt_"))
async def ask_for_receipt(call: CallbackQuery, state: FSMContext):
    payment_id = int(call.data.replace("send_receipt_", ""))
    await state.update_data(payment_id=payment_id)
    await state.set_state(TopupStates.waiting_for_receipt)
    await call.answer()
    await call.message.edit_text(
        "📸 <b>Chek rasmini yuboring:</b>\n\n"
        "To'lov chekining rasmini (screenshot) yuboring."
    )


# ─── Chek rasmi keldi ─────────────────────────────────────────────────────────
@router.message(TopupStates.waiting_for_receipt, F.photo)
async def receipt_received(message: Message, state: FSMContext, bot: Bot):
    data       = await state.get_data()
    payment_id = data.get("payment_id")
    if not payment_id:
        await state.clear()
        await message.answer(
            "⚠️ Xatolik yuz berdi. Qaytadan /start bosing.",
            reply_markup=kb_main_menu(),
        )
        return

    photo_file_id = message.photo[-1].file_id
    await update_payment_receipt(payment_id, photo_file_id)

    payment = await get_payment(payment_id)
    amount  = int(payment["amount"]) if payment else 0

    await state.clear()
    await message.answer(
        "✅ <b>Chekingiz qabul qilindi!</b>\n\n"
        "⏳ To'lov tekshiruvga yuborildi. "
        "Adminlar tekshirib tasdiqlaydi. Tez orada!\n\n"
        f"💰 So'ralgan miqdor: <b>{amount:,} so'm</b>",
        reply_markup=kb_main_menu(),
    )

    # Adminga chek + tugmalar
    try:
        user = message.from_user
        await bot.send_photo(
            chat_id=ADMIN_ID,
            photo=photo_file_id,
            caption=(
                f"💳 <b>Yangi to'lov so'rovi!</b>\n\n"
                f"👤 Foydalanuvchi: "
                f"<a href='tg://user?id={user.id}'>{safe_name(user)}</a>\n"
                f"🆔 ID: <code>{user.id}</code>\n"
                f"💰 Miqdor: <b>{amount:,} so'm</b>\n"
                f"🔖 To'lov ID: <code>{payment_id}</code>"
            ),
            reply_markup=kb_payment_actions(payment_id),
        )
    except Exception as e:
        logger.warning("Adminga chek yuborganda xato: %s", e)

# ─── Noto'g'ri format ─────────────────────────────────────────────────────────
@router.message(TopupStates.waiting_for_receipt)
async def wrong_receipt_type(message: Message):
    await message.answer(
        "⚠️ Iltimos, faqat <b>rasm (screenshot)</b> yuboring."
    )

# ─── Bekor qilish (foydalanuvchi) ────────────────────────────────────────────
@router.callback_query(F.data == "cancel")
async def cancel_action(call: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = call.from_user.id
    await call.answer()
    try:
        await call.message.edit_text("❌ Amal bekor qilindi.")
    except Exception:
        pass
    await call.message.answer("Asosiy menyu:", reply_markup=user_menu(uid))

# ─── Bekor qilish (admin inline) ─────────────────────────────────────────────
@router.callback_query(F.data == "cancel_admin")
async def cancel_admin_action(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# FOYDALANUVCHI — KINO QIDIRISH
# ══════════════════════════════════════════════════════════════════════════════
@router.message(F.text == "🎬 Kino qidirish")
async def movie_search_start(message: Message, state: FSMContext):
    if is_admin(message.from_user.id):
        return
    await state.set_state(MovieSearch.waiting_for_query)
    await message.answer(
        "🔍 <b>Kino nomini yoki kodini yozing:</b>\n\n"
        "Misol: <code>batman</code> yoki <code>001</code>"
    )


@router.message(MovieSearch.waiting_for_query)
async def movie_search_query(message: Message, state: FSMContext, bot: Bot):
    query = message.text.strip() if message.text else ""
    await state.clear()

    if not query:
        await message.answer(
            "⚠️ Iltimos, kino nomi yoki kodini yozing.",
            reply_markup=kb_main_menu(),
        )
        return

    # Avval aniq kod bo'yicha
    movie = await get_movie(query)
    if movie:
        await _send_movie_to_user(message, bot, movie)
        return

    # Keyin qisman mos ism bo'yicha
    results = await search_movies(query)
    if not results:
        await message.answer(
            "❌ <b>Kino topilmadi!</b>\n\n"
            "Boshqa nom yoki kod bilan qidirib ko'ring.",
            reply_markup=kb_main_menu(),
        )
        return

    if len(results) == 1:
        await _send_movie_to_user(message, bot, results[0])
        return

    # Bir nechta natija — ro'yxat ko'rsatish
    text = "🎬 <b>Topilgan kinolar:</b>\n\n"
    for m in results:
        text += f"• <code>{m['code']}</code> — {html.escape(m['title'])}\n"
    text += "\nAniq <b>kodni</b> yuboring."
    await message.answer(text, reply_markup=kb_main_menu())


async def _send_movie_to_user(message: Message, bot: Bot, movie) -> None:
    """Kinoni yuborish. protect_content=True — saqlash/uzatish bloklangan."""
    uid     = message.from_user.id
    balance = await get_balance(uid)

    if balance < MOVIE_PRICE:
        await message.answer(
            f"💸 <b>Balansingiz yetarli emas!</b>\n\n"
            f"💰 Joriy balans: <b>{balance:,} so'm</b>\n"
            f"🎬 Kino narxi: <b>{MOVIE_PRICE:,} so'm</b>\n\n"
            "Balansingizni to'ldiring:",
            reply_markup=kb_balance_empty(),
        )
        return

    ok = await deduct_balance(uid, MOVIE_PRICE)
    if not ok:
        await message.answer(
            "⚠️ Balansdan pul yechishda xatolik. Qayta urinib ko'ring.",
            reply_markup=kb_main_menu(),
        )
        return

    await add_transaction(
        uid, MOVIE_PRICE, "watch",
        f"Kino: {movie['title']} ({movie['code']})",
    )
    new_bal = await get_balance(uid)

    caption = (
        f"🎬 <b>{html.escape(movie['title'])}</b>\n"
        f"🔖 Kod: <code>{movie['code']}</code>\n\n"
        f"💰 Hisobdan yechildi: <b>{MOVIE_PRICE:,} so'm</b>\n"
        f"💳 Qolgan balans: <b>{new_bal:,} so'm</b>"
    )

    try:
        if movie["file_type"] == "document":
            await bot.send_document(
                chat_id=message.chat.id,
                document=movie["file_id"],
                caption=caption,
                protect_content=True,
            )
        else:
            await bot.send_video(
                chat_id=message.chat.id,
                video=movie["file_id"],
                caption=caption,
                protect_content=True,
            )
    except Exception as e:
        logger.error("Kino yuborishda xato: %s", e)
        # Balansni qaytarish
        await update_balance(uid, MOVIE_PRICE)
        await add_transaction(
            uid, MOVIE_PRICE, "refund",
            f"Qaytarildi — kino yuborishda xato: {movie['code']}",
        )
        await message.answer(
            "⚠️ Kino yuborishda xatolik yuz berdi. Balansingiz qaytarildi.",
            reply_markup=kb_main_menu(),
        )


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — TO'LOV TASDIQLASH / BEKOR QILISH
# ══════════════════════════════════════════════════════════════════════════════
@router.callback_query(F.data.startswith("approve_"))
async def cb_approve_payment(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Siz admin emassiz!", show_alert=True)
        return

    payment_id = int(call.data.replace("approve_", ""))
    result     = await approve_payment(payment_id)

    if not result:
        await call.answer(
            "⚠️ To'lov topilmadi yoki allaqachon ko'rib chiqilgan!",
            show_alert=True,
        )
        return

    user_id, amount = result

    try:
        new_cap = (call.message.caption or "") + "\n\n✅ <b>TASDIQLANDI</b>"
        await call.message.edit_caption(new_cap, parse_mode="HTML")
    except Exception:
        pass

    await call.answer("✅ To'lov tasdiqlandi!")

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"✅ <b>To'lovingiz tasdiqlandi!</b>\n\n"
                f"💰 Balansingizga <b>{amount:,} so'm</b> qo'shildi.\n\n"
                "Endi kinolarni tomosha qilishingiz mumkin! 🎬"
            ),
            reply_markup=kb_main_menu(),
        )
    except Exception as e:
        logger.warning("Foydalanuvchiga xabar yuborganda xato: %s", e)


@router.callback_query(F.data.startswith("reject_"))
async def cb_reject_payment(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Siz admin emassiz!", show_alert=True)
        return

    payment_id = int(call.data.replace("reject_", ""))
    await state.update_data(reject_payment_id=payment_id)
    await call.answer()
    await call.message.answer(
        "❌ <b>Bekor qilish sababini tanlang:</b>",
        reply_markup=kb_reject_reasons(payment_id),
    )


@router.callback_query(F.data.startswith("reason_"))
async def cb_reject_reason(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Siz admin emassiz!", show_alert=True)
        return

    # format: reason_{payment_id}_{reason_key}
    parts      = call.data.split("_", 2)
    payment_id = int(parts[1])
    reason_key = parts[2]

    if reason_key == "custom":
        await state.update_data(reject_payment_id=payment_id)
        await state.set_state(AdminStates.waiting_for_reject_reason)
        await call.answer()
        await call.message.edit_text("✏️ Bekor qilish sababini yozing:")
        return

    reason_map = {
        "nogri_chek":     "Noto'g'ri chek yuborilgan",
        "summa_mos_emas": "To'lov summasi mos emas",
        "sifat_yomon":    "Chek rasmi sifati yomon, aniq emas",
    }
    reason = reason_map.get(reason_key, "Noma'lum sabab")
    await call.answer()
    await _do_reject_cb(call, bot, state, payment_id, reason)


@router.message(AdminStates.waiting_for_reject_reason)
async def msg_reject_custom_reason(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    data       = await state.get_data()
    payment_id = data.get("reject_payment_id")
    if not payment_id:
        await state.clear()
        await message.answer(
            "⚠️ Xatolik. Qaytadan urinib ko'ring.",
            reply_markup=kb_admin_menu(),
        )
        return
    reason = message.text.strip() if message.text else "Sabab ko'rsatilmadi"
    await state.clear()
    await _do_reject_msg(message, bot, int(payment_id), reason)


async def _do_reject_cb(
    call: CallbackQuery, bot: Bot,
    state: FSMContext, payment_id: int, reason: str,
) -> None:
    result = await reject_payment(payment_id)
    await state.clear()
    if not result:
        await call.answer(
            "⚠️ To'lov topilmadi yoki allaqachon ko'rib chiqilgan!",
            show_alert=True,
        )
        return
    user_id, amount = result
    try:
        await call.message.edit_text(
            f"❌ <b>Bekor qilindi</b>\n📝 Sabab: {html.escape(reason)}"
        )
    except Exception:
        pass
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"❌ <b>To'lovingiz bekor qilindi.</b>\n\n"
                f"💰 Miqdor: <b>{amount:,} so'm</b>\n"
                f"📝 Sabab: {html.escape(reason)}\n\n"
                "Qayta to'lov qilishingiz mumkin. 💳"
            ),
            reply_markup=kb_main_menu(),
        )
    except Exception as e:
        logger.warning("Foydalanuvchiga xabar yuborganda xato: %s", e)


async def _do_reject_msg(
    message: Message, bot: Bot, payment_id: int, reason: str,
) -> None:
    result = await reject_payment(payment_id)
    if not result:
        await message.answer("⚠️ To'lov topilmadi!", reply_markup=kb_admin_menu())
        return
    user_id, amount = result
    await message.answer(
        f"❌ To'lov #{payment_id} bekor qilindi.\n📝 Sabab: {html.escape(reason)}",
        reply_markup=kb_admin_menu(),
    )
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"❌ <b>To'lovingiz bekor qilindi.</b>\n\n"
                f"💰 Miqdor: <b>{amount:,} so'm</b>\n"
                f"📝 Sabab: {html.escape(reason)}\n\n"
                "Qayta to'lov qilishingiz mumkin. 💳"
            ),
            reply_markup=kb_main_menu(),
        )
    except Exception as e:
        logger.warning("Foydalanuvchiga xabar yuborganda xato: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — KARTA SOZLAMALARI
# ══════════════════════════════════════════════════════════════════════════════
@router.message(F.text == "💳 Karta sozlamalari")
async def card_settings(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    card   = await get_card_number()
    holder = await get_card_holder()

    if card:
        holder_line = f"\n👤 Karta egasi: <b>{html.escape(holder)}</b>" if holder else ""
        text = (
            f"💳 <b>Joriy karta ma'lumotlari:</b>\n\n"
            f"🔢 Raqam: <code>{card}</code>"
            f"{holder_line}"
        )
    else:
        text = "💳 <b>Hozircha karta raqam qo'shilmagan.</b>"

    await message.answer(text, reply_markup=kb_card_manage(bool(card)))


# ─── Karta qo'shish tugmasi ───────────────────────────────────────────────────
@router.callback_query(F.data.in_({"card_add", "card_change"}))
async def card_add_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q!", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_card_number)
    await call.answer()
    await call.message.edit_text(
        "🔢 Yangi karta raqamini yozing:\n\n"
        "Misol: <code>9860 1701 1234 5678</code>\n\n"
        "Karta egasini ham qo'shish uchun ikkinchi qatorda yozing:\n"
        "<code>9860 1701 1234 5678\nAlisher Karimov</code>"
    )


# ─── Karta raqam kiritildi ────────────────────────────────────────────────────
@router.message(AdminStates.waiting_for_card_number)
async def card_number_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if not message.text:
        await message.answer("⚠️ Matn kiriting.")
        return

    lines      = message.text.strip().splitlines()
    card_input = lines[0].strip()
    holder     = lines[1].strip() if len(lines) > 1 else ""

    # Raqamlar va bo'shliqlardan tashkari belgi yo'qligini tekshirish
    digits_only = card_input.replace(" ", "").replace("-", "")
    if not digits_only.isdigit() or len(digits_only) < 12:
        await message.answer(
            "⚠️ Karta raqami noto'g'ri!\n\n"
            "Faqat raqamlar bo'lishi kerak (12-19 ta raqam).\n"
            "Misol: <code>9860 1701 1234 5678</code>"
        )
        return

    await set_setting("card_number", card_input)
    await set_setting("card_holder", holder)
    await state.clear()

    holder_line = f"\n👤 Karta egasi: <b>{html.escape(holder)}</b>" if holder else ""
    await message.answer(
        f"✅ <b>Karta ma'lumotlari saqlandi!</b>\n\n"
        f"🔢 Raqam: <code>{card_input}</code>"
        f"{holder_line}",
        reply_markup=kb_admin_menu(),
    )


# ─── Karta o'chirish — tasdiqlash so'rash ────────────────────────────────────
@router.callback_query(F.data == "card_delete")
async def card_delete_ask(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q!", show_alert=True)
        return
    await call.answer()
    await call.message.edit_text(
        "🗑 <b>Karta raqamini o'chirishni tasdiqlaysizmi?</b>\n\n"
        "O'chirilgandan so'ng foydalanuvchilar to'lov qila olmaydi.",
        reply_markup=kb_card_delete_confirm(),
    )


# ─── Karta o'chirish — tasdiqlandi ───────────────────────────────────────────
@router.callback_query(F.data == "card_delete_confirm")
async def card_delete_confirm(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q!", show_alert=True)
        return
    await set_setting("card_number", "")
    await set_setting("card_holder", "")
    await call.answer("✅ Karta o'chirildi!")
    try:
        await call.message.edit_text(
            "🗑 <b>Karta raqami o'chirildi.</b>\n\n"
            "Yangi karta qo'shish uchun «💳 Karta sozlamalari» tugmasini bosing."
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — STATISTIKA VA FOYDALANUVCHILAR
# ══════════════════════════════════════════════════════════════════════════════
@router.message(F.text == "📊 Statistika")
async def show_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    total_users   = await get_total_users()
    total_balance = await get_total_balance()
    total_topup   = await get_total_topup()
    card          = await get_card_number()
    card_status   = f"<code>{card}</code>" if card else "❌ Qo'shilmagan"

    await message.answer(
        f"📊 <b>Bot statistikasi:</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{total_users:,}</b>\n"
        f"💳 Jami to'ldirilgan pul:  <b>{total_topup:,} so'm</b>\n"
        f"💰 Hozirgi umumiy balans:  <b>{total_balance:,} so'm</b>\n\n"
        f"🏦 Faol karta: {card_status}",
        reply_markup=kb_admin_menu(),
    )


@router.message(F.text == "👥 Foydalanuvchilar")
async def show_users(message: Message):
    if not is_admin(message.from_user.id):
        return
    users = await get_all_users()
    if not users:
        await message.answer(
            "Hali foydalanuvchilar yo'q.", reply_markup=kb_admin_menu()
        )
        return

    lines = []
    for u in users[:50]:
        uname = f"@{u['username']}" if u["username"] else "—"
        lines.append(
            f"• <a href='tg://user?id={u['user_id']}'>"
            f"{html.escape(u['full_name'] or 'Nomsiz')}</a>"
            f" | {uname} | 💰 {u['balance']:,} so'm"
        )

    header   = f"👥 <b>Foydalanuvchilar ({len(users)} ta):</b>\n\n"
    footer   = f"\n... va yana {len(users)-50} ta" if len(users) > 50 else ""
    full_txt = header + "\n".join(lines) + footer

    # 4096 belgi limitidan oshsa bo'lib yuborish
    if len(full_txt) <= 4096:
        await message.answer(full_txt, reply_markup=kb_admin_menu())
    else:
        chunk = header
        for i, line in enumerate(lines):
            if len(chunk) + len(line) + 1 > 4000:
                await message.answer(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            await message.answer(chunk, reply_markup=kb_admin_menu())


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — KINO QO'SHISH
# ══════════════════════════════════════════════════════════════════════════════
@router.message(F.text == "🎬 Kino qo'shish")
async def add_movie_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_for_movie_code)
    await message.answer(
        "🎬 <b>Yangi kino qo'shish</b>\n\n"
        "1️⃣ Kino uchun <b>kod</b> yozing (masalan: <code>001</code>):"
    )


@router.message(AdminStates.waiting_for_movie_code)
async def add_movie_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    code = (message.text or "").strip()
    if not code:
        await message.answer("⚠️ Kod bo'sh bo'lmasin, qayta yozing:")
        return
    await state.update_data(movie_code=code)
    await state.set_state(AdminStates.waiting_for_movie_title)
    await message.answer(
        f"✅ Kod: <code>{html.escape(code)}</code>\n\n"
        "2️⃣ Kino <b>nomini</b> yozing:"
    )


@router.message(AdminStates.waiting_for_movie_title)
async def add_movie_title(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    title = (message.text or "").strip()
    if not title:
        await message.answer("⚠️ Nom bo'sh bo'lmasin, qayta yozing:")
        return
    await state.update_data(movie_title=title)
    await state.set_state(AdminStates.waiting_for_movie_file)
    await message.answer(
        f"✅ Nom: <b>{html.escape(title)}</b>\n\n"
        "3️⃣ Kino <b>video faylini</b> yuboring (video yoki dokument sifatida):"
    )


@router.message(AdminStates.waiting_for_movie_file, F.video | F.document)
async def add_movie_file(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data  = await state.get_data()
    code  = data.get("movie_code", "")
    title = data.get("movie_title", "")

    if not code or not title:
        await state.clear()
        await message.answer(
            "⚠️ Xatolik yuz berdi. Qaytadan /start bosing.",
            reply_markup=kb_admin_menu(),
        )
        return

    if message.video:
        file_id   = message.video.file_id
        file_type = "video"
    else:
        file_id   = message.document.file_id
        file_type = "document"

    await add_movie(code, title, file_id, file_type)
    await state.clear()
    await message.answer(
        f"✅ <b>Kino muvaffaqiyatli qo'shildi!</b>\n\n"
        f"🔖 Kod: <code>{html.escape(code)}</code>\n"
        f"🎬 Nom: <b>{html.escape(title)}</b>\n"
        f"📁 Tur: <b>{file_type}</b>",
        reply_markup=kb_admin_menu(),
    )


@router.message(AdminStates.waiting_for_movie_file)
async def add_movie_file_wrong(message: Message):
    await message.answer(
        "⚠️ Iltimos, <b>video yoki dokument</b> fayl yuboring!"
    )


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — REKLAMA YUBORISH (flood-safe)
# ══════════════════════════════════════════════════════════════════════════════
@router.message(F.text == "📢 Reklama yuborish")
async def ad_start(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "📢 <b>Reklama turini tanlang:</b>",
        reply_markup=kb_ad_type(),
    )


@router.callback_query(F.data == "ad_text")
async def ad_text_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.waiting_for_ad_text)
    await call.answer()
    await call.message.edit_text("✏️ Reklama <b>matnini</b> yozing:")


@router.callback_query(F.data == "ad_photo")
async def ad_photo_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.waiting_for_ad_photo)
    await call.answer()
    await call.message.edit_text(
        "🖼 Reklama <b>rasmini</b> yuboring (caption bilan yoki ustirsiz):"
    )


@router.callback_query(F.data == "ad_video")
async def ad_video_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.waiting_for_ad_video)
    await call.answer()
    await call.message.edit_text(
        "🎥 Reklama <b>videosini</b> yuboring (caption bilan yoki ustirsiz):"
    )


@router.message(AdminStates.waiting_for_ad_text)
async def send_ad_text(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    text     = message.text or ""
    user_ids = await get_all_user_ids()
    status   = await message.answer(
        f"⏳ Reklama yuborilmoqda... (0 / {len(user_ids)})"
    )

    async def _send_text(uid: int) -> None:
        await bot.send_message(uid, text, parse_mode="HTML")

    sent, failed = await _broadcast(bot, user_ids, _send_text)

    try:
        await status.edit_text(
            f"📢 <b>Reklama (matn) yuborildi!</b>\n\n"
            f"✅ Muvaffaqiyatli: <b>{sent}</b>\n"
            f"❌ Xatolik: <b>{failed}</b>"
        )
    except Exception:
        pass
    await message.answer("✅ Tayyor.", reply_markup=kb_admin_menu())


@router.message(AdminStates.waiting_for_ad_photo, F.photo)
async def send_ad_photo(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    photo_id = message.photo[-1].file_id
    caption  = message.caption or ""
    user_ids = await get_all_user_ids()
    status   = await message.answer(
        f"⏳ Reklama yuborilmoqda... (0 / {len(user_ids)})"
    )

    async def _send_photo(uid: int) -> None:
        await bot.send_photo(uid, photo_id, caption=caption, parse_mode="HTML")

    sent, failed = await _broadcast(bot, user_ids, _send_photo)

    try:
        await status.edit_text(
            f"📢 <b>Reklama (rasm) yuborildi!</b>\n\n"
            f"✅ Muvaffaqiyatli: <b>{sent}</b>\n"
            f"❌ Xatolik: <b>{failed}</b>"
        )
    except Exception:
        pass
    await message.answer("✅ Tayyor.", reply_markup=kb_admin_menu())


@router.message(AdminStates.waiting_for_ad_photo)
async def send_ad_photo_wrong(message: Message):
    await message.answer("⚠️ Iltimos, <b>rasm</b> yuboring!")


@router.message(AdminStates.waiting_for_ad_video, F.video)
async def send_ad_video(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    video_id = message.video.file_id
    caption  = message.caption or ""
    user_ids = await get_all_user_ids()
    status   = await message.answer(
        f"⏳ Reklama yuborilmoqda... (0 / {len(user_ids)})"
    )

    async def _send_video(uid: int) -> None:
        await bot.send_video(uid, video_id, caption=caption, parse_mode="HTML")

    sent, failed = await _broadcast(bot, user_ids, _send_video)

    try:
        await status.edit_text(
            f"📢 <b>Reklama (video) yuborildi!</b>\n\n"
            f"✅ Muvaffaqiyatli: <b>{sent}</b>\n"
            f"❌ Xatolik: <b>{failed}</b>"
        )
    except Exception:
        pass
    await message.answer("✅ Tayyor.", reply_markup=kb_admin_menu())


@router.message(AdminStates.waiting_for_ad_video)
async def send_ad_video_wrong(message: Message):
    await message.answer("⚠️ Iltimos, <b>video</b> yuboring!")


# ══════════════════════════════════════════════════════════════════════════════
# ASOSIY ISHGA TUSHIRISH
# ══════════════════════════════════════════════════════════════════════════════
async def main() -> None:
    await init_db()
    logger.info("Ma'lumotlar bazasi tayyor.")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Bot ishga tushmoqda...")
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        await bot.session.close()
        logger.info("Bot to'xtatildi.")


if __name__ == "__main__":
    asyncio.run(main())
