"""
SECRET SOCIETY Telegram Bot — MVP
Python 3.10+
Install:
    pip install "aiogram>=3.25" aiosqlite pillow
Run:
    export BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
    export ADMIN_ID="YOUR_TELEGRAM_NUMERIC_ID"
    python secret_society_bot.py

Features:
- Admin panel inside Telegram (missions, XP, announcements, stats, reward review)
- SQLite persistence
- Ranks and XP
- Mission list and mission completion (admin-approved)
- Reward application workflow to admin private chat
- Generated classified-dossier cover image for each mission (Pillow)
- Admin can also attach a custom image to a mission
"""

import asyncio
import os
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from PIL import Image, ImageDraw, ImageFont
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # مالک اصلی
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "").strip()
DB_PATH = "secret_society.sqlite3"
ASSET_DIR = Path("mission_assets")
ASSET_DIR.mkdir(exist_ok=True)

if not BOT_TOKEN or not ADMIN_ID:
    raise RuntimeError("Set BOT_TOKEN and ADMIN_ID environment variables first.")

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

RANKS = [
    ("Initiate", 0),
    ("Operative", 100),
    ("Phantom", 500),
    ("Elite", 1500),
    ("Shadow", 3000),
    ("Candidate", 5000),
    ("Council", 10000),
]

class AdminFlow(StatesGroup):
    mission_title = State()
    mission_description = State()
    mission_reward_xp = State()
    mission_photo = State()
    xp_user_id = State()
    xp_amount = State()
    announce_text = State()
    channel_announce_text = State()
    channel_link = State()
    text_value = State()
    add_admin_id = State()
    mission_submission = State()

async def is_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT user_id FROM admins WHERE user_id=?", (user_id,))).fetchone()
    return bool(row)

async def is_owner(user_id: int) -> bool:
    return user_id == ADMIN_ID

async def admin_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("SELECT user_id FROM admins")).fetchall()
    return list({ADMIN_ID, *(r[0] for r in rows)})

def rank_for(xp: int) -> str:
    current = RANKS[0][0]
    for name, threshold in RANKS:
        if xp >= threshold:
            current = name
    return current

def rank_progress(xp: int):
    current_idx = 0
    for i, (_, threshold) in enumerate(RANKS):
        if xp >= threshold:
            current_idx = i
    current_name, current_threshold = RANKS[current_idx]
    next_rank = RANKS[current_idx + 1] if current_idx + 1 < len(RANKS) else None
    if next_rank:
        next_name, next_threshold = next_rank
        pct = int((xp - current_threshold) / max(1, next_threshold - current_threshold) * 100)
        return current_name, next_name, next_threshold, max(0, min(100, pct))
    return current_name, None, None, 100

def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_kb():
    return kb([
        [InlineKeyboardButton(text="➕ ساخت مأموریت", callback_data="admin:newmission", style="success"),
         InlineKeyboardButton(text="📋 مأموریت‌ها", callback_data="admin:missions", style="primary")],
        [InlineKeyboardButton(text="🎁 درخواست جایزه‌ها", callback_data="admin:rewards", style="success"),
         InlineKeyboardButton(text="⭐ دادن XP", callback_data="admin:xp", style="primary")],
        [InlineKeyboardButton(text="📢 پیام همگانی", callback_data="admin:announce", style="primary"),
         InlineKeyboardButton(text="📣 پیام به کانال", callback_data="admin:channelannounce", style="primary")],
        [InlineKeyboardButton(text="✏️ ویرایش متن‌ها", callback_data="admin:edittexts", style="primary"),
         InlineKeyboardButton(text="🔗 تغییر لینک کانال", callback_data="admin:channellink", style="primary")],
        [InlineKeyboardButton(text="👑 ادمین‌ها", callback_data="admin:admins", style="success"),
         InlineKeyboardButton(text="📊 آمار ربات", callback_data="admin:stats", style="primary")],
    ])

def user_kb(user_is_admin=False):
    rows = [
        [InlineKeyboardButton(text="🚀 شروع بازی", callback_data="user:missions", style="success"),
         InlineKeyboardButton(text="📖 راهنما", callback_data="user:guide", style="primary")],
        [InlineKeyboardButton(text="🗂 مأموریت‌ها", callback_data="user:missions", style="primary"),
         InlineKeyboardButton(text="🪪 پروفایل", callback_data="user:profile", style="primary")],
        [InlineKeyboardButton(text="🎁 درخواست بررسی جایزه", callback_data="user:reward", style="success")],
        [InlineKeyboardButton(text="🏆 رتبه‌بندی", callback_data="user:leaderboard", style="primary")],
        [InlineKeyboardButton(text="🔗 دعوت دوستان", callback_data="user:referral", style="success"),
         InlineKeyboardButton(text="🎁 جایزه روزانه", callback_data="user:daily", style="success")],
    ]
    if user_is_admin:
        rows.append([InlineKeyboardButton(text="🛠 پنل مدیریت", callback_data="admin:openpanel", style="primary")])
    else:
        rows.append([InlineKeyboardButton(text="🛠 پنل", callback_data="admin:openpanel", style="primary")])
    return kb(rows)

