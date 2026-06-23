"""
Kino Bot — to'liq, hatolarsiz, barcha funksiyalar ishlaydigan
"""
import asyncio
import html
import logging
import os
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
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

load_dotenv()
BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
ADMIN_ID   = int(os.getenv("ADMIN_ID", "0"))
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN topilmadi!")

MOVIE_PRICE = 200
BASE_DIR    = Path(__file__).resolve().parent
DB_PATH     = str(BASE_DIR / "bot.db")
AD_DELAY    = 0.05
PAGE_SIZE   = 20

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════
# YORDAMCHI FUNKSIYALAR
# ═══════════════════════════════════════════════════════
async def safe_edit(msg: Message, text: str, **kwargs):
    """Xabarni xavfsiz tahrirlash (agar o'zgarish bo'lmasa xatolik yo'q)."""
    try:
        await msg.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
        # Xabar o'zgarmagani uchun error yo'q — ignor qilish

# ─── In-Memory Cache (100K+ foydalanuvchi uchun) ───────────────────────
_cache = {
    "cfg": {},  # card_number, card_holder
    "movies_all": None,
    "movies_all_ts": 0,
    "ml_text": None,
    "ml_text_ts": 0,
}

def cache_invalidate(key=None):
    """Cache tozalash."""
    if key:
        if key == "movies":
            _cache["movies_all"] = None
            _cache["movies_all_ts"] = 0
        elif key == "text":
            _cache["ml_text"] = None
            _cache["ml_text_ts"] = 0
    else:
        _cache["movies_all"] = None
        _cache["ml_text"] = None
        _cache["cfg"].clear()
        _cache["movies_all_ts"] = 0
        _cache["ml_text_ts"] = 0

async def safe_send_text(bot: Bot, chat_id: int, text: str):
    try:
        return await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        return await bot.send_message(chat_id, esc(text))

async def safe_send_caption(bot: Bot, method: Callable, chat_id: int,
                            media, caption: str):
    try:
        return await method(chat_id, media,
                            caption=caption, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        return await method(chat_id, media, caption=esc(caption))

async def _broadcast(bot: Bot, uids: List[int], fn: Callable) -> Tuple[int, int]:
    ok = fail = 0
    sem = asyncio.Semaphore(8)

    async def _send(uid: int):
        async with sem:
            try:
                await fn(uid)
                return 1, 0
            except Exception:
                return 0, 1

    chunk_size = 2000
    for start in range(0, len(uids), chunk_size):
        batch = uids[start:start + chunk_size]
        results = await asyncio.gather(
            *[_send(uid) for uid in batch],
            return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                fail += 1
            else:
                ok += result[0]
                fail += result[1]
    return ok, fail

# ═══════════════════════════════════════════════════════
# FSM HOLATLARI
# ═══════════════════════════════════════════════════════
class STopup(StatesGroup):
    amount  = State()
    receipt = State()

class SAddMovie(StatesGroup):
    code  = State()
    title = State()
    file  = State()

class SEditMovie(StatesGroup):
    choose = State()
    title  = State()
    file   = State()

class SDelMovie(StatesGroup):
    code = State()

class SAd(StatesGroup):
    text  = State()
    photo = State()
    video = State()

class SCard(StatesGroup):
    inp = State()

class SReject(StatesGroup):
    reason = State()

class SMLText(StatesGroup):
    text = State()

# ═══════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════
async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        # Performance optimizations for large-scale (100K+ users)
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA cache_size=-64000")  # 64MB cache
        await db.execute("PRAGMA temp_store=MEMORY")
        await db.execute("PRAGMA mmap_size=30000000")  # Memory-mapped I/O
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT    DEFAULT '',
                full_name   TEXT    DEFAULT '',
                balance     INTEGER DEFAULT 0,
                total_topup INTEGER DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                code      TEXT UNIQUE NOT NULL,
                title     TEXT NOT NULL,
                file_id   TEXT NOT NULL,
                file_type TEXT NOT NULL DEFAULT 'video',
                added_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                amount     INTEGER NOT NULL,
                status     TEXT    DEFAULT 'pending',
                receipt_id TEXT    DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                amount     INTEGER NOT NULL,
                type       TEXT    NOT NULL,
                note       TEXT    DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            )""")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_pu ON payments(user_id)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ps ON payments(status)")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_mc ON movies(code)")
        try:
            await db.execute(
                "ALTER TABLE users ADD COLUMN total_topup INTEGER DEFAULT 0")
        except Exception:
            pass
        # Eski DB da receipt_id ustuni yo'q bo'lishi mumkin
        try:
            await db.execute(
                "ALTER TABLE payments ADD COLUMN receipt_id TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            async with db.execute("PRAGMA table_info(transactions)") as cur:
                cols = [row[1] for row in await cur.fetchall()]
            if "note" not in cols:
                await db.execute(
                    "ALTER TABLE transactions ADD COLUMN note TEXT DEFAULT ''")
        except Exception:
            pass
        # Kinolar ro'yxati uchun e'lon (matn) jadval
        await db.execute("""
            CREATE TABLE IF NOT EXISTS movie_list_text (
                id    INTEGER PRIMARY KEY DEFAULT 1,
                txt   TEXT    DEFAULT ''
            )""")
        # Default qator bo'lmasa qo'shamiz
        await db.execute(
            "INSERT OR IGNORE INTO movie_list_text(id,txt) VALUES(1,'')")
        await db.commit()

# ── yordamchi ─────────────────────────────────────────
def esc(v) -> str:
    return html.escape(str(v or ""))

async def cfg_get(key: str, default: str = "") -> str:
    """Sozlamalarni o'qish (cache bilan)."""
    if key not in _cache["cfg"]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                    "SELECT value FROM settings WHERE key=?", (key,)) as c:
                r = await c.fetchone()
                val = r[0] if r else default
        _cache["cfg"][key] = val
    return _cache["cfg"][key]

