"""
Vynexa News Bot — automatic Persian news publisher
Python 3.10+
Install:
    pip install "aiogram>=3.7" aiosqlite aiohttp feedparser
Run:
    BOT_TOKEN=... ADMIN_ID=... python bot.py

Optional:
    OPENAI_API_KEY=...       # original Persian summaries (otherwise headline translation only)
    OPENAI_MODEL=gpt-4o-mini
    DB_PATH=newsbot.sqlite3
    PORT=8080                # optional health endpoint for hosting platforms
"""
import asyncio
import hashlib
import html
import logging
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp
import aiosqlite
import feedparser
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
DB_PATH = os.getenv("DB_PATH", "secret_society.sqlite3")
DEFAULT_INTERVAL = 60
MAX_POSTS_PER_CYCLE = 5
POST_IMAGES_DEFAULT = "1"  # enable only when the channel has permission to reuse source images

if not BOT_TOKEN or not ADMIN_ID:
    raise RuntimeError("BOT_TOKEN and ADMIN_ID environment variables are required.")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("vynexa_news")

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# Iran-only feeds are marked "iran". General feeds are classified by headline.
# Reuters and AP are accessed through Google News RSS search feeds because their
# old public RSS endpoints are no longer maintained.
SOURCES = [
    {
        "id": "iranintl", "name": "Iran International",
        "url": "https://www.iranintl.com/en/feed", "category": "iran",
    },
    {
        "id": "iranwire", "name": "IranWire",
        "url": "https://iranwire.com/en/feed/", "category": "iran",
    },
    {
        "id": "radiofarda", "name": "Radio Farda",
        "url": "https://www.rferl.org/api/z-oiievgii", "category": "iran",
    },
    {
        "id": "guardian_iran", "name": "The Guardian — Iran",
        "url": "https://www.theguardian.com/world/iran/rss", "category": "iran",
    },
    {
        "id": "reuters_iran", "name": "Reuters",
        "url": "https://news.google.com/rss/search?q=site%3Areuters.com+Iran&hl=en-US&gl=US&ceid=US:en",
        "category": "iran",
    },
    {
        "id": "ap_iran", "name": "Associated Press",
        "url": "https://news.google.com/rss/search?q=site%3Aapnews.com+Iran&hl=en-US&gl=US&ceid=US:en",
        "category": "iran",
    },
    {
        "id": "aljazeera", "name": "Al Jazeera",
        "url": "https://www.aljazeera.com/xml/rss/all.xml", "category": "world",
    },
    {
        "id": "dw", "name": "Deutsche Welle",
        "url": "https://rss.dw.com/xml/rss-en-all", "category": "world",
    },
    {
        "id": "wsj", "name": "The Wall Street Journal",
        "url": "https://feeds.a.dj.com/rss/RSSWorldNews.xml", "category": "world",
    },
    {
        "id": "euronews_iran", "name": "Euronews — Iran",
        "url": "https://www.euronews.com/rss?level=tag&name=iran", "category": "iran",
    },
    {
        "id": "france24_iran", "name": "France 24 — Iran",
        "url": "https://www.france24.com/en/tag/iran/rss", "category": "iran",
    },
]

IRAN_TERMS = (
    "iran", "iranian", "tehran", "persian gulf", "islamic republic",
    "ایران", "تهران", "ایرانی", "فارس", "خلیج فارس",
)

class AdminFlow(StatesGroup):
    channel = State()
    interval = State()


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 تنظیم کانال مقصد", callback_data="adm:channel")],
        [InlineKeyboardButton(text="📰 مدیریت منابع خبری", callback_data="adm:sources")],
        [InlineKeyboardButton(text="⏱ فاصله بررسی خبرها", callback_data="adm:interval")],
        [InlineKeyboardButton(text="🇮🇷 سهمیه ایران / جهان", callback_data="adm:ratio")],
        [InlineKeyboardButton(text="🖼 تنظیم تصاویر خبر", callback_data="adm:images")],
        [InlineKeyboardButton(text="▶️ روشن / خاموش کردن انتشار", callback_data="adm:toggle")],
        [InlineKeyboardButton(text="🔎 بررسی اتصال منابع", callback_data="adm:test")],
        [InlineKeyboardButton(text="📊 آمار ربات", callback_data="adm:stats")],
    ])