TEXT_DEFAULTS = {
    "welcome": ("🕶️ <b>به SECRET SOCIETY خوش آمدی</b>\n"
                "📁 پرونده عضویتت ساخته شد.\n"
                "🎯 مأموریت‌ها را انجام بده، XP جمع کن و رتبه‌ات را ارتقا بده.\n"
                "از منوی زیر شروع کن."),
    "guide": ("📖 <b>راهنمای بازی SECRET SOCIETY</b>\n"
              "🚀 از دکمه شروع بازی وارد بخش مأموریت‌ها شو.\n"
              "🗂 مأموریت‌ها را باز کن و توضیحات هر مأموریت را بخوان.\n"
              "📩 پاسخ یا مدرک انجام مأموریت را بفرست تا مدیر بررسی کند.\n"
              "⭐ بعد از تأیید، XP می‌گیری و رتبه‌ات بالاتر می‌رود.\n"
              "🎁 با جمع‌کردن حداقل ۵۰۰۰ XP می‌توانی درخواست جایزه ثبت کنی.\n"
              "🔗 با دعوت دوستان و دریافت پاداش روزانه هم XP بیشتری جمع کن."),
}

async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT value FROM settings WHERE key=?", (key,))).fetchone()
    return row[0] if row else default

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await db.commit()

