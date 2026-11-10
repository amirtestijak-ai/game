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
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATA_DIR = Path("/data") if Path("/data").is_dir() else Path(".")
DB_PATH = str(DATA_DIR / "secret_society.sqlite3")
ASSET_DIR = DATA_DIR / "mission_assets"
ASSET_DIR.mkdir(parents=True, exist_ok=True)

if not BOT_TOKEN or not ADMIN_ID:
    raise RuntimeError("Set BOT_TOKEN and ADMIN_ID environment variables first.")

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

REQUIRED_CHANNEL = "@dsgjnjjyfg"
REQUIRED_CHANNEL_URL = "https://t.me/dsgjnjjyfg"
BUTTON_STYLES = {}
BUTTON_STYLE_DEFAULTS = {
    "admin:newmission": "success",
    "admin:missions": "primary",
    "admin:rewards": "success",
    "admin:xp": "primary",
    "admin:announce": "primary",
    "admin:stats": "primary",
    "admin:admins": "primary",
    "admin:pendingmissions": "primary",
    "admin:referrals": "success",
    "admin:colors": "primary",
    "user:missions": "primary",
    "user:profile": "primary",
    "user:reward": "success",
    "user:leaderboard": "primary",
    "user:referral": "success",
    "user:daily": "success",
}
BUTTON_LABELS = {
    "admin:newmission": "ساخت مأموریت",
    "admin:missions": "مأموریت‌ها",
    "admin:rewards": "درخواست جایزه‌ها",
    "admin:xp": "دادن XP",
    "admin:announce": "پیام همگانی",
    "admin:stats": "آمار ربات",
    "admin:admins": "مدیریت ادمین‌ها",
    "admin:pendingmissions": "بررسی پاسخ مأموریت‌ها",
    "admin:referrals": "گزارشات رفرال",
    "user:missions": "مأموریت‌ها",
    "user:profile": "پروفایل",
    "user:reward": "درخواست بررسی جایزه",
    "user:leaderboard": "رتبه‌بندی",
    "user:referral": "دعوت دوستان",
    "user:daily": "جایزه روزانه",
}
BUTTON_STYLE_CHOICES = ("primary", "success", "danger")

def make_button(text, callback_data, style="primary", **kwargs):
    # Telegram supports preset inline-button styles, not arbitrary RGB colors.
    selected = BUTTON_STYLES.get(callback_data, BUTTON_STYLES.get(callback_data.split(":")[0] + ":" + callback_data.split(":")[1] if ":" in callback_data else callback_data, style))
    return InlineKeyboardButton(text=text, callback_data=callback_data, style=selected, **kwargs)

async def load_button_styles():
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("SELECT button_key, style FROM button_styles")).fetchall()
    BUTTON_STYLES.clear()
    BUTTON_STYLES.update({key: value for key, value in rows})

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
        [make_button(text="➕ ساخت مأموریت", callback_data="admin:newmission", style="success"),
         make_button(text="📋 مأموریت‌ها", callback_data="admin:missions", style="primary")],
        [make_button(text="🎁 درخواست جایزه‌ها", callback_data="admin:rewards", style="success"),
         make_button(text="⭐ دادن XP", callback_data="admin:xp", style="primary")],
        [make_button(text="📢 پیام همگانی", callback_data="admin:announce", style="primary"),
         make_button(text="📊 آمار ربات", callback_data="admin:stats", style="primary")],
        [make_button(text="👥 گزارشات رفرال", callback_data="admin:referrals", style="success"),
         make_button(text="🎨 تغییر رنگ دکمه‌ها", callback_data="admin:colors", style="primary")],
    ])

def user_kb():
    return kb([
        [make_button(text="🗂 مأموریت‌ها", callback_data="user:missions", style="primary"),
         make_button(text="🪪 پروفایل", callback_data="user:profile", style="primary")],
        [make_button(text="🎁 درخواست بررسی جایزه", callback_data="user:reward", style="success")],
        [make_button(text="🏆 رتبه‌بندی", callback_data="user:leaderboard", style="primary")],
        [make_button(text="🔗 دعوت دوستان", callback_data="user:referral", style="success"),
         make_button(text="🎁 جایزه روزانه", callback_data="user:daily", style="success")],
    ])

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
        await db.execute("CREATE TABLE IF NOT EXISTS referrals(invitee_id INTEGER PRIMARY KEY, inviter_id INTEGER, created_at TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS pending_referrals(invitee_id INTEGER PRIMARY KEY, inviter_id INTEGER, created_at TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS button_styles(button_key TEXT PRIMARY KEY, style TEXT NOT NULL)")
        await db.execute("CREATE TABLE IF NOT EXISTS referral_bonus(user_id INTEGER PRIMARY KEY, awarded_at TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS daily_claims(user_id INTEGER, claim_date TEXT, PRIMARY KEY(user_id, claim_date))")
        await db.execute("CREATE TABLE IF NOT EXISTS mission_submissions(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, mission_id INTEGER, answer TEXT, file_id TEXT, status TEXT DEFAULT 'pending', submitted_at TEXT)")
        await db.commit()

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