async def cfg_set(key: str, val: str):
    """Sozlamalarni yozish va cache invalidate qilish."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, val))
        await db.commit()
    _cache["cfg"][key] = val  # Inline cache update

# ── users ──────────────────────────────────────────────
async def user_upsert(uid: int, uname: str, fname: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id,username,full_name)"
            " VALUES(?,?,?)", (uid, uname, fname))
        await db.commit()

async def user_bal(uid: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT balance FROM users WHERE user_id=?", (uid,)) as c:
            r = await c.fetchone()
            return int(r[0]) if r else 0

async def bal_add(uid: int, amt: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "UPDATE users SET balance=balance+? WHERE user_id=?", (amt, uid))
        await db.commit()

async def bal_deduct(uid: int, amt: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        c = await db.execute(
            "UPDATE users SET balance=balance-?"
            " WHERE user_id=? AND balance>=?", (amt, uid, amt))
        await db.commit()
        return c.rowcount > 0

async def users_all():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM users ORDER BY created_at DESC") as c:
            return await c.fetchall()

async def users_ids() -> List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as c:
            return [int(r[0]) for r in await c.fetchall()]

async def users_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            r = await c.fetchone()
            return int(r[0]) if r else 0

async def total_bal() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT COALESCE(SUM(balance),0) FROM users") as c:
            r = await c.fetchone()
            return int(r[0]) if r else 0

async def total_topup() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM payments"
                " WHERE status='approved'") as c:
            r = await c.fetchone()
            return int(r[0]) if r else 0

# ── movies ─────────────────────────────────────────────
async def movie_save(code: str, title: str, fid: str, ftype: str = "video"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "INSERT OR REPLACE INTO movies(code,title,file_id,file_type)"
            " VALUES(?,?,?,?)", (code, title, fid, ftype))
        await db.commit()
    cache_invalidate("movies")  # Cache invalidate

async def movie_get(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM movies WHERE code=?", (code,)) as c:
            return await c.fetchone()

async def movies_all():
    """Barcha kinolarni qaytarish (keshirish bilan)."""
    import time
    now = time.time()
    # Cache 5 soniyaga valid
    if _cache["movies_all"] and (now - _cache["movies_all_ts"] < 5):
        return _cache["movies_all"]
    
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM movies ORDER BY id") as c:
            result = await c.fetchall()
    _cache["movies_all"] = result
    _cache["movies_all_ts"] = now
    return result

async def movie_del(code: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        c = await db.execute("DELETE FROM movies WHERE code=?", (code,))
        await db.commit()
        ok = c.rowcount > 0
    if ok:
        cache_invalidate("movies")  # Cache invalidate
    return ok

async def ml_text_get() -> str:
    """Kinolar ro'yxati ostidagi e'lon matnini qaytaradi (keshirish bilan)."""
    import time
    now = time.time()
    # Cache 10 soniyaga valid
    if _cache["ml_text"] is not None and (now - _cache["ml_text_ts"] < 10):
        return _cache["ml_text"]
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT txt FROM movie_list_text WHERE id=1") as cur:
            r = await cur.fetchone()
            txt = r[0] if r else ""
    _cache["ml_text"] = txt
    _cache["ml_text_ts"] = now
    return txt

async def ml_text_set(txt: str):
    """Kinolar ro'yxati e'lonini saqlash va cache invalidate qilish."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "INSERT OR REPLACE INTO movie_list_text(id,txt) VALUES(1,?)",
            (txt,))
        await db.commit()
    cache_invalidate("text")  # Cache invalidate