def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ برگشت به پنل", callback_data="adm:panel")]
    ])


async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        )).fetchone()
    return row[0] if row else default


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        await db.commit()


async def is_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT user_id FROM admins WHERE user_id=?", (user_id,)
        )).fetchone()
    return bool(row)


async def admin_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("SELECT user_id FROM admins")).fetchall()
    return sorted({ADMIN_ID, *(int(r[0]) for r in rows)})


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS seen_articles(
                guid TEXT PRIMARY KEY, source_id TEXT, category TEXT,
                title TEXT, link TEXT, seen_at TEXT
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS posted_articles(
                id INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE,
                source_id TEXT, category TEXT, title TEXT, link TEXT,
                posted_at TEXT
            )"""
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, added_at TEXT)"
        )
        await db.commit()
    defaults = {
        "channel": os.getenv("NEWS_CHANNEL", "").strip(),
        "interval": str(DEFAULT_INTERVAL),
        "enabled": "1",
        "post_images": POST_IMAGES_DEFAULT,
        "iran_percent": "80",
        "world_percent": "20",
        "seeded": "0",
    }
    for key, value in defaults.items():
        if await get_setting(key, "") == "":
            await set_setting(key, value)
    for source in SOURCES:
        if await get_setting("source_" + source["id"], "") == "":
            await set_setting("source_" + source["id"], "1")


def normalize_channel(value: str):
    value = value.strip()
    if value.startswith("@"):
        return value
    if value.startswith("-100") and value[1:].isdigit():
        return value
    if "t.me/" in value:
        tail = value.rstrip("/").split("t.me/", 1)[1].split("?", 1)[0].strip("/")
        if tail and not tail.startswith("+") and not tail.startswith("joinchat/"):
            return "@" + tail
    return None


def clean_html(value: str) -> str:
    value = re.sub(r"<(script|style).*?>.*?</\1>", " ", value or "", flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def extract_image(entry):
    for item in entry.get("media_content", []) or []:
        url = item.get("url")
        if url and url.startswith("https://"):
            return url
    for item in entry.get("media_thumbnail", []) or []:
        url = item.get("url")
        if url and url.startswith("https://"):
            return url
    for item in entry.get("enclosures", []) or []:
        url = item.get("href") or item.get("url")
        kind = item.get("type", "")
        if url and url.startswith("https://") and kind.startswith("image/"):
            return url
    return None


def entry_guid(source_id: str, entry) -> str:
    raw = str(entry.get("id") or entry.get("guid") or entry.get("link") or entry.get("title") or "")
    return hashlib.sha256((source_id + "|" + raw).encode("utf-8")).hexdigest()


def entry_timestamp(entry) -> int:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            return int(time.mktime(parsed))
        except Exception:
            pass
    return 0


async def fetch_source(session: aiohttp.ClientSession, source):
    headers = {"User-Agent": "VynexaNewsBot/1.0 (RSS reader; contact admin via Telegram)"}
    try:
        async with session.get(source["url"], headers=headers, timeout=aiohttp.ClientTimeout(total=18)) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
            raw = await resp.read()
        parsed = feedparser.parse(raw)
        if parsed.bozo and not parsed.entries:
            raise RuntimeError(str(parsed.bozo_exception)[:150])
        results = []
        for entry in parsed.entries[:30]:
            title = clean_html(entry.get("title", ""))
            link = str(entry.get("link") or "").strip()
            if not title or not link.startswith("http"):
                continue
            summary = clean_html(entry.get("summary", "") or entry.get("description", ""))
            results.append({
                "guid": entry_guid(source["id"], entry),
                "source_id": source["id"],
                "source_name": source["name"],
                "source_category": source["category"],
                "title": title[:500],
                "summary": summary[:1200],
                "link": link,
                "image": extract_image(entry),
                "timestamp": entry_timestamp(entry),
            })
        return source["id"], results, None
    except Exception as exc:
        return source["id"], [], f"{type(exc).__name__}: {str(exc)[:180]}"


async def translate_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    # Public Google Translate endpoint is used only as a no-key fallback.
    url = "https://translate.googleapis.com/translate_a/single"
    params = {"client": "gtx", "sl": "auto", "tl": "fa", "dt": "t", "q": text[:3500]}
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    return "".join(part[0] for part in data[0] if part and part[0]).strip()


async def make_persian_copy(item):
    title_fa = await translate_text(item["title"])
    summary_fa = ""
    excerpt = (item.get("summary") or "").strip()
    if excerpt:
        try:
            summary_fa = await translate_text(excerpt[:900])
        except Exception as exc:
            log.warning("RSS excerpt translation failed: %s", str(exc)[:120])
    return (title_fa or item["title"])[:320], summary_fa[:420]


def classify_item(item):
    if item["source_category"] == "iran":
        return "iran"
    text = (item["title"] + " " + item.get("summary", "")).lower()
    return "iran" if any(term in text for term in IRAN_TERMS) else "world"


async def was_seen(guid: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT 1 FROM seen_articles WHERE guid=?", (guid,)
        )).fetchone()
    return bool(row)


async def mark_seen(item, category):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_articles(guid,source_id,category,title,link,seen_at) VALUES(?,?,?,?,?,?)",
            (item["guid"], item["source_id"], category, item["title"], item["link"],
             datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def current_ratio_counts():
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT category FROM posted_articles ORDER BY id DESC LIMIT 20"
        )).fetchall()
    total = len(rows)
    iran = sum(1 for row in rows if row[0] == "iran")
    world = total - iran
    return iran, world, total


async def publish_item(item):
    destination = (await get_setting("channel", "")).strip()
    if not destination:
        log.warning("No destination channel configured; skipping article.")
        return False

    category = classify_item(item)
    title_fa, summary_fa = await make_persian_copy(item)
    label = "🇮🇷 ایران" if category == "iran" else "🌍 جهان"
    caption = (
        f"{label}\n\n"
        f"📰 {title_fa}\n"
    )
    if summary_fa:
        caption += f"\n{summary_fa}\n"
    caption += (
        f"\nمنبع: {item['source_name']}"
        f"\n🔗 {item['link']}"
    )
    caption = caption[:1020]

    post_images = await get_setting("post_images", POST_IMAGES_DEFAULT) == "1"
    try:
        if post_images and item.get("image"):
            await bot.send_photo(destination, photo=item["image"], caption=caption)
        else:
            await bot.send_message(destination, caption, disable_web_page_preview=True)
    except Exception as exc:
        log.warning("Image post failed; trying text fallback: %s", str(exc)[:180])
        try:
            await bot.send_message(destination, caption, disable_web_page_preview=True)
        except Exception:
            log.exception("Could not publish article")
            return False

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO posted_articles(guid,source_id,category,title,link,posted_at) VALUES(?,?,?,?,?,?)",
            (item["guid"], item["source_id"], category, title_fa, item["link"],
             datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    await mark_seen(item, category)
    return True


async def seed_existing_articles(session):
    """On first run, mark current feed contents as old to avoid flooding the channel."""
    if await get_setting("seeded", "0") == "1":
        return
    log.info("First run: indexing existing feed entries to avoid posting old news.")
    tasks = [fetch_source(session, source) for source in SOURCES]
    results = await asyncio.gather(*tasks)
    for source_id, entries, error in results:
        for item in entries:
            await mark_seen(item, classify_item(item))
        if error:
            log.warning("Seed feed %s: %s", source_id, error)
    await set_setting("seeded", "1")


async def poll_once():
    if await get_setting("enabled", "1") != "1":
        return {"fetched": 0, "posted": 0, "errors": []}
    destination = (await get_setting("channel", "")).strip()
    if not destination:
        return {"fetched": 0, "posted": 0, "errors": ["کانال مقصد تنظیم نشده است."]}

    enabled_sources = []
    for source in SOURCES:
        if await get_setting("source_" + source["id"], "1") == "1":
            enabled_sources.append(source)
    if not enabled_sources:
        return {"fetched": 0, "posted": 0, "errors": ["هیچ منبعی فعال نیست."]}

    timeout = aiohttp.ClientTimeout(total=22)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await seed_existing_articles(session)
        results = await asyncio.gather(
            *(fetch_source(session, source) for source in enabled_sources)
        )

    candidates, errors = [], []
    fetched = 0
    for source_id, entries, error in results:
        fetched += len(entries)
        if error:
            errors.append(f"{source_id}: {error}")
        for item in entries:
            if not await was_seen(item["guid"]):
                item["category"] = classify_item(item)
                candidates.append(item)

    candidates.sort(key=lambda x: x["timestamp"])
    iran_items = [x for x in candidates if x["category"] == "iran"]
    world_items = [x for x in candidates if x["category"] == "world"]

    iran_count, world_count, total_count = await current_ratio_counts()
    iran_share = (iran_count / total_count) if total_count else 1.0
    iran_target = int(await get_setting("iran_percent", "80"))
    world_target = 100 - iran_target

    # Prefer Iran news until the rolling share reaches the configured target.
    # World news is held back while its rolling share is above its target.
    chosen = iran_items[:MAX_POSTS_PER_CYCLE]
    remaining_slots = MAX_POSTS_PER_CYCLE - len(chosen)
    if remaining_slots > 0:
        _, _, recent_total = await current_ratio_counts()
        world_allowed = (
            not iran_items or
            (recent_total >= 5 and iran_share >= iran_target / 100) or
            (world_count < max(1, round((recent_total + 1) * world_target / 100)))
        )
        if world_allowed:
            chosen.extend(world_items[:remaining_slots])

    posted = 0
    for item in chosen:
        try:
            if await publish_item(item):
                posted += 1
            else:
                errors.append(f"ارسال ناموفق: {item['title'][:80]}")
        except Exception as exc:
            log.exception("Article processing failed")
            errors.append(f"{item['source_name']}: {type(exc).__name__}")
        await asyncio.sleep(1.2)
    return {"fetched": fetched, "posted": posted, "errors": errors}


async def news_loop():
    while True:
        try:
            result = await poll_once()
            if result["posted"] or result["errors"]:
                log.info("News poll: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("News poll crashed")
        try:
            interval = max(30, min(3600, int(await get_setting("interval", str(DEFAULT_INTERVAL)))))
        except Exception:
            interval = DEFAULT_INTERVAL
        await asyncio.sleep(interval)


async def set_bot_commands():
    await bot.set_my_commands([
        BotCommand(command="start", description="شروع خبرگزاری"),
        BotCommand(command="panel", description="پنل مدیریت"),
    ], scope=BotCommandScopeDefault())


@dp.message(CommandStart())
async def start(message: Message):
    text = (
        "📰 <b>خبرگزاری خودکار</b>\n\n"
        "این ربات خبرهای منابع انتخاب‌شده را دریافت می‌کند، "
        "به فارسی برمی‌گرداند و در کانال مدیر منتشر می‌کند.\n\n"
        "🇮🇷 هدف پوشش: ۸۰٪ ایران\n"
        "🌍 هدف پوشش: ۲۰٪ جهان"
    )
    if await is_admin(message.from_user.id):
        await message.answer(text + "\n\nبرای تنظیمات از /panel استفاده کن.", parse_mode="HTML")
    else:
        await message.answer(text, parse_mode="HTML")


@dp.message(Command("panel", "admin"))
async def panel(message: Message):
    if not await is_admin(message.from_user.id):
        return await message.answer("⛔ این بخش فقط برای مدیر رباته.")
    await message.answer("🛠 <b>پنل مدیریت خبرگزاری</b>", reply_markup=admin_keyboard(), parse_mode="HTML")


@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("عملیات لغو شد.", reply_markup=admin_keyboard() if await is_admin(message.from_user.id) else None)


@dp.callback_query(F.data.startswith("adm:"))
async def admin_callbacks(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        return await call.answer("دسترسی غیرمجاز", show_alert=True)
    action = call.data.split(":", 1)[1]
    await call.answer()

    if action == "panel":
        await state.clear()
        return await call.message.edit_text("🛠 <b>پنل مدیریت خبرگزاری</b>", reply_markup=admin_keyboard(), parse_mode="HTML")

    if action == "channel":
        await state.set_state(AdminFlow.channel)
        current = await get_setting("channel", "")
        return await call.message.edit_text(
            "📢 آیدی یا لینک کانال عمومی رو بفرست.\nمثال: @my_channel\n"
            "ربات باید در کانال ادمین و دارای اجازه ارسال پست باشه.\n\n"
            f"مقدار فعلی: {current or 'تنظیم نشده'}\nبرای لغو /cancel",
            reply_markup=back_keyboard(),
        )

    if action == "interval":
        await state.set_state(AdminFlow.interval)
        current = await get_setting("interval", str(DEFAULT_INTERVAL))
        return await call.message.edit_text(
            f"⏱ فاصله بررسی فعلی: {current} ثانیه\nعدد بین ۳۰ تا ۳۶۰۰ بفرست.",
            reply_markup=back_keyboard(),
        )

    if action == "toggle":
        current = await get_setting("enabled", "1")
        new_value = "0" if current == "1" else "1"
        await set_setting("enabled", new_value)
        label = "روشن" if new_value == "1" else "خاموش"
        return await call.message.edit_text(
            f"✅ انتشار خودکار اکنون {label} است.",
            reply_markup=admin_keyboard(),
        )

    if action == "images":
        current = await get_setting("post_images", POST_IMAGES_DEFAULT)
        new_value = "0" if current == "1" else "1"
        await set_setting("post_images", new_value)
        label = "روشن" if new_value == "1" else "خاموش"
        note = "\nفقط وقتی روشنش کن که اجازه بازنشر تصویر منبع رو داشته باشی."
        return await call.message.edit_text(
            f"🖼 انتشار تصویر خبر: {label}{note}",
            reply_markup=admin_keyboard(),
        )

    if action == "ratio":
        return await call.message.edit_text(
            "🇮🇷 سهمیه هدف: ۸۰٪ اخبار ایران\n🌍 سهمیه هدف: ۲۰٪ اخبار جهان\n"
            "این نسبت با توجه به خبرهای موجود و در بازه آخرین پست‌ها اعمال می‌شه.",
            reply_markup=back_keyboard(),
        )

    if action == "sources":
        rows = []
        for source in SOURCES:
            enabled = await get_setting("source_" + source["id"], "1") == "1"
            mark = "🟢" if enabled else "⚪"
            rows.append([InlineKeyboardButton(
                text=f"{mark} {source['name']}",
                callback_data="src:" + source["id"],
            )])
        rows.append([InlineKeyboardButton(text="⬅️ پنل مدیریت", callback_data="adm:panel")])
        return await call.message.edit_text(
            "📰 منابع خبری\nروی هر منبع بزن تا فعال/غیرفعال بشه:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    if action == "test":
        await call.message.edit_text("🔎 در حال بررسی فیدهای خبری...")
        timeout = aiohttp.ClientTimeout(total=22)
        enabled_sources = []
        for source in SOURCES:
            if await get_setting("source_" + source["id"], "1") == "1":
                enabled_sources.append(source)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            results = await asyncio.gather(
                *(fetch_source(session, source) for source in enabled_sources)
            )
        lines = ["🔎 <b>نتیجه بررسی منابع</b>"]
        for source_id, entries, error in results:
            source = next(s for s in SOURCES if s["id"] == source_id)
            lines.append(
                f"{'🟢' if not error else '🔴'} {source['name']}: "
                f"{len(entries)} خبر" + (f" — {html.escape(error[:90])}" if error else "")
            )
        return await call.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=admin_keyboard())

    if action == "stats":
        async with aiosqlite.connect(DB_PATH) as db:
            total = (await (await db.execute("SELECT COUNT(*) FROM posted_articles")).fetchone())[0]
            iran = (await (await db.execute("SELECT COUNT(*) FROM posted_articles WHERE category='iran'")).fetchone())[0]
            world = total - iran
        enabled = await get_setting("enabled", "1") == "1"
        channel = await get_setting("channel", "") or "تنظیم نشده"
        interval = await get_setting("interval", str(DEFAULT_INTERVAL))
        ratio = round((iran / total) * 100) if total else 0
        return await call.message.edit_text(
            f"📊 <b>آمار خبرگزاری</b>\n\n"
            f"وضعیت انتشار: {'🟢 روشن' if enabled else '🔴 خاموش'}\n"
            f"کانال: {html.escape(channel)}\n"
            f"فاصله بررسی: {interval} ثانیه\n"
            f"کل پست‌ها: {total}\n"
            f"اخبار ایران: {iran} ({ratio}٪)\n"
            f"اخبار جهان: {world}",
            reply_markup=back_keyboard(), parse_mode="HTML",
        )


@dp.callback_query(F.data.startswith("src:"))
async def source_toggle(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        return await call.answer("دسترسی غیرمجاز", show_alert=True)
    source_id = call.data.split(":", 1)[1]
    source = next((s for s in SOURCES if s["id"] == source_id), None)
    if not source:
        return await call.answer("منبع پیدا نشد.", show_alert=True)
    key = "source_" + source_id
    value = await get_setting(key, "1")
    await set_setting(key, "0" if value == "1" else "1")
    await call.answer("وضعیت منبع تغییر کرد.")
    rows = []
    for item in SOURCES:
        enabled = await get_setting("source_" + item["id"], "1") == "1"
        rows.append([InlineKeyboardButton(
            text=f"{'🟢' if enabled else '⚪'} {item['name']}",
            callback_data="src:" + item["id"],
        )])
    rows.append([InlineKeyboardButton(text="⬅️ پنل مدیریت", callback_data="adm:panel")])
    await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.message(AdminFlow.channel)
async def save_channel(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    value = normalize_channel(message.text or "")
    if not value:
        return await message.answer("لینک یا آیدی معتبر بفرست؛ مثل @my_channel")
    await set_setting("channel", value)
    await state.clear()
    try:
        chat = await bot.get_chat(value)
        await message.answer(
            f"✅ کانال ثبت شد: {chat.title or value}\n"
            "مطمئن شو ربات در کانال ادمین و مجاز به ارسال پسته.",
            reply_markup=admin_keyboard(),
        )
    except Exception as exc:
        await message.answer(
            f"⚠️ آدرس ذخیره شد، اما بررسی کانال موفق نبود: {str(exc)[:180]}\n"
            "ربات رو به کانال اضافه کن و دسترسی ارسال پست بده.",
            reply_markup=admin_keyboard(),
        )


@dp.message(AdminFlow.interval)
async def save_interval(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    try:
        value = int((message.text or "").strip())
        if not 30 <= value <= 3600:
            raise ValueError
    except ValueError:
        return await message.answer("عدد باید بین ۳۰ تا ۳۶۰۰ ثانیه باشه.")
    await set_setting("interval", str(value))
    await state.clear()
    await message.answer(f"✅ فاصله بررسی روی {value} ثانیه تنظیم شد.", reply_markup=admin_keyboard())


async def health_server():
    async def health(_request):
        return web.json_response({"status": "ok", "service": "vynexa-news-bot"})
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health endpoint listening on port %s", port)
    return runner


async def main():
    await init_db()
    await set_bot_commands()
    me = await bot.get_me()
    log.info("Vynexa News Bot started as @%s", me.username)
    runner = None
    try:
        runner = await health_server()
    except OSError as exc:
        log.warning("Health server not started: %s", exc)
    background = asyncio.create_task(news_loop())
    try:
        await dp.start_polling(bot)
    finally:
        background.cancel()
        try:
            await background
        except asyncio.CancelledError:
            pass
        if runner:
            await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