async def is_channel_member(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ("creator", "administrator", "member") or (
            member.status == "restricted" and bool(getattr(member, "is_member", False))
        )
    except Exception:
        return False

def join_required_markup():
    return kb([
        [InlineKeyboardButton(text="📢 عضویت در کانال", url=REQUIRED_CHANNEL_URL)],
        [make_button("✅ تأیید عضویت", "membership:verify", style="success")]
    ])

async def complete_user_registration(user):
    await ensure_user(user)
    async with aiosqlite.connect(DB_PATH) as db:
        pending = await (await db.execute(
            "SELECT inviter_id FROM pending_referrals WHERE invitee_id=?", (user.id,)
        )).fetchone()
        if pending:
            inviter = int(pending[0])
            if inviter != user.id:
                exists = await (await db.execute(
                    "SELECT 1 FROM referrals WHERE invitee_id=?", (user.id,)
                )).fetchone()
                inviter_exists = await (await db.execute(
                    "SELECT 1 FROM users WHERE user_id=?", (inviter,)
                )).fetchone()
                if not exists and inviter_exists:
                    now = datetime.now(timezone.utc).isoformat()
                    await db.execute(
                        "INSERT INTO referrals(invitee_id,inviter_id,created_at) VALUES(?,?,?)",
                        (user.id, inviter, now)
                    )
                    await db.execute("UPDATE users SET xp=xp+5 WHERE user_id=?", (inviter,))
                    count = (await (await db.execute(
                        "SELECT COUNT(*) FROM referrals WHERE inviter_id=?", (inviter,)
                    )).fetchone())[0]
                    bonus = await (await db.execute(
                        "SELECT 1 FROM referral_bonus WHERE user_id=?", (inviter,)
                    )).fetchone()
                    if count >= 15 and not bonus:
                        await db.execute("UPDATE users SET xp=xp+200 WHERE user_id=?", (inviter,))
                        await db.execute(
                            "INSERT INTO referral_bonus(user_id,awarded_at) VALUES(?,?)",
                            (inviter, now)
                        )
                        await bot.send_message(
                            inviter, "🏆 جایزه ویژه دعوت! به ۱۵ دعوت موفق رسیدی و ۲۰۰ XP جایزه گرفتی!"
                        )
                    await bot.send_message(
                        inviter, f"🎉 یک عضو جدید با لینک تو وارد شد! +5 XP\\nدعوت موفق: {count}/15"
                    )
            await db.execute("DELETE FROM pending_referrals WHERE invitee_id=?", (user.id,))
        await db.commit()

async def show_join_gate(message: Message):
    await message.answer(
        "🔒 برای استفاده از SECRET SOCIETY اول باید عضو کانال زیر بشی.\\n"
        "بعد از عضویت، دکمه «تأیید عضویت» رو بزن.",
        reply_markup=join_required_markup()
    )

@dp.message(CommandStart())
async def start(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        already_registered = await (await db.execute("SELECT 1 FROM users WHERE user_id=?", (message.from_user.id,))).fetchone()
    parts = (message.text or "").split(maxsplit=1)
    if not already_registered and len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            inviter = int(parts[1][4:])
            if inviter != message.from_user.id:
                async with aiosqlite.connect(DB_PATH) as db:
                    inviter_exists = await (await db.execute(
                        "SELECT 1 FROM users WHERE user_id=?", (inviter,)
                    )).fetchone()
                    if inviter_exists:
                        await db.execute(
                            "INSERT OR IGNORE INTO pending_referrals(invitee_id,inviter_id,created_at) VALUES(?,?,?)",
                            (message.from_user.id, inviter, datetime.now(timezone.utc).isoformat())
                        )
                        await db.commit()
        except (ValueError, aiosqlite.Error):
            pass

    if not await is_channel_member(message.from_user.id):
        return await show_join_gate(message)

    await complete_user_registration(message.from_user)
    greeting = ("🕶️ <b>به SECRET SOCIETY خوش آمدی</b>\\n\\n"
                "📁 پرونده‌ی عضویتت ساخته شد.\\n"
                "🎯 مأموریت‌ها را انجام بده، XP جمع کن، رتبه‌ات را ارتقا بده و به شورای سایه نزدیک شو.\\n\\n"
                "🔐 <i>هر عضو، یک پرونده. هر مأموریت، یک آزمون.</i>\\n\\nاز منوی زیر شروع کن، Operative.")
    await message.answer(greeting, reply_markup=user_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "membership:verify")
async def verify_membership(call: CallbackQuery):
    if not await is_channel_member(call.from_user.id):
        return await call.answer("هنوز عضویتت در کانال تأیید نشده.", show_alert=True)
    await complete_user_registration(call.from_user)
    await call.answer("عضویت تأیید شد ✅")
    greeting = ("🕶️ <b>به SECRET SOCIETY خوش آمدی</b>\\n\\n"
                "📁 پرونده‌ی عضویتت ساخته شد.\\n"
                "🎯 مأموریت‌ها را انجام بده، XP جمع کن، رتبه‌ات را ارتقا بده و به شورای سایه نزدیک شو.\\n\\n"
                "🔐 <i>هر عضو، یک پرونده. هر مأموریت، یک آزمون.</i>\\n\\nاز منوی زیر شروع کن، Operative.")
    await call.message.answer(greeting, reply_markup=user_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "noop:join")
async def noop_join(call: CallbackQuery):
    await call.answer()

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
    if action == "newmission":
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
                reply_markup=kb([[make_button(text="✅ تأیید", callback_data=f"reward:approve:{rid}"),
                                  make_button(text="❌ رد", callback_data=f"reward:reject:{rid}")]])
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
        admin_text = "\n".join(str(r[0]) for r in rows) or "ادمین فرعی ثبت نشده."
        await call.message.answer("👑 ادمین‌های فعلی:\n" + admin_text + "\n\nبرای افزودن ادمین، دکمه زیر را بزن.", reply_markup=kb([[make_button(text="➕ افزودن ادمین", callback_data="admin:addadmin")]]))
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
            await call.message.answer(f"📨 ارسال #{sid} | کاربر {uid} | مأموریت #{mid}\n{(answer or '[بدون متن]')[:1500]}", reply_markup=kb([[make_button(text="✅ قبول + XP", callback_data=f"submission:approve:{sid}"), make_button(text="❌ رد", callback_data=f"submission:reject:{sid}")]]))
    elif action == "colors":
        rows = []
        for key, label in BUTTON_LABELS.items():
            rows.append([make_button(f"🎨 {label}", f"admin:colorpick:{key}", style=BUTTON_STYLES.get(key, BUTTON_STYLE_DEFAULTS.get(key, "primary")))])
        rows.append([make_button("↩️ بازگشت به پنل", "admin:back", style="primary")])
        await call.message.answer(
            "🎨 <b>تغییر رنگ دکمه‌ها</b>\\nدکمه موردنظر رو انتخاب کن.\\n"
            "رنگ‌های قابل پشتیبانی تلگرام: آبی، سبز و قرمز.",
            reply_markup=kb(rows), parse_mode="HTML"
        )
    elif action.startswith("colorpick:"):
        key = action.split(":", 1)[1]
        if key not in BUTTON_LABELS:
            return await call.answer("دکمه معتبر نیست.", show_alert=True)
        choices = [
            [make_button("🔵 آبی", f"admin:colorset:primary:{key}", style="primary")],
            [make_button("🟢 سبز", f"admin:colorset:success:{key}", style="success")],
            [make_button("🔴 قرمز", f"admin:colorset:danger:{key}", style="danger")],
            [make_button("↩️ بازگشت", "admin:colors", style="primary")]
        ]
        await call.message.answer(f"رنگ جدید دکمه «{BUTTON_LABELS[key]}» رو انتخاب کن:", reply_markup=kb(choices))
    elif action.startswith("colorset:"):
        parts = action.split(":", 2)
        if len(parts) != 3:
            return await call.answer("درخواست نامعتبر است.", show_alert=True)
        style, key = parts[1], parts[2]
        if style not in BUTTON_STYLE_CHOICES or key not in BUTTON_LABELS:
            return await call.answer("تنظیم نامعتبر است.", show_alert=True)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO button_styles(button_key,style) VALUES(?,?) ON CONFLICT(button_key) DO UPDATE SET style=excluded.style",
                (key, style)
            )
            await db.commit()
        BUTTON_STYLES[key] = style
        await call.answer("رنگ دکمه ذخیره شد ✅")
        await call.message.answer(f"✅ رنگ «{BUTTON_LABELS[key]}» تغییر کرد.", reply_markup=admin_kb())
    elif action == "back":
        await call.message.answer("🛠 <b>پنل مدیریت سازمان</b>", reply_markup=admin_kb(), parse_mode="HTML")
    elif action.startswith("referrals"):
        try:
            page = int(action.split(":", 1)[1]) if ":" in action else 0
        except ValueError:
            page = 0
        page = max(0, page)
        page_size = 15
        async with aiosqlite.connect(DB_PATH) as db:
            total = (await (await db.execute("SELECT COUNT(*) FROM referrals")).fetchone())[0]
            rows = await (await db.execute(
                """SELECT r.created_at,
                          i.user_id, i.username, i.first_name, i.xp,
                          u.user_id, u.username, u.first_name, u.xp
                   FROM referrals r
                   LEFT JOIN users i ON i.user_id=r.inviter_id
                   LEFT JOIN users u ON u.user_id=r.invitee_id
                   ORDER BY r.created_at DESC LIMIT ? OFFSET ?""",
                (page_size, page * page_size)
            )).fetchall()
            top = await (await db.execute(
                """SELECT u.user_id,u.username,u.first_name,COUNT(r.invitee_id)
                   FROM users u JOIN referrals r ON r.inviter_id=u.user_id
                   GROUP BY u.user_id ORDER BY COUNT(r.invitee_id) DESC LIMIT 5"""
            )).fetchall()
        lines = [f"👥 <b>گزارش رفرال‌ها</b>\\nکل دعوت‌های موفق: <b>{total}</b>\\nصفحه: {page+1}\\n"]
        for created, iid, iu, inf, ixp, uid, uu, unf, uxp in rows:
            lines.append(
                f"━━━━━━━━━━━━\\n"
                f"👤 <b>دعوت‌کننده:</b> {inf or 'بدون نام'} (@{iu or 'ندارد'})\\n"
                f"🆔 ID: <code>{iid}</code> | XP: {ixp or 0}\\n"
                f"➕ <b>عضو دعوت‌شده:</b> {unf or 'بدون نام'} (@{uu or 'ندارد'})\\n"
                f"🆔 ID: <code>{uid}</code> | XP: {uxp or 0}\\n"
                f"🕒 {created}"
            )
        if not rows:
            lines.append("هنوز دعوت موفقی ثبت نشده.")
        if top:
            lines.append("\\n🏆 <b>بیشترین دعوت‌ها</b>")
            for rank, (uid, username, first_name, count) in enumerate(top, 1):
                lines.append(f"{rank}. {first_name or 'کاربر'} (@{username or 'ندارد'}) — {count} دعوت | <code>{uid}</code>")
        nav = []
        if page > 0:
            nav.append(make_button("⬅️ قبلی", f"admin:referrals:{page-1}", style="primary"))
        if (page + 1) * page_size < total:
            nav.append(make_button("بعدی ➡️", f"admin:referrals:{page+1}", style="primary"))
        markup = kb([nav]) if nav else None
        await call.message.answer("\\n".join(lines)[:4000], parse_mode="HTML", reply_markup=markup)
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
        await db.execute("INSERT OR IGNORE INTO admins(user_id,added_by,added_at) VALUES(?,?,?)", (uid, message.from_user.id, datetime.now(timezone.utc).isoformat()))
        await db.commit()
    await state.clear()
    await message.answer(f"👑 کاربر {uid} به فهرست ادمین‌ها اضافه شد.", reply_markup=admin_kb())

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
    if not await is_channel_member(call.from_user.id):
        await call.answer("برای استفاده از ربات باید عضو کانال باشی.", show_alert=True)
        try: await call.message.answer("🔒 ابتدا عضو کانال شو و سپس تأیید عضویت را بزن.", reply_markup=join_required_markup())
        except Exception: pass
        return
    await complete_user_registration(call.from_user)
    action = call.data.split(":", 1)[1]
    await call.answer()
    if action == "profile":
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
            markup = kb([[make_button(text="📩 ارسال پاسخ برای بررسی", callback_data=f"mission:submit:{mid}", style="success")]])
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
        await bot.send_message(ADMIN_ID, f"🎁 درخواست جایزه #{rid}\nUser: {call.from_user.id}\nUsername: @{call.from_user.username or 'ندارد'}",
                               reply_markup=kb([[make_button(text="✅ تأیید", callback_data=f"reward:approve:{rid}"),
                                                 make_button(text="❌ رد", callback_data=f"reward:reject:{rid}")]]))
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
    if not await is_channel_member(call.from_user.id):
        return await call.answer("ابتدا عضو کانال شو.", show_alert=True)
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
    if not await is_channel_member(message.from_user.id):
        await state.clear()
        return await show_join_gate(message)
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
    markup = kb([[make_button(text="✅ تأیید + XP", callback_data=f"submission:approve:{sid}"),make_button(text="❌ رد", callback_data=f"submission:reject:{sid}")]])
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
    await load_button_styles()
    print("Secret Society bot is running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