# ── payments ───────────────────────────────────────────
async def pay_new(uid: int, amt: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        c = await db.execute(
            "INSERT INTO payments(user_id,amount,status)"
            " VALUES(?,?,'pending')", (uid, amt))
        await db.commit()
        return int(c.lastrowid) if c.lastrowid else 0

async def pay_set_rcpt(pid: int, fid: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "UPDATE payments SET receipt_id=?"
            " WHERE id=? AND status='pending'", (fid, pid))
        await db.commit()

async def pay_get(pid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM payments WHERE id=?", (pid,)) as c:
            return await c.fetchone()

async def pay_approve(pid: int) -> Optional[Tuple[int, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        async with db.execute(
                "SELECT user_id,amount FROM payments"
                " WHERE id=? AND status='pending'", (pid,)) as c:
            row = await c.fetchone()
            if not row:
                return None
            uid, amt = int(row[0]), int(row[1])
        await db.execute(
            "UPDATE payments SET status='approved' WHERE id=?", (pid,))
        await db.execute(
            "UPDATE users SET balance=balance+?,"
            " total_topup=total_topup+? WHERE user_id=?",
            (amt, amt, uid))
        await db.execute(
            "INSERT INTO transactions(user_id,amount,type,note)"
            " VALUES(?,?,'topup',?)", (uid, amt, f"To'lov #{pid}"))
        await db.commit()
        return uid, amt

async def pay_reject(pid: int) -> Optional[Tuple[int, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        async with db.execute(
                "SELECT user_id,amount FROM payments"
                " WHERE id=? AND status='pending'", (pid,)) as c:
            row = await c.fetchone()
            if not row:
                return None
            uid, amt = int(row[0]), int(row[1])
        await db.execute(
            "UPDATE payments SET status='rejected' WHERE id=?", (pid,))
        await db.commit()
        return uid, amt

async def tx_add(uid: int, amt: int, tp: str, note: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "INSERT INTO transactions(user_id,amount,type,note)"
            " VALUES(?,?,?,?)", (uid, amt, tp, note))
        await db.commit()

# ═══════════════════════════════════════════════════════
# TUGMALAR
# ═══════════════════════════════════════════════════════
def kb_user() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="🎬 Kinolar ro'yxati")
    kb.button(text="💰 Balansim")
    kb.button(text="💳 Balansni to'ldirish")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def kb_admin() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="🎬 Kino qo'shish")
    kb.button(text="📋 Kinolar ro'yxati")
    kb.button(text="📝 Ro'yxat e'loni")
    kb.button(text="✏️ Kinoni tahrirlash")
    kb.button(text="🗑 Kinoni o'chirish")
    kb.button(text="💳 Karta sozlamalari")
    kb.button(text="📊 Statistika")
    kb.button(text="📢 Reklama")
    kb.button(text="👥 Foydalanuvchilar")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def menu(uid: int) -> ReplyKeyboardMarkup:
    return kb_admin() if uid == ADMIN_ID else kb_user()

def kb_amounts() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="5 000 so'm",  callback_data="AMT:5000")
    kb.button(text="10 000 so'm", callback_data="AMT:10000")
    kb.button(text="15 000 so'm", callback_data="AMT:15000")
    kb.button(text="20 000 so'm", callback_data="AMT:20000")
    kb.button(text="❌ Bekor",    callback_data="AMT:cancel")
    kb.adjust(2, 2, 1)
    return kb.as_markup()

def kb_rcpt(pid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📸 Chek rasmini yuborish",
              callback_data=f"RCPT:{pid}")
    kb.button(text="❌ Bekor qilish", callback_data="AMT:cancel")
    kb.adjust(1)
    return kb.as_markup()

def kb_retry(pid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📸 Qayta chek yuborish", callback_data=f"RCPT:{pid}")
    kb.button(text="❌ Bekor",              callback_data="AMT:cancel")
    kb.adjust(1)
    return kb.as_markup()

def kb_low_bal() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Balansni to'ldirish", callback_data="TOPUP:go")
    kb.adjust(1)
    return kb.as_markup()

def kb_pay_admin(pid: int) -> InlineKeyboardMarkup:
    """Adminga chek kelganda — tasdiqlash/bekor tugmalari."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Tasdiqlash",   callback_data=f"APV:{pid}")
    kb.button(text="❌ Bekor qilish", callback_data=f"RJT:{pid}")
    kb.adjust(2)
    return kb.as_markup()

def kb_rej_reasons(pid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Noto'g'ri chek",    callback_data=f"RSN:{pid}:bad_check")
    kb.button(text="❌ Summa mos emas",     callback_data=f"RSN:{pid}:bad_amount")
    kb.button(text="❌ Chek sifati yomon",  callback_data=f"RSN:{pid}:bad_quality")
    kb.button(text="✏️ Boshqa sabab",      callback_data=f"RSN:{pid}:custom")
    kb.button(text="🔙 Orqaga",            callback_data=f"RSN:{pid}:back")
    kb.adjust(1)
    return kb.as_markup()

def kb_ad() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Matn",  callback_data="AD:text")
    kb.button(text="🖼 Rasm",  callback_data="AD:photo")
    kb.button(text="🎥 Video", callback_data="AD:video")
    kb.button(text="❌ Bekor", callback_data="AD:cancel")
    kb.adjust(3, 1)
    return kb.as_markup()

def kb_card(has: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if has:
        kb.button(text="✏️ O'zgartirish", callback_data="CARD:edit")
        kb.button(text="🗑 O'chirish",     callback_data="CARD:del")
    else:
        kb.button(text="➕ Qo'shish", callback_data="CARD:add")
    kb.button(text="❌ Yopish", callback_data="CARD:close")
    kb.adjust(1)
    return kb.as_markup()

def kb_card_del_confirm() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Ha, o'chirish", callback_data="CARD:del_yes")
    kb.button(text="❌ Yo'q",          callback_data="CARD:close")
    kb.adjust(2)
    return kb.as_markup()

def movies_page_markup(movies: list, page: int,
                       is_adm: bool) -> Tuple[str, InlineKeyboardMarkup]:
    total    = len(movies)
    kb       = InlineKeyboardBuilder()
    if total == 0:
        if is_adm:
            text = (
                "🎬 <b>Admin kinolar ro'yxati</b>\n\n"
                "Hozircha kino yo'q. «➕ Kino qo'shish» orqali kino qo'shing."
            )
            kb.button(text="➕ Kino qo'shish", callback_data="ML:add")
        else:
            text = (
                "🎬 <b>Admin tomonidan qo'shilgan kinolar</b>\n\n"
                "Hozircha kino yo'q. Iltimos, keyinroq qayta urinib ko'ring."
            )
        return text, kb.as_markup()

    total_pg = (total - 1) // PAGE_SIZE + 1
    start    = page * PAGE_SIZE
    chunk    = movies[start: start + PAGE_SIZE]

    lines = [f"<code>{esc(m['code'])}</code>  —  {esc(m['title'])}"
             for m in chunk]
    heading = (
        "Admin kinolar ro'yxati" if is_adm
        else "Admin tomonidan qo'shilgan kinolar"
    )
    text = (f"🎬 <b>{heading}</b> ({total} ta)\n"
            f"📄 {page+1}/{total_pg}\n\n"
            + "\n".join(lines))
    if not is_adm:
        text += "\n\n✏️ Kino kodini yozing — kino keladi."

    nav = []
    if page > 0:
        nav.append(("◀️", f"ML:page:{page-1}"))
    if page + 1 < total_pg:
        nav.append(("▶️", f"ML:page:{page+1}"))
    for lbl, cbd in nav:
        kb.button(text=lbl, callback_data=cbd)
    if nav:
        kb.adjust(len(nav))

    if is_adm:
        kb.button(text="➕ Kino qo'shish",   callback_data="ML:add")
        kb.button(text="✏️ Kino tahrirlash",  callback_data="ML:edit")
        kb.button(text="🗑 Kino o'chirish",   callback_data="ML:del")
        kb.adjust(1)

    return text, kb.as_markup()

# ═══════════════════════════════════════════════════════
# ROUTER + MIDDLEWARE
# ═══════════════════════════════════════════════════════
router = Router()

@router.message.outer_middleware()
async def _mw_m(handler, event: Message, data: dict):
    if event.from_user:
        return await handler(event, data)

@router.callback_query.outer_middleware()
async def _mw_c(handler, event: CallbackQuery, data: dict):
    if event.from_user:
        return await handler(event, data)

# ─── /start ───────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    await user_upsert(uid, msg.from_user.username or "",
                      msg.from_user.full_name or "")
    if uid == ADMIN_ID:
        await msg.answer(
            "👑 Xush kelibsiz, <b>Admin</b>!",
            reply_markup=kb_admin())
    else:
        await msg.answer(
            f"👋 Xush kelibsiz, <b>{esc(msg.from_user.full_name)}</b>!\n\n"
            f"🎬 Har bir kino <b>{MOVIE_PRICE} so'm</b>.\n"
            "Kino kodini yozing yoki ro'yxatdan tanlang 👇",
            reply_markup=kb_user())

@router.message(Command("admin"))
async def cmd_admin(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        await msg.answer("⛔ Ruxsat yo'q.")
        return
    await state.clear()
    await msg.answer("👑 <b>Admin panel</b>", reply_markup=kb_admin())

# ─── Balans ───────────────────────────────────────────
@router.message(F.text == "💰 Balansim")
async def show_bal(msg: Message):
    uid = msg.from_user.id
    bal = await user_bal(uid)
    await msg.answer(
        f"💰 <b>Balansingiz:</b> {bal:,} so'm\n"
        f"🎬 Kino narxi: <b>{MOVIE_PRICE} so'm</b>",
        reply_markup=menu(uid))

# ═══════════════════════════════════════════════════════
# KINOLAR RO'YXATI (foydalanuvchi + admin)
# ═══════════════════════════════════════════════════════

@router.message(F.text == "🎬 Kinolar ro'yxati")
async def movies_list(msg: Message):
    uid  = msg.from_user.id
    mvs  = await movies_all()
    is_a = uid == ADMIN_ID
    text, markup = movies_page_markup(mvs, 0, is_a)
    ml = await ml_text_get()
    if ml:
        text = text + "\n\n" + esc(ml)
    await msg.answer(text, reply_markup=markup)

@router.message(F.text == "📋 Kinolar ro'yxati")
async def admin_movies_list(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    mvs  = await movies_all()
    text, markup = movies_page_markup(mvs, 0, True)
    ml = await ml_text_get()
    if ml:
        text = text + "\n\n" + esc(ml)
    await msg.answer(text, reply_markup=markup)

@router.callback_query(F.data.startswith("ML:page:"))
async def movies_nav(call: CallbackQuery):
    page = int(call.data[8:])
    mvs  = await movies_all()
    is_a = call.from_user.id == ADMIN_ID
    text, markup = movies_page_markup(mvs, page, is_a)
    await call.answer()
    try:
        ml = await ml_text_get()
        if ml:
            text = text + "\n\n" + esc(ml)
        await safe_edit(call.message, text, reply_markup=markup)
    except Exception:
        await call.message.answer(text, reply_markup=markup)

@router.callback_query(F.data == "ML:add")
async def ml_add(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Ruxsat yo'q!", show_alert=True)
        return
    await state.set_state(SAddMovie.code)
    await call.answer()
    await call.message.answer(
        "🎬 <b>Kino qo'shish</b>\n\n"
        "1️⃣ Kino <b>kodini</b> yozing (masalan: <code>001</code>):")


@router.message(F.text == "📝 Ro'yxat e'loni")
async def ml_text_edit(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    await state.set_state(SMLText.text)
    cur = await ml_text_get()
    hint = f"\nHozirgi matn:\n{cur}\n\n" if cur else "\n"
    await msg.answer(
        "📝 <b>Kinolar ro'yxati e'lonini</b> yozing:" + hint)


@router.message(SMLText.text)
async def ml_text_save(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    txt = (msg.text or "").strip()
    await ml_text_set(txt)
    await state.clear()
    await msg.answer("✅ <b>E'lon yangilandi!</b>", reply_markup=kb_admin())

@router.callback_query(F.data == "ML:edit")
async def ml_edit(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Ruxsat yo'q!", show_alert=True)
        return
    await state.set_state(SEditMovie.choose)
    await call.answer()
    await call.message.answer(
        "✏️ Tahrirlash uchun kino <b>kodini</b> yozing:")

@router.callback_query(F.data == "ML:del")
async def ml_del_cb(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Ruxsat yo'q!", show_alert=True)
        return
    await state.set_state(SDelMovie.code)
    await call.answer()
    await call.message.answer("🗑 O'chirish uchun kino <b>kodini</b> yozing:")

# ═══════════════════════════════════════════════════════
# TO'LOV TIZIMI
# ═══════════════════════════════════════════════════════

@router.message(F.text == "💳 Balansni to'ldirish")
async def topup_start(msg: Message, state: FSMContext):
    if msg.from_user.id == ADMIN_ID:
        await msg.answer("Admin foydalanuvchi balansni to'ldira olmaydi.",
                         reply_markup=kb_admin())
        return
    await state.clear()
    await _show_amounts(msg, state)

@router.callback_query(F.data == "TOPUP:go")
async def topup_go(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await _show_amounts(call.message, state, edit=True)

async def _show_amounts(target, state: FSMContext, edit: bool = False):
    card = await cfg_get("card_number")
    if not card:
        txt = (
            "⚠️ Hozircha to'lov qabul qilinmayapti.\n"
            "Admin hali karta sozlamagan bo'lishi mumkin.\n"
            "Iltimos, keyinroq qayta urinib ko'ring."
        )
        if edit:
            try:
                await safe_edit(target, txt, reply_markup=kb_user())
                return
            except Exception:
                pass
        await target.answer(txt, reply_markup=kb_user())
        return
    await state.set_state(STopup.amount)
    txt = "💳 <b>Qancha to'ldirmoqchisiz?</b>"
    if edit:
        try:
            await safe_edit(target, txt, reply_markup=kb_amounts())
            return
        except Exception:
            pass
    await target.answer(txt, reply_markup=kb_amounts())

# Miqdor tanlash — FAQAT STopup.amount holatida
@router.callback_query(STopup.amount, F.data.startswith("AMT:"))
async def topup_amount(call: CallbackQuery, state: FSMContext):
    val = call.data[4:]
    if val == "cancel":
        await state.clear()
        await call.answer("Bekor qilindi")
        try:
            await call.message.edit_text("❌ Bekor qilindi.")
        except Exception:
            pass
        await call.message.answer("Asosiy menyu:", reply_markup=kb_user())
        return
    if not val.isdigit():
        await call.answer()
        return
    amount = int(val)
    card   = await cfg_get("card_number")
    if not card:
        await call.answer("⚠️ To'lov qabul qilinmayapti!", show_alert=True)
        await state.clear()
        return
    pid = await pay_new(call.from_user.id, amount)
    if not pid:
        await call.answer("⚠️ Xatolik! Qayta urinib ko'ring.", show_alert=True)
        return
    holder = await cfg_get("card_holder")
    h_line = f"\n👤 Karta egasi: <b>{esc(holder)}</b>" if holder else ""
    await state.update_data(pid=pid, amount=amount)
    await state.set_state(STopup.receipt)
    await call.answer()
    await safe_edit(call.message,
        f"💳 <b>To'lov ma'lumotlari:</b>\n\n"
        f"💰 Summa: <b>{amount:,} so'm</b>\n"
        f"🏦 Karta: <code>{card}</code>"
        f"{h_line}\n\n"
        "⬆️ Kartaga pul o'tkizing, so'ng\n"
        "«📸 Chek rasmini yuborish» tugmasini bosing.",
        reply_markup=kb_rcpt(pid))

# "📸 Chek" tugmasi — FAQAT STopup.receipt holatida
@router.callback_query(STopup.receipt, F.data.startswith("RCPT:"))
async def rcpt_btn(call: CallbackQuery):
    await call.answer()
    try:
        await call.message.edit_text(
            "📸 <b>Chek rasmini yuboring:</b>\n\n"
            "To'lov chekining screenshot rasmini yuboring.\n"
            "Bekor qilish: /start")
    except Exception:
        pass

# Bekor qilish — STopup.receipt holatida
@router.callback_query(STopup.receipt, F.data == "AMT:cancel")
async def topup_cancel_rcpt(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("Bekor qilindi")
    try:
        await safe_edit(call.message, "❌ Bekor qilindi.")
    except Exception:
        pass
    await call.message.answer("Asosiy menyu:", reply_markup=kb_user())

# Chek rasmi — FAQAT STopup.receipt holatida
@router.message(STopup.receipt, F.photo | F.document)
async def rcpt_photo(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    pid  = data.get("pid")
    if not pid:
        await state.clear()
        await msg.answer("⚠️ Sessiya tugadi. /start bosing.",
                         reply_markup=kb_user())
        return
    pay = await pay_get(pid)
    if pay and pay["receipt_id"]:
        await msg.answer(
            "ℹ️ Bu to'lov uchun chek allaqachon yuborilgan.\n"
            "Admin tekshirmoqda — kuting.",
            reply_markup=kb_user())
        await state.clear()
        return
    is_photo = bool(msg.photo)
    if is_photo:
        fid = msg.photo[-1].file_id
    else:
        if not (msg.document.mime_type or "").startswith("image/"):
            await msg.answer(
                "⚠️ Iltimos, faqat rasm (screenshot) yuboring!\n"
                "Agar fayl sifatida yuborgan bo'lsangiz, qaytadan sinab ko'ring.",
                reply_markup=kb_retry(pid))
            return
        fid = msg.document.file_id
    amount = int(pay["amount"]) if pay else data.get("amount", 0)
    await pay_set_rcpt(pid, fid)
    await state.clear()
    await msg.answer(
        "✅ <b>Chekingiz qabul qilindi!</b>\n\n"
        "⏳ Admin tekshirib tasdiqlaydi.\n"
        f"💰 Summa: <b>{amount:,} so'm</b>",
        reply_markup=kb_user())
    try:
        u = msg.from_user
        caption = (
            f"💳 <b>Yangi to'lov so'rovi</b>\n\n"
            f"👤 <a href='tg://user?id={u.id}'>{esc(u.full_name)}</a>\n"
            f"🆔 <code>{u.id}</code>\n"
            f"💰 <b>{amount:,} so'm</b>\n"
            f"🔖 To'lov ID: <code>{pid}</code>"
        )
        if is_photo:
            await bot.send_photo(
                chat_id=ADMIN_ID, photo=fid,
                caption=caption,
                reply_markup=kb_pay_admin(pid))
        else:
            await bot.send_document(
                chat_id=ADMIN_ID, document=fid,
                caption=caption,
                reply_markup=kb_pay_admin(pid))
    except Exception as e:
        log.exception("Admin ga chek: %s", e)

# Noto'g'ri fayl — STopup.receipt holatida
@router.message(STopup.receipt)
async def rcpt_wrong(msg: Message, state: FSMContext):
    data = await state.get_data()
    pid  = int(data.get("pid") or 0)
    await msg.answer(
        "⚠️ Faqat <b>rasm (screenshot)</b> yuboring!\n"
        "Bekor qilish: /start",
        reply_markup=kb_retry(pid) if pid else None)

# ═══════════════════════════════════════════════════════
# ADMIN — TO'LOV TASDIQLASH / BEKOR QILISH
# ═══════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("APV:"))
async def cb_approve(call: CallbackQuery, bot: Bot):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Ruxsat yo'q!", show_alert=True)
        return
    pid    = int(call.data[4:])
    result = await pay_approve(pid)
    if not result:
        await call.answer(
            "⚠️ Topilmadi yoki allaqachon ko'rib chiqilgan!",
            show_alert=True)
        return
    uid, amt = result
    # Tugmalarni o'chirish, caption yangilash
    try:
        new_cap = (call.message.caption or "") + "\n\n✅ <b>TASDIQLANDI</b>"
        await call.message.edit_caption(
            new_cap, parse_mode="HTML", reply_markup=None)
    except Exception:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await call.answer("✅ Tasdiqlandi!")
    try:
        await bot.send_message(
            uid,
            f"✅ <b>To'lovingiz tasdiqlandi!</b>\n\n"
            f"💰 Balansingizga <b>{amt:,} so'm</b> qo'shildi. 🎬",
            reply_markup=kb_user())
    except Exception as e:
        log.exception("Foydalanuvchiga tasdiqlash xabari: %s", e)

@router.callback_query(F.data.startswith("RJT:"))
async def cb_reject(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Ruxsat yo'q!", show_alert=True)
        return
    pid = int(call.data[4:])
    await call.answer()
    await call.message.answer(
        f"❌ To'lov #{pid} — sabab tanlang:",
        reply_markup=kb_rej_reasons(pid))

@router.callback_query(F.data.startswith("RSN:"))
async def cb_reason(call: CallbackQuery, state: FSMContext, bot: Bot):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Ruxsat yo'q!", show_alert=True)
        return
    # "RSN:{pid}:{key}"
    parts = call.data.split(":", 2)
    pid   = int(parts[1])
    key   = parts[2]
    if key == "back":
        await call.answer()
        try:
            await call.message.delete()
        except Exception:
            pass
        return
    if key == "custom":
        await state.update_data(rej_pid=pid)
        await state.set_state(SReject.reason)
        await call.answer()
        await call.message.edit_text("✏️ Bekor qilish sababini yozing:")
        return
    reasons = {
        "bad_check":   "Noto'g'ri chek yuborilgan",
        "bad_amount":  "To'lov summasi mos emas",
        "bad_quality": "Chek sifati yomon, aniq emas",
    }
    reason = reasons.get(key, "Bekor qilindi")
    await call.answer()
    await _do_reject(call.message, bot, pid, reason)

@router.message(SReject.reason)
async def reject_custom(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id != ADMIN_ID:
        return
    data   = await state.get_data()
    pid    = int(data.get("rej_pid", 0))
    reason = (msg.text or "").strip() or "Sabab ko'rsatilmadi"
    await state.clear()
    if not pid:
        await msg.answer("⚠️ Xatolik.", reply_markup=kb_admin())
        return
    await _do_reject(msg, bot, pid, reason)

async def _do_reject(target, bot: Bot, pid: int, reason: str):
    result = await pay_reject(pid)
    if not result:
        txt = "⚠️ To'lov topilmadi yoki allaqachon ko'rib chiqilgan!"
        if isinstance(target, Message):
            await target.answer(txt, reply_markup=kb_admin())
        else:
            try:
                await target.edit_text(txt)
            except Exception:
                pass
        return
    uid, amt = result
    summ = f"❌ To'lov #{pid} bekor qilindi.\n📝 Sabab: {esc(reason)}"
    if isinstance(target, Message):
        await target.answer(summ, reply_markup=kb_admin())
    else:
        try:
            await target.edit_text(summ)
        except Exception:
            pass
    try:
        await bot.send_message(
            uid,
            f"❌ <b>To'lovingiz bekor qilindi.</b>\n\n"
            f"💰 Miqdor: <b>{amt:,} so'm</b>\n"
            f"📝 Sabab: {esc(reason)}\n\n"
            "Qayta to'lov qilishingiz mumkin. 💳",
            reply_markup=kb_user())
    except Exception as e:
        log.warning("Foydalanuvchiga bekor xabari: %s", e)

# ═══════════════════════════════════════════════════════
# ADMIN — KINO QO'SHISH
# ═══════════════════════════════════════════════════════

@router.message(F.text == "🎬 Kino qo'shish")
async def add_movie_btn(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    await state.set_state(SAddMovie.code)
    await msg.answer(
        "🎬 <b>Kino qo'shish</b>\n\n"
        "1️⃣ Kino <b>kodini</b> yozing (masalan: <code>001</code>):")

@router.message(SAddMovie.code)
async def add_code(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    code = (msg.text or "").strip()
    if not code:
        await msg.answer("⚠️ Kod bo'sh bo'lmasin. Qayta yozing:")
        return
    await state.update_data(code=code)
    await state.set_state(SAddMovie.title)
    await msg.answer(
        f"✅ Kod: <code>{esc(code)}</code>\n\n"
        "2️⃣ Kino <b>nomini</b> yozing:")

@router.message(SAddMovie.title)
async def add_title(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    title = (msg.text or "").strip()
    if not title:
        await msg.answer("⚠️ Nom bo'sh bo'lmasin. Qayta yozing:")
        return
    await state.update_data(title=title)
    await state.set_state(SAddMovie.file)
    await msg.answer(
        f"✅ Nom: <b>{esc(title)}</b>\n\n"
        "3️⃣ Kino <b>video faylini</b> yuboring (video yoki dokument):")

@router.message(SAddMovie.file, F.video | F.document)
async def add_file(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    data  = await state.get_data()
    code  = data.get("code", "")
    title = data.get("title", "")
    if not code or not title:
        await state.clear()
        await msg.answer("⚠️ Xatolik. /start bosing.", reply_markup=kb_admin())
        return
    fid, ftype = (msg.video.file_id, "video") if msg.video \
        else (msg.document.file_id, "document")
    await movie_save(code, title, fid, ftype)
    await state.clear()
    await msg.answer(
        f"✅ <b>Kino qo'shildi!</b>\n"
        f"🔖 Kod: <code>{esc(code)}</code>\n"
        f"🎬 Nom: <b>{esc(title)}</b>",
        reply_markup=kb_admin())

@router.message(SAddMovie.file)
async def add_file_wrong(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer("⚠️ <b>Video yoki dokument</b> fayl yuboring!")

# ═══════════════════════════════════════════════════════
# ADMIN — KINO TAHRIRLASH
# ═══════════════════════════════════════════════════════

@router.message(F.text == "✏️ Kinoni tahrirlash")
async def edit_movie_btn(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    await state.set_state(SEditMovie.choose)
    await msg.answer("✏️ Tahrirlash uchun kino <b>kodini</b> yozing:")

@router.message(SEditMovie.choose)
async def edit_choose(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    code  = (msg.text or "").strip()
    movie = await movie_get(code)
    if not movie:
        await msg.answer(
            f"❌ <code>{esc(code)}</code> topilmadi. Qayta yozing:")
        return
    await state.update_data(
        ec=code, ot=movie["title"],
        of=movie["file_id"], oft=movie["file_type"])
    await state.set_state(SEditMovie.title)
    await msg.answer(
        f"🎬 <b>{esc(movie['title'])}</b> (<code>{esc(code)}</code>)\n\n"
        "Yangi nom yozing.\n"
        "O'zgartirmaslik uchun <code>-</code> yozing:")

@router.message(SEditMovie.title)
async def edit_title(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    inp  = (msg.text or "").strip()
    data = await state.get_data()
    new_t = inp if inp and inp != "-" else data["ot"]
    await state.update_data(nt=new_t)
    await state.set_state(SEditMovie.file)
    await msg.answer(
        "Yangi video faylini yuboring.\n"
        "O'zgartirmaslik uchun <code>-</code> yozing:")

@router.message(SEditMovie.file, F.text)
async def edit_file_skip(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    if (msg.text or "").strip() != "-":
        await msg.answer(
            "⚠️ Video fayl yuboring yoki <code>-</code> yozing:")
        return
    data = await state.get_data()
    await movie_save(data["ec"], data["nt"], data["of"], data["oft"])
    await state.clear()
    await msg.answer(
        f"✅ <code>{esc(data['ec'])}</code> yangilandi!\n"
        f"🎬 Nom: <b>{esc(data['nt'])}</b>",
        reply_markup=kb_admin())

@router.message(SEditMovie.file, F.video | F.document)
async def edit_file_new(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    data  = await state.get_data()
    fid, ftype = (msg.video.file_id, "video") if msg.video \
        else (msg.document.file_id, "document")
    await movie_save(data["ec"], data["nt"], fid, ftype)
    await state.clear()
    await msg.answer(
        f"✅ <code>{esc(data['ec'])}</code> yangilandi!\n"
        f"🎬 Nom: <b>{esc(data['nt'])}</b>",
        reply_markup=kb_admin())

@router.message(SEditMovie.file)
async def edit_file_wrong(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer(
        "⚠️ Video fayl yuboring yoki <code>-</code> yozing:")

# ═══════════════════════════════════════════════════════
# ADMIN — KINO O'CHIRISH
# ═══════════════════════════════════════════════════════

@router.message(F.text == "🗑 Kinoni o'chirish")
async def del_movie_btn(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    await state.set_state(SDelMovie.code)
    await msg.answer("🗑 O'chirish uchun kino <b>kodini</b> yozing:")

@router.message(SDelMovie.code)
async def del_movie(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    code = (msg.text or "").strip()
    ok   = await movie_del(code)
    await state.clear()
    if ok:
        await msg.answer(
            f"✅ <code>{esc(code)}</code> o'chirildi.",
            reply_markup=kb_admin())
    else:
        await msg.answer(
            f"❌ <code>{esc(code)}</code> topilmadi.",
            reply_markup=kb_admin())

# ═══════════════════════════════════════════════════════
# ADMIN — KARTA SOZLAMALARI
# ═══════════════════════════════════════════════════════

@router.message(F.text == "💳 Karta sozlamalari")
async def card_settings(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    await state.clear()
    card   = await cfg_get("card_number")
    holder = await cfg_get("card_holder")
    if card:
        h = f"\n👤 Egasi: <b>{esc(holder)}</b>" if holder else ""
        text = f"💳 <b>Joriy karta:</b>\n🔢 <code>{card}</code>{h}"
    else:
        text = "💳 <b>Karta raqam qo'shilmagan.</b>"
    await msg.answer(text, reply_markup=kb_card(bool(card)))

@router.callback_query(F.data.in_({"CARD:add", "CARD:edit"}))
async def card_add_edit(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    await state.set_state(SCard.inp)
    await call.answer()
    await safe_edit(call.message,
        "🔢 Karta raqamini yozing:\n\n"
        "Misol: <code>9860 1701 1234 5678</code>\n\n"
        "Karta egasini ham qo'shish uchun ikkinchi qatorda:\n"
        "<code>9860 1701 1234 5678\nAlisher Karimov</code>")

@router.message(SCard.inp)
async def card_input(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    lines  = (msg.text or "").strip().splitlines()
    card   = lines[0].strip()
    holder = lines[1].strip() if len(lines) > 1 else ""
    digits = card.replace(" ", "").replace("-", "")
    if not digits.isdigit() or len(digits) < 12:
        await msg.answer(
            "⚠️ Noto'g'ri karta raqami!\n"
            "Misol: <code>9860 1701 1234 5678</code>")
        return
    await cfg_set("card_number", card)
    await cfg_set("card_holder", holder)
    await state.clear()
    h = f"\n👤 Egasi: <b>{esc(holder)}</b>" if holder else ""
    await msg.answer(
        f"✅ <b>Karta saqlandi!</b>\n🔢 <code>{card}</code>{h}",
        reply_markup=kb_admin())

@router.callback_query(F.data == "CARD:del")
async def card_del_ask(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    await call.answer()
    await safe_edit(call.message,
        "🗑 Kartani o'chirishni tasdiqlaysizmi?",
        reply_markup=kb_card_del_confirm())

@router.callback_query(F.data == "CARD:del_yes")
async def card_del_yes(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer()
        return
    await cfg_set("card_number", "")
    await cfg_set("card_holder", "")
    await call.answer("✅ O'chirildi!")
    try:
        await safe_edit(call.message,
            "🗑 Karta o'chirildi.\n"
            "«💳 Karta sozlamalari» orqali yangi qo'shing.")
    except Exception:
        pass

@router.callback_query(F.data == "CARD:close")
async def card_close(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass

# ═══════════════════════════════════════════════════════
# ADMIN — STATISTIKA
# ═══════════════════════════════════════════════════════

@router.message(F.text == "📊 Statistika")
async def statistics(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    cnt  = await users_count()
    tb   = await total_bal()
    tt   = await total_topup()
    mvs  = await movies_all()
    card = await cfg_get("card_number")
    cs   = f"<code>{card}</code>" if card else "❌ Qo'shilmagan"
    await msg.answer(
        f"📊 <b>Bot statistikasi:</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{cnt:,}</b>\n"
        f"🎬 Kinolar: <b>{len(mvs)}</b>\n"
        f"💳 Jami to'ldirilgan: <b>{tt:,} so'm</b>\n"
        f"💰 Joriy umumiy balans: <b>{tb:,} so'm</b>\n\n"
        f"🏦 Faol karta: {cs}",
        reply_markup=kb_admin())

# ═══════════════════════════════════════════════════════
# ADMIN — FOYDALANUVCHILAR
# ═══════════════════════════════════════════════════════

@router.message(F.text == "👥 Foydalanuvchilar")
async def show_users(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    users = await users_all()
    if not users:
        await msg.answer("Hali foydalanuvchilar yo'q.", reply_markup=kb_admin())
        return
    lines = []
    for u in users[:50]:
        uname = f"@{u['username']}" if u["username"] else "—"
        lines.append(
            f"• <a href='tg://user?id={u['user_id']}'>"
            f"{esc(u['full_name'] or 'Nomsiz')}</a>"
            f" | {uname} | 💰 {u['balance']:,} so'm")
    header = f"👥 <b>Foydalanuvchilar ({len(users)} ta):</b>\n\n"
    footer = f"\n... va yana {len(users)-50} ta" if len(users) > 50 else ""
    full   = header + "\n".join(lines) + footer
    if len(full) <= 4096:
        await msg.answer(full, reply_markup=kb_admin())
    else:
        chunk = header
        for line in lines:
            if len(chunk) + len(line) + 1 > 4000:
                await msg.answer(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            await msg.answer(chunk, reply_markup=kb_admin())

# ═══════════════════════════════════════════════════════
# ADMIN — REKLAMA
# ═══════════════════════════════════════════════════════

@router.message(F.text == "📢 Reklama")
async def ad_menu(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer("📢 <b>Reklama turini tanlang:</b>",
                     reply_markup=kb_ad())

@router.callback_query(F.data == "AD:text")
async def ad_text_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(SAd.text)
    await call.answer()
    await safe_edit(call.message, "✏️ Reklama <b>matnini</b> yozing:")

@router.callback_query(F.data == "AD:photo")
async def ad_photo_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(SAd.photo)
    await call.answer()
    await safe_edit(call.message,
        "🖼 Reklama <b>rasmini</b> yuboring (caption bilan yoki ustirsiz):")

@router.callback_query(F.data == "AD:video")
async def ad_video_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(SAd.video)
    await call.answer()
    await safe_edit(call.message,
        "🎥 Reklama <b>videosini</b> yuboring (caption bilan yoki ustirsiz):")

@router.callback_query(F.data == "AD:cancel")
async def ad_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("Bekor qilindi")
    try:
        await safe_edit(call.message, "❌ Bekor qilindi.")
    except Exception:
        pass

@router.message(SAd.text)
async def ad_send_text(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id != ADMIN_ID:
        return
    await state.clear()
    text = msg.text or ""
    uids = await users_ids()
    st   = await msg.answer(f"⏳ Yuborilmoqda... (0/{len(uids)})")
    async def _s(uid): await bot.send_message(uid, text, parse_mode="HTML")
    ok, fail = await _broadcast(bot, uids, _s)
    try:
        await st.edit_text(f"📢 <b>Yuborildi!</b>\n✅ {ok}  ❌ {fail}")
    except Exception:
        pass
    await msg.answer("✅ Tayyor.", reply_markup=kb_admin())

@router.message(SAd.photo, F.photo)
async def ad_send_photo(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id != ADMIN_ID:
        return
    await state.clear()
    fid  = msg.photo[-1].file_id
    cap  = msg.caption or ""
    uids = await users_ids()
    st   = await msg.answer(f"⏳ Yuborilmoqda... (0/{len(uids)})")
    async def _s(uid): await bot.send_photo(uid, fid, caption=cap, parse_mode="HTML")
    ok, fail = await _broadcast(bot, uids, _s)
    try:
        await st.edit_text(f"📢 <b>Rasm yuborildi!</b>\n✅ {ok}  ❌ {fail}")
    except Exception:
        pass
    await msg.answer("✅ Tayyor.", reply_markup=kb_admin())

@router.message(SAd.photo)
async def ad_photo_wrong(msg: Message):
    await msg.answer("⚠️ Iltimos, <b>rasm</b> yuboring!")

@router.message(SAd.video, F.video)
async def ad_send_video(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id != ADMIN_ID:
        return
    await state.clear()
    vid  = msg.video.file_id
    cap  = msg.caption or ""
    uids = await users_ids()
    st   = await msg.answer(f"⏳ Yuborilmoqda... (0/{len(uids)})")
    async def _s(uid): await bot.send_video(uid, vid, caption=cap, parse_mode="HTML")
    ok, fail = await _broadcast(bot, uids, _s)
    try:
        await st.edit_text(f"📢 <b>Video yuborildi!</b>\n✅ {ok}  ❌ {fail}")
    except Exception:
        pass
    await msg.answer("✅ Tayyor.", reply_markup=kb_admin())

@router.message(SAd.video)
async def ad_video_wrong(msg: Message):
    await msg.answer("⚠️ Iltimos, <b>video</b> yuboring!")

# ═══════════════════════════════════════════════════════
# FOYDALANUVCHI — KOD ORQALI KINO OLISH
# (OXIRGI HANDLER — barcha boshqalardan keyin!)
# ═══════════════════════════════════════════════════════

@router.message(F.text)
async def catch_code(msg: Message, state: FSMContext, bot: Bot):
    uid = msg.from_user.id
    if uid == ADMIN_ID:
        return
    cur = await state.get_state()
    if cur is not None:
        return
    code  = msg.text.strip()
    movie = await movie_get(code)
    if not movie:
        await msg.answer(
            f"❌ <code>{esc(code)}</code> kodli kino topilmadi.\n\n"
            "«🎬 Kinolar ro'yxati» tugmasini bosib ro'yxatni ko'ring.",
            reply_markup=kb_user())
        return
    await _give_movie(msg, bot, uid, movie)

async def _give_movie(msg: Message, bot: Bot, uid: int, movie):
    bal = await user_bal(uid)
    if bal < MOVIE_PRICE:
        await msg.answer(
            f"💸 <b>Balansingiz yetarli emas!</b>\n\n"
            f"💰 Balans: <b>{bal:,} so'm</b>\n"
            f"🎬 Kino narxi: <b>{MOVIE_PRICE:,} so'm</b>",
            reply_markup=kb_low_bal())
        return
    ok = await bal_deduct(uid, MOVIE_PRICE)
    if not ok:
        await msg.answer(
            "⚠️ Balansdan yechishda xatolik. Qayta urinib ko'ring.",
            reply_markup=kb_user())
        return
    await tx_add(uid, MOVIE_PRICE, "watch",
                 f"{movie['title']} ({movie['code']})")
    new_bal = await user_bal(uid)
    caption = (
        f"🎬 <b>{esc(movie['title'])}</b>\n"
        f"🔖 Kod: <code>{esc(movie['code'])}</code>\n\n"
        f"💰 Yechildi: <b>{MOVIE_PRICE:,} so'm</b>\n"
        f"💳 Qolgan balans: <b>{new_bal:,} so'm</b>")
    try:
        if movie["file_type"] == "document":
            try:
                await bot.send_document(
                    msg.chat.id, movie["file_id"],
                    caption=caption)
            except Exception:
                await bot.send_video(
                    msg.chat.id, movie["file_id"],
                    caption=caption)
        else:
            try:
                await bot.send_video(
                    msg.chat.id, movie["file_id"],
                    caption=caption)
            except Exception:
                await bot.send_document(
                    msg.chat.id, movie["file_id"],
                    caption=caption)
    except Exception as e:
        error_str = str(e)
        log.error("Kino yuborishda xato: %s", error_str)
        
        # File ID expired - notify admin to re-upload
        if "wrong remote file identifier" in error_str.lower():
            log.warning(f"File ID expired for movie {movie['code']}")
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ <b>Film ID muddati o'tib ketdi!</b>\n\n"
                f"📌 Film: {esc(movie['title'])} ({movie['code']})\n"
                f"Iltimos qaytadan yuklab bering.",
                parse_mode=ParseMode.HTML)
            await msg.answer(
                "⚠️ Film vaqtinchalik mavjud emas. Admin tez orada bartaraf etadi.",
                reply_markup=kb_user())
        else:
            await msg.answer(
                "⚠️ Kino yuborishda xatolik. Balansingiz qaytarildi.",
                reply_markup=kb_user())
        
        # Refund balance
        await bal_add(uid, MOVIE_PRICE)
        await tx_add(uid, MOVIE_PRICE, "refund", f"Qaytarildi: {movie['code']}")

# ═══════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════

async def main():
    await db_init()
    log.info("DB tayyor.")
    bot = Bot(token=BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp  = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    log.info("Bot ishga tushmoqda...")
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(
            bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        log.info("Bot to'xtatildi.")

if __name__ == "__main__":
    asyncio.run(main())