async def channel_destination():
    link = (await get_setting("channel_link", CHANNEL_LINK)).strip()
    if not link:
        return None
    if link.startswith("@"):
        return link
    clean = link.rstrip("/")
    if "t.me/" in clean:
        tail = clean.split("t.me/", 1)[1].split("?", 1)[0].strip("/")
        if tail and not tail.startswith("+") and not tail.startswith("joinchat/"):
            return "@" + tail
    return None

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            xp INTEGER DEFAULT 0, created_at TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS missions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, description TEXT,
            xp INTEGER DEFAULT 100, image_path TEXT, active INTEGER DEFAULT 1,
            created_at TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS completions(
            user_id INTEGER, mission_id INTEGER, status TEXT DEFAULT 'pending',
            submitted_at TEXT, PRIMARY KEY(user_id, mission_id))""")
        await db.execute("""CREATE TABLE IF NOT EXISTS reward_requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, status TEXT DEFAULT 'pending',
            created_at TEXT)""")
        await db.execute("CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, added_by INTEGER, added_at TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        await db.execute("CREATE TABLE IF NOT EXISTS referrals(invitee_id INTEGER PRIMARY KEY, inviter_id INTEGER, created_at TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS referral_bonus(user_id INTEGER PRIMARY KEY, awarded_at TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS daily_claims(user_id INTEGER, claim_date TEXT, PRIMARY KEY(user_id, claim_date))")
        await db.execute("CREATE TABLE IF NOT EXISTS mission_submissions(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, mission_id INTEGER, answer TEXT, file_id TEXT, status TEXT DEFAULT 'pending', submitted_at TEXT)")
        await db.commit()
    for key, value in TEXT_DEFAULTS.items():
        if not await get_setting(key):
            await set_setting(key, value)
    if CHANNEL_LINK and not await get_setting("channel_link"):
        await set_setting("channel_link", CHANNEL_LINK)

async def ensure_user(user):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, username, first_name, xp, created_at) VALUES(?,?,?,?,?)",
            (user.id, user.username or "", user.first_name or "", 0,
             datetime.now(timezone.utc).isoformat())
        )
        await db.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?",
                         (user.username or "", user.first_name or "", user.id))
        await db.commit()

def make_dossier_image(mission_id: int, title: str) -> str:
    """Create a simple classified dossier cover for a mission."""
    path = ASSET_DIR / f"mission_{mission_id}.png"
    img = Image.new("RGB", (1000, 650), (14, 17, 20))
    d = ImageDraw.Draw(img)
    try:
        font_big = ImageFont.truetype("DejaVuSans-Bold.ttf", 42)
        font_mid = ImageFont.truetype("DejaVuSans.ttf", 26)
        font_small = ImageFont.truetype("DejaVuSans.ttf", 20)
    except OSError:
        font_big = font_mid = font_small = ImageFont.load_default()
    # Dossier border, stamp, paper panel
    d.rectangle((35, 35, 965, 615), outline=(75, 105, 95), width=3)
    d.rectangle((65, 85, 935, 560), fill=(25, 30, 31), outline=(70, 80, 78), width=2)
    d.text((85, 55), "SECRET SOCIETY  //  INTELLIGENCE DIVISION", font=font_small, fill=(135, 190, 160))
    d.text((85, 120), f"CASE FILE #{mission_id:04d}", font=font_mid, fill=(210, 215, 205))
    d.line((85, 165, 900, 165), fill=(90, 115, 105), width=2)
    wrapped = textwrap.wrap(title.upper(), width=28)[:3]
    y = 205
    for line in wrapped:
        d.text((85, y), line, font=font_big, fill=(235, 235, 220))
        y += 58
    d.text((85, 475), "STATUS: CLASSIFIED", font=font_mid, fill=(210, 75, 70))
    d.text((85, 520), "AUTHORIZED OPERATIVES ONLY", font=font_small, fill=(130, 160, 145))
    # red stamp
    d.rectangle((700, 205, 885, 280), outline=(170, 55, 50), width=5)
    d.text((720, 225), "CLASSIFIED", font=font_mid, fill=(200, 70, 65))
    img.save(path)
    return str(path)

@dp.message(CommandStart())
async def start(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        was_registered = await (await db.execute("SELECT 1 FROM users WHERE user_id=?", (message.from_user.id,))).fetchone()
    await ensure_user(message.from_user)
    # Telegram deep-link referral: /start ref_<inviter_id>; only brand-new accounts count.
    parts = (message.text or "").split(maxsplit=1)
    if not was_registered and len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            inviter = int(parts[1][4:])
            if inviter != message.from_user.id:
                async with aiosqlite.connect(DB_PATH) as db:
                    exists = await (await db.execute("SELECT 1 FROM referrals WHERE invitee_id=?", (message.from_user.id,))).fetchone()
                    inviter_exists = await (await db.execute("SELECT 1 FROM users WHERE user_id=?", (inviter,))).fetchone()
                    if not exists and inviter_exists:
                        await db.execute("INSERT INTO referrals(invitee_id,inviter_id,created_at) VALUES(?,?,?)", (message.from_user.id, inviter, datetime.now(timezone.utc).isoformat()))
                        await db.execute("UPDATE users SET xp=xp+5 WHERE user_id=?", (inviter,))
                        await db.commit()
                        count = (await (await db.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id=?", (inviter,))).fetchone())[0]
                        bonus = await (await db.execute("SELECT 1 FROM referral_bonus WHERE user_id=?", (inviter,))).fetchone()
                        if count >= 15 and not bonus:
                            await db.execute("UPDATE users SET xp=xp+200 WHERE user_id=?", (inviter,))
                            await db.execute("INSERT INTO referral_bonus(user_id,awarded_at) VALUES(?,?)", (inviter, datetime.now(timezone.utc).isoformat()))
                            await db.commit()
                            await bot.send_message(inviter, "🏆 جایزه ویژه دعوت! به ۱۵ دعوت موفق رسیدی و ۲۰۰ XP جایزه گرفتی!")
                        await bot.send_message(inviter, f"🎉 یک عضو جدید با لینک تو وارد شد! +5 XP\nدعوت موفق: {count}/15")
        except (ValueError, aiosqlite.Error):
            pass
    greeting = await get_setting("welcome", TEXT_DEFAULTS["welcome"])
    # متن‌ها از پنل قابل تغییرند؛ خط‌های خالی اضافی نمایش داده نمی‌شوند.
    greeting = greeting.replace("\n\n", "\n\n")
    await message.answer(greeting, reply_markup=user_kb(await is_admin(message.from_user.id)), parse_mode="HTML")

@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not await is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی مدیر لازم است.")
    await message.answer("🛠 <b>پنل مدیریت سازمان</b>", reply_markup=admin_kb(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("admin:"))
async def admin_callbacks(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return await call.answer("دسترسی غیرمجاز", show_alert=True)
    action = call.data.split(":", 1)[1]
    await call.answer()
    if action == "openpanel":
        if not await is_admin(call.from_user.id):
            return await call.message.answer("⛔ این بخش فقط برای مدیران است.")
        await call.message.answer("🛠 <b>پنل مدیریت سازمان</b>", reply_markup=admin_kb(), parse_mode="HTML")
    elif action == "edittexts":
        await call.message.answer("✏️ کدام متن را می‌خواهی ویرایش کنی؟", reply_markup=kb([
            [InlineKeyboardButton(text="متن خوش‌آمدگویی", callback_data="admin:edittext:welcome")],
            [InlineKeyboardButton(text="متن راهنما", callback_data="admin:edittext:guide")],
            [InlineKeyboardButton(text="⬅️ پنل", callback_data="admin:openpanel")]
        ]))
    elif action.startswith("edittext:"):
        key = action.split(":", 1)[1]
        if key not in TEXT_DEFAULTS:
            return await call.message.answer("متن موردنظر پیدا نشد.")
        await state.update_data(edit_text_key=key)
        await state.set_state(AdminFlow.text_value)
        current = await get_setting(key, TEXT_DEFAULTS[key])
        await call.message.answer("متن جدید را بفرست. برای حفظ قالب‌بندی می‌توانی از خط جدید استفاده کنی.\n\nمتن فعلی:\n" + current, parse_mode="HTML")
    elif action == "channellink":
        current = await get_setting("channel_link", CHANNEL_LINK)
        await state.set_state(AdminFlow.channel_link)
        await call.message.answer("لینک جدید کانال را بفرست (مثلاً https://t.me/channel یا @channel).\nلینک فعلی: " + (current or "ثبت نشده"))
    elif action == "channelannounce":
        await state.set_state(AdminFlow.channel_announce_text)
        await call.message.answer("متن پست کانال را بفرست. پست با دکمه «ورود به بازی» منتشر می‌شود.")
    elif action == "newmission":
        await state.set_state(AdminFlow.mission_title)
        await call.message.answer("عنوان مأموریت جدید را ارسال کن:")
    elif action == "missions":
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT id,title,xp,active FROM missions ORDER BY id DESC")
            rows = await cur.fetchall()
        if not rows:
            return await call.message.answer("هنوز مأموریتی ثبت نشده.")
        text = "\n".join(f"#{i} | {title} | {xp} XP | {'فعال' if active else 'غیرفعال'}" for i,title,xp,active in rows)
        await call.message.answer(text)
    elif action == "rewards":
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT id,user_id,created_at FROM reward_requests WHERE status='pending' ORDER BY id")
            rows = await cur.fetchall()
        if not rows:
            return await call.message.answer("درخواست جایزه‌ی بررسی‌نشده‌ای نیست.")
        for rid, uid, created in rows:
            await call.message.answer(
                f"🎁 درخواست #{rid}\nUser ID: <code>{uid}</code>\n{created}",
                parse_mode="HTML",
                reply_markup=kb([[InlineKeyboardButton(text="✅ تأیید", callback_data=f"reward:approve:{rid}"),
                                  InlineKeyboardButton(text="❌ رد", callback_data=f"reward:reject:{rid}")]])
            )
    elif action == "xp":
        await state.set_state(AdminFlow.xp_user_id)
        await call.message.answer("Telegram User ID کاربر را بفرست:")
    elif action == "announce":
        await state.set_state(AdminFlow.announce_text)
        await call.message.answer("متن اطلاعیه را بفرست:")
    elif action == "admins":
        if not await is_owner(call.from_user.id):
            return await call.message.answer("⛔ فقط مالک ربات می‌تواند ادمین اضافه کند.")
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await (await db.execute("SELECT user_id FROM admins ORDER BY added_at DESC")).fetchall()
        admin_text = f"مالک اصلی: {ADMIN_ID}\n" + ("\n".join(f"ادمین دوم: {r[0]}" for r in rows) if rows else "ادمین دوم: ثبت نشده")
        buttons = []
        if len({ADMIN_ID, *(r[0] for r in rows)}) < 2:
            buttons.append([InlineKeyboardButton(text="➕ افزودن ادمین دوم", callback_data="admin:addadmin", style="success")])
        buttons.append([InlineKeyboardButton(text="⬅️ پنل", callback_data="admin:openpanel")])
        await call.message.answer("👑 فهرست مدیران\n" + admin_text + "\n\nحداکثر دو مدیر قابل ثبت است.", reply_markup=kb(buttons))
    elif action == "addadmin":
        if not await is_owner(call.from_user.id):
            return await call.message.answer("⛔ فقط مالک ربات.")
        await state.set_state(AdminFlow.add_admin_id)
        await call.message.answer("Telegram User ID ادمین جدید را بفرست:")
    elif action == "pendingmissions":
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await (await db.execute("SELECT id,user_id,mission_id,answer FROM mission_submissions WHERE status='pending' ORDER BY id DESC LIMIT 20")).fetchall()
        if not rows: return await call.message.answer("ارسال بررسی‌نشده‌ای وجود ندارد.")
        for sid, uid, mid, answer in rows:
            await call.message.answer(f"📨 ارسال #{sid} | کاربر {uid} | مأموریت #{mid}\n{(answer or '[بدون متن]')[:1500]}", reply_markup=kb([[InlineKeyboardButton(text="✅ قبول + XP", callback_data=f"submission:approve:{sid}"), InlineKeyboardButton(text="❌ رد", callback_data=f"submission:reject:{sid}")]]))
    elif action == "stats":
        async with aiosqlite.connect(DB_PATH) as db:
            users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
            missions = (await (await db.execute("SELECT COUNT(*) FROM missions WHERE active=1")).fetchone())[0]
            pending = (await (await db.execute("SELECT COUNT(*) FROM reward_requests WHERE status='pending'")).fetchone())[0]
        await call.message.answer(f"📊 کاربران: {users}\nمأموریت‌های فعال: {missions}\nدرخواست جایزه در انتظار: {pending}")

@dp.message(AdminFlow.add_admin_id)
async def add_admin_id(message: Message, state: FSMContext):
    if not await is_owner(message.from_user.id): return
    try: uid = int(message.text.strip())
    except (ValueError, AttributeError): return await message.answer("User ID باید عدد باشد.")
    if uid == ADMIN_ID: return await message.answer("این کاربر مالک اصلی است.")
    async with aiosqlite.connect(DB_PATH) as db:
        count = (await (await db.execute("SELECT COUNT(*) FROM admins")).fetchone())[0]
        existing = await (await db.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,))).fetchone()
        if not existing and count >= 1:
            await state.clear()
            return await message.answer("ظرفیت تکمیل است؛ ربات حداکثر دو مدیر دارد (مالک اصلی + یک ادمین دوم).", reply_markup=admin_kb())
        await db.execute("INSERT OR IGNORE INTO admins(user_id,added_by,added_at) VALUES(?,?,?)", (uid, message.from_user.id, datetime.now(timezone.utc).isoformat()))
        await db.commit()
    await state.clear()
    await message.answer(f"👑 کاربر {uid} به فهرست ادمین‌ها اضافه شد.", reply_markup=admin_kb())

@dp.message(AdminFlow.text_value)
async def save_text_value(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("edit_text_key")
    if key not in TEXT_DEFAULTS:
        await state.clear()
        return await message.answer("نوع متن نامعتبر است.")
    value = (message.text or "").strip()
    if not value:
        return await message.answer("متن خالی قابل ذخیره نیست؛ متن را دوباره بفرست.")
    await set_setting(key, value[:4000])
    await state.clear()
    await message.answer("✅ متن ذخیره شد.", reply_markup=admin_kb())

@dp.message(AdminFlow.channel_link)
async def save_channel_link(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if not (value.startswith("@") or "t.me/" in value):
        return await message.answer("لینک معتبر بفرست؛ مثل https://t.me/channel یا @channel")
    await set_setting("channel_link", value)
    await state.clear()
    await message.answer("✅ لینک کانال ذخیره شد.", reply_markup=admin_kb())

@dp.message(AdminFlow.channel_announce_text)
async def channel_announcement(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    destination = await channel_destination()
    if not destination:
        return await message.answer("اول از پنل، لینک عمومی کانال را ثبت کن. برای انتشار، ربات باید در کانال دسترسی ارسال پیام داشته باشد.")
    text_value = (message.text or message.caption or "").strip()
    if not text_value:
        return await message.answer("متن پست را بفرست.")
    me = await bot.get_me()
    game_url = f"https://t.me/{me.username}?start=play"
    markup = kb([[InlineKeyboardButton(text="🎮 ورود به بازی", url=game_url, style="success")]])
    try:
        if message.photo:
            await bot.send_photo(destination, message.photo[-1].file_id, caption=text_value[:1000], reply_markup=markup)
        elif message.document:
            await bot.send_document(destination, message.document.file_id, caption=text_value[:1000], reply_markup=markup)
        else:
            await bot.send_message(destination, text_value[:4000], reply_markup=markup)
    except Exception as exc:
        await state.clear()
        return await message.answer(f"❌ ارسال به کانال انجام نشد. مطمئن شو ربات ادمین کانال است و لینک عمومی درست است.\nخطا: {str(exc)[:300]}", reply_markup=admin_kb())
    await state.clear()
    await message.answer("✅ پست در کانال منتشر شد.", reply_markup=admin_kb())

@dp.message(AdminFlow.mission_title)
async def mission_title(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    await state.update_data(title=message.text[:100])
    await state.set_state(AdminFlow.mission_description)
    await message.answer("شرح مأموریت را ارسال کن:")

@dp.message(AdminFlow.mission_description)
async def mission_description(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    await state.update_data(description=message.text[:3000])
    await state.set_state(AdminFlow.mission_reward_xp)
    await message.answer("چند XP جایزه بده؟ (عدد، مثلاً 150)")

@dp.message(AdminFlow.mission_reward_xp)
async def mission_xp(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    try: xp = max(1, min(100000, int(message.text)))
    except ValueError: return await message.answer("لطفاً عدد معتبر بفرست.")
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT INTO missions(title,description,xp,created_at) VALUES(?,?,?,?)",
                               (data["title"], data["description"], xp, datetime.now(timezone.utc).isoformat()))
        mission_id = cur.lastrowid
        await db.commit()
    path = make_dossier_image(mission_id, data["title"])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE missions SET image_path=? WHERE id=?", (path, mission_id))
        await db.commit()
    await state.clear()
    await message.answer(f"✅ مأموریت #{mission_id} ساخته شد و تصویر پرونده‌ای آن تولید شد.\nXP: {xp}",
                         reply_markup=admin_kb())

@dp.message(AdminFlow.xp_user_id)
async def xp_user(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    try: uid = int(message.text)
    except ValueError: return await message.answer("User ID باید عدد باشد.")
    await state.update_data(uid=uid)
    await state.set_state(AdminFlow.xp_amount)
    await message.answer("مقدار XP (برای کم کردن، عدد منفی بفرست):")

@dp.message(AdminFlow.xp_amount)
async def xp_amount(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    try: amount = int(message.text)
    except ValueError: return await message.answer("عدد معتبر بفرست.")
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET xp=MAX(0,xp+?) WHERE user_id=?", (amount, data["uid"]))
        await db.commit()
    await state.clear()
    await message.answer("⭐ XP کاربر به‌روزرسانی شد.", reply_markup=admin_kb())

@dp.message(AdminFlow.announce_text)
async def announcement(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users")
        ids = [r[0] for r in await cur.fetchall()]
    sent = 0
    for uid in ids:
        try:
            await bot.send_message(uid, "📢 <b>اطلاعیه سازمان</b>\n\n" + message.text[:3500], parse_mode="HTML")
            sent += 1
        except Exception:
            pass
    await state.clear()
    await message.answer(f"اطلاعیه برای {sent} کاربر ارسال شد.", reply_markup=admin_kb())

@dp.callback_query(F.data.startswith("user:"))
async def user_callbacks(call: CallbackQuery):
    await ensure_user(call.from_user)
    action = call.data.split(":", 1)[1]
    await call.answer()
    if action == "startgame":
        await call.message.answer("🎮 بازی شروع شد! از منوی زیر یک بخش را انتخاب کن.", reply_markup=user_kb(await is_admin(call.from_user.id)))
    elif action == "guide":
        guide = await get_setting("guide", TEXT_DEFAULTS["guide"])
        await call.message.answer(guide, parse_mode="HTML", reply_markup=kb([[InlineKeyboardButton(text="🚀 شروع بازی", callback_data="user:missions")]]))
    elif action == "profile":
        async with aiosqlite.connect(DB_PATH) as db:
            row = await (await db.execute("SELECT xp FROM users WHERE user_id=?", (call.from_user.id,))).fetchone()
        xp = row[0] if row else 0
        rank, next_rank, next_xp, pct = rank_progress(xp)
        text = f"🪪 <b>پرونده عضو</b>\nID: <code>{call.from_user.id}</code>\nXP: {xp}\nRank: <b>{rank}</b>"
        if next_rank: text += f"\nرتبه بعدی: {next_rank} ({next_xp} XP)\nپیشرفت: {pct}%"
        await call.message.answer(text, parse_mode="HTML")
    elif action == "missions":
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await (await db.execute("SELECT id,title,description,xp,image_path FROM missions WHERE active=1 ORDER BY id DESC")).fetchall()
        if not rows: return await call.message.answer("فعلاً مأموریت فعالی وجود ندارد.")
        for mid,title,desc,xp,img_path in rows:
            markup = kb([[InlineKeyboardButton(text="📩 ارسال پاسخ برای بررسی", callback_data=f"mission:submit:{mid}", style="success")]])
            caption = f"🗂 <b>CASE #{mid:04d}</b>\n<b>{title}</b>\n\n{desc}\n\n🎯 پاداش: {xp} XP"
            try:
                with open(img_path, "rb") as f:
                    await call.message.answer_photo(BufferedInputFile(f.read(), filename="case.png"), caption=caption,
                                                    parse_mode="HTML", reply_markup=markup)
            except Exception:
                await call.message.answer(caption, parse_mode="HTML", reply_markup=markup)
    elif action == "reward":
        async with aiosqlite.connect(DB_PATH) as db:
            xp_row = await (await db.execute("SELECT xp FROM users WHERE user_id=?", (call.from_user.id,))).fetchone()
            xp = xp_row[0] if xp_row else 0
            if xp < 5000:
                return await call.message.answer(f"برای درخواست جایزه باید حداقل 5000 XP داشته باشی.\nXP فعلی: {xp}")
            cur = await db.execute("INSERT INTO reward_requests(user_id,created_at) VALUES(?,?)",
                                   (call.from_user.id, datetime.now(timezone.utc).isoformat()))
            rid = cur.lastrowid
            await db.commit()
        await call.message.answer("🎁 درخواستت برای مدیر ارسال شد. منتظر بررسی باش.")
        for admin_uid in await admin_ids():
            try:
                await bot.send_message(admin_uid, f"🎁 درخواست جایزه #{rid}\nUser: {call.from_user.id}\nUsername: @{call.from_user.username or 'ندارد'}",
                                       reply_markup=kb([[InlineKeyboardButton(text="✅ تأیید", callback_data=f"reward:approve:{rid}"),
                                                         InlineKeyboardButton(text="❌ رد", callback_data=f"reward:reject:{rid}")]]))
            except Exception:
                pass
    elif action == "referral":
        me = await bot.get_me()
        async with aiosqlite.connect(DB_PATH) as db:
            count = (await (await db.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id=?",(call.from_user.id,))).fetchone())[0]
            bonus = await (await db.execute("SELECT 1 FROM referral_bonus WHERE user_id=?",(call.from_user.id,))).fetchone()
        link = f"https://t.me/{me.username}?start=ref_{call.from_user.id}"
        await call.message.answer(f"🔗 <b>لینک دعوت اختصاصی تو</b>\n<code>{link}</code>\n\nهر دعوت موفق: <b>+5 XP</b>\nبا ۱۵ دعوت موفق: <b>+200 XP جایزه ویژه</b>\n\nدعوت‌های موفق: {count}/15\nجایزه ۱۵تایی: {'گرفته شده' if bonus else 'هنوز فعال'}",parse_mode="HTML")
    elif action == "daily":
        today=datetime.now(timezone.utc).date().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            cur=await db.execute("INSERT OR IGNORE INTO daily_claims(user_id,claim_date) VALUES(?,?)",(call.from_user.id,today))
            if cur.rowcount:
                await db.execute("UPDATE users SET xp=xp+25 WHERE user_id=?",(call.from_user.id,)); await db.commit()
                await call.message.answer("🎁 پاداش روزانه دریافت شد: +25 XP")
            else: await call.message.answer("پاداش امروزت را قبلاً گرفتی. فردا دوباره سر بزن!")
    elif action == "leaderboard":
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await (await db.execute("SELECT first_name,xp FROM users ORDER BY xp DESC LIMIT 10")).fetchall()
        text = "🏆 <b>Top Operatives</b>\n\n" + "\n".join(f"{i}. {name or 'Operative'} — {xp} XP" for i,(name,xp) in enumerate(rows,1))
        await call.message.answer(text, parse_mode="HTML")

@dp.callback_query(F.data.startswith("mission:submit:"))
async def mission_submit(call: CallbackQuery, state: FSMContext):
    mid = int(call.data.rsplit(":", 1)[1])
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT title FROM missions WHERE id=? AND active=1", (mid,))).fetchone()
        if not row: return await call.answer("مأموریت پیدا نشد.", show_alert=True)
        done = await (await db.execute("SELECT status FROM completions WHERE user_id=? AND mission_id=?", (call.from_user.id,mid))).fetchone()
        if done and done[0] == "approved": return await call.answer("این مأموریت قبلاً تأیید شده.", show_alert=True)
    await state.update_data(submission_mission_id=mid)
    await state.set_state(AdminFlow.mission_submission)
    await call.answer()
    await call.message.answer(f"📤 پاسخ مأموریت «{row[0]}» را بفرست.\nمی‌توانی متن، عکس یا فایل ارسال کنی. اگر عکس داری، توضیح را در کپشن بنویس.")

@dp.message(AdminFlow.mission_submission)
async def receive_mission_submission(message: Message, state: FSMContext):
    data = await state.get_data(); mid = data.get("submission_mission_id")
    answer = message.text or message.caption or ""
    file_id = message.photo[-1].file_id if message.photo else (message.document.file_id if message.document else None)
    if not answer and not file_id:
        return await message.answer("لطفاً متن، عکس یا فایل پاسخ را ارسال کن.")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO mission_submissions(user_id,mission_id,answer,file_id,status,submitted_at) VALUES(?,?,?,?,?,?)", (message.from_user.id,mid,answer,file_id,"pending",datetime.now(timezone.utc).isoformat()))
        await db.execute("INSERT OR IGNORE INTO completions(user_id,mission_id,status,submitted_at) VALUES(?,?,?,?)", (message.from_user.id,mid,"pending",datetime.now(timezone.utc).isoformat()))
        await db.commit()
        sid = (await (await db.execute("SELECT last_insert_rowid()")).fetchone())[0]
        mission = await (await db.execute("SELECT title,xp FROM missions WHERE id=?",(mid,))).fetchone()
    await state.clear()
    await message.answer("📨 پاسخ محرمانه‌ات برای بررسی ارسال شد. نتیجه در همین ربات اعلام می‌شود.", reply_markup=user_kb())
    text = f"🕵️ ارسال مأموریت #{sid}\nعنوان: {mission[0]}\nکاربر: {message.from_user.id} (@{message.from_user.username or 'بدون username'})\n\n{answer[:3000]}"
    markup = kb([[InlineKeyboardButton(text="✅ تأیید + XP", callback_data=f"submission:approve:{sid}"),InlineKeyboardButton(text="❌ رد", callback_data=f"submission:reject:{sid}")]])
    for admin_uid in await admin_ids():
        try:
            if message.photo:
                await bot.send_photo(admin_uid, file_id, caption=text[:1000], reply_markup=markup)
            elif message.document:
                await bot.send_document(admin_uid, file_id, caption=text[:1000], reply_markup=markup)
            else:
                await bot.send_message(admin_uid, text, reply_markup=markup)
        except Exception:
            pass

@dp.callback_query(F.data.startswith("submission:"))
async def submission_review(call: CallbackQuery):
    if not await is_admin(call.from_user.id): return await call.answer("دسترسی غیرمجاز",show_alert=True)
    _,decision,sid=call.data.split(":")
    async with aiosqlite.connect(DB_PATH) as db:
        row=await (await db.execute("SELECT user_id,mission_id,status FROM mission_submissions WHERE id=?",(int(sid),))).fetchone()
        if not row or row[2] != "pending": return await call.answer("قبلاً بررسی شده.",show_alert=True)
        uid,mid,_=row
        await db.execute("UPDATE mission_submissions SET status=? WHERE id=?",("approved" if decision=="approve" else "rejected",int(sid)))
        if decision=="approve":
            mission=await (await db.execute("SELECT title,xp FROM missions WHERE id=?",(mid,))).fetchone()
            await db.execute("UPDATE users SET xp=xp+? WHERE user_id=?",(mission[1],uid))
            await db.execute("UPDATE completions SET status='approved' WHERE user_id=? AND mission_id=?",(uid,mid))
        else:
            await db.execute("UPDATE completions SET status='rejected' WHERE user_id=? AND mission_id=?",(uid,mid))
        await db.commit()
    if decision=="approve": await bot.send_message(uid,f"✅ مأموریت «{mission[0]}» تأیید شد! +{mission[1]} XP")
    else: await bot.send_message(uid,"❌ پاسخ مأموریت رد شد. می‌توانی پاسخ جدید ارسال کنی.")
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("بررسی انجام شد.")

@dp.callback_query(F.data.startswith("missionapprove:"))
async def mission_approve(call: CallbackQuery):
    if not await is_admin(call.from_user.id): return await call.answer("دسترسی غیرمجاز", show_alert=True)
    _, uid, mid = call.data.split(":")
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT xp,title FROM missions WHERE id=?", (int(mid),))).fetchone()
        if not row: return await call.answer("مأموریت پیدا نشد.")
        cur = await db.execute("UPDATE completions SET status='approved' WHERE user_id=? AND mission_id=? AND status='pending'",
                               (int(uid), int(mid)))
        if cur.rowcount == 0: return await call.answer("این درخواست قبلاً بررسی شده.")
        await db.execute("UPDATE users SET xp=xp+? WHERE user_id=?", (row[0], int(uid)))
        await db.commit()
    await bot.send_message(int(uid), f"✅ مأموریت «{row[1]}» تأیید شد!\n+{row[0]} XP")
    await call.message.edit_text(f"تأیید شد: کاربر {uid}، مأموریت {mid}")
    await call.answer("تأیید شد")

@dp.callback_query(F.data.startswith("missionreject:"))
async def mission_reject(call: CallbackQuery):
    if not await is_admin(call.from_user.id): return await call.answer("دسترسی غیرمجاز", show_alert=True)
    _, uid, mid = call.data.split(":")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE completions SET status='rejected' WHERE user_id=? AND mission_id=? AND status='pending'",
                         (int(uid), int(mid)))
        await db.commit()
    await bot.send_message(int(uid), "❌ درخواست مأموریتت رد شد. می‌تونی دوباره تلاش کنی.")
    await call.message.edit_text("درخواست رد شد.")
    await call.answer()

@dp.callback_query(F.data.startswith("reward:"))
async def reward_review(call: CallbackQuery):
    if not await is_admin(call.from_user.id): return await call.answer("دسترسی غیرمجاز", show_alert=True)
    _, decision, rid = call.data.split(":")
    status = "approved" if decision == "approve" else "rejected"
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT user_id,status FROM reward_requests WHERE id=?", (int(rid),))).fetchone()
        if not row or row[1] != "pending": return await call.answer("قبلاً بررسی شده.")
        await db.execute("UPDATE reward_requests SET status=? WHERE id=?", (status, int(rid)))
        await db.commit()
    await bot.send_message(row[0], "🎁 درخواست جایزه‌ات تأیید شد! برای هماهنگی جایزه با مدیر صحبت کن." if decision == "approve"
                           else "درخواست جایزه‌ات فعلاً تأیید نشد.")
    await call.message.edit_text(f"درخواست #{rid}: {status}")
    await call.answer("انجام شد")

async def main():
    await init_db()
    print("Secret Society bot is running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
