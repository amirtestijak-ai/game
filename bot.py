"""
Vynexa News Bot — automatic Persian news publisher
Python 3.10+
Install:
    pip install "aiogram>=3.7" aiosqlite aiohttp feedparser
Run:
    BOT_TOKEN=... ADMIN_ID=... python bot.py

Optional:
    OPENAI_API_KEY=...       # optional AI translation; RSS excerpts are translated when available
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
from html.parser import HTMLParser
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
    InputMediaPhoto, InputMediaVideo, BufferedInputFile,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
DB_PATH = os.getenv("DB_PATH", "secret_society.sqlite3")
DEFAULT_INTERVAL = 60
MAX_POSTS_PER_CYCLE = 5
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
TRANSLATION_LOCK = asyncio.Lock()
TRANSLATION_LAST_CALL = 0.0
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
        "id": "iranintl_fa", "name": "Iran International فارسی",
        "url": "https://www.iranintl.com/feed", "category": "iran",
    },
    {
        "id": "iranintl", "name": "Iran International English",
        "url": "https://www.iranintl.com/en/feed", "category": "iran",
    },
    {
        "id": "iranintl_liveblog", "name": "Iran International",
        "url": "https://www.iranintl.com/fa/liveblog/iran-conflict-november17",
        "category": "iran", "type": "liveblog",
    },
    # Extra discovery feeds ensure Iran International stories are not missed
    # when its general RSS feed is busy with other subjects.
    {
        "id": "iranintl_all", "name": "Iran International — همه خبرها",
        "url": "https://news.google.com/rss/search?q=site%3Airanintl.com%2Fen%2F&hl=en-US&gl=US&ceid=US:en",
        "category": "iran",
    },
    {
        "id": "iranintl_protests", "name": "Iran International — اعتراضات ایران",
        "url": "https://news.google.com/rss/search?q=site%3Airanintl.com+Iran+%28protest+OR+protests+OR+demonstration+OR+strike+OR+unrest%29&hl=en-US&gl=US&ceid=US:en",
        "category": "iran",
    },
    {
        "id": "iranintl_protesters", "name": "Iran International — معترضان و اعتصاب‌ها",
        "url": "https://news.google.com/rss/search?q=site%3Airanintl.com+%28Iranian+protesters+OR+Iran+strike+OR+student+protest+OR+workers+protest+OR+teachers+protest%29&hl=en-US&gl=US&ceid=US:en",
        "category": "iran",
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

# Protest-related coverage is deliberately prioritized over other Iran news.
PROTEST_TERMS = (
    "protest", "protests", "protester", "protesters", "demonstration",
    "demonstrations", "strike", "strikes", "walkout", "unrest", "rally",
    "retirees", "workers protest", "student protest", "teachers protest",
    "labor protest", "labor strike", "nationwide strike", "street protest",
    "march", "demonstrators", "unrest", "uprising",
    "اعتراض", "اعتراضات", "اعتراضی", "معترض", "معترضان", "تجمع",
    "تجمعات", "اعتصاب", "اعتصابات", "اعتصابی", "بازنشستگان",
    "دانشجویان", "کارگران", "فرهنگیان", "کسبه", "مردم معترض",
)

IMPORTANT_TERMS = (
    "government", "president", "parliament", "minister", "election", "sanction", "sanctions",
    "war", "attack", "missile", "nuclear", "security", "diplomacy", "diplomatic",
    "human rights", "detained", "arrested", "execution", "internet blackout", "shutdown",
    "oil", "inflation", "currency", "crisis", "resign", "resignation", "political",
    "دولت", "رئیس جمهور", "رییس جمهور", "مجلس", "وزیر", "انتخابات", "تحریم", "جنگ",
    "حمله", "موشک", "هسته ای", "امنیتی", "دیپلماسی", "حقوق بشر", "بازداشت", "اعدام",
    "قطع اینترنت", "اختلال اینترنت", "نفت", "تورم", "ارز", "بحران", "استعفا", "سیاسی",
)
LOW_VALUE_TERMS = (
    "recipe", "restaurant", "food trend", "celebrity style", "horoscope", "pet of the day",
    "mouse intestine", "روده موش", "دستور پخت", "رستوران", "مد و زیبایی", "فال روزانه",
    "حیوان خانگی", "سرگرمی روزمره", "آشپزی",
)


def is_protest_item(item):
    text = (item.get("title", "") + " " + item.get("summary", "")).casefold()
    return any(term.casefold() in text for term in PROTEST_TERMS)

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
        await db.execute(
            """CREATE TABLE IF NOT EXISTS translation_cache(
                cache_key TEXT PRIMARY KEY, source_text TEXT NOT NULL,
                translated_text TEXT NOT NULL, created_at TEXT NOT NULL
            )"""
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
    # Earlier builds could leave this switch disabled in the persistent database.
    # Re-enable once on startup so existing deployments resume photo posts.
    if await get_setting("post_images", "1") != "1":
        await set_setting("post_images", "1")
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
    # RSS feeds expose images in several incompatible places.
    candidates = []
    candidates.extend(entry.get("media_content", []) or [])
    candidates.extend(entry.get("media_thumbnail", []) or [])
    candidates.extend(entry.get("enclosures", []) or [])
    for group in entry.get("media_groups", []) or []:
        candidates.extend(group.get("contents", []) or [])
        candidates.extend(group.get("thumbnails", []) or [])
    for item in candidates:
        url = item.get("url") or item.get("href")
        kind = (item.get("type") or "").lower()
        medium = (item.get("medium") or "").lower()
        if url and url.startswith("https://") and (kind.startswith("image/") or medium == "image" or not kind and not medium):
            return url
    # Some publishers put the image only inside the HTML description/content.
    html_fields = [entry.get("summary", "")]
    html_fields.extend(x.get("value", "") for x in (entry.get("content", []) or []))
    for fragment in html_fields:
        match = re.search(r"<img\b[^>]*?src=[\"\'](https?://[^\"\']+)[\"\']", str(fragment or ""), re.I)
        if match:
            return html.unescape(match.group(1))
    return None


def extract_video(entry):
    """Return only a direct video file URL advertised by the feed."""
    candidates = list(entry.get("media_content", []) or [])
    candidates += list(entry.get("enclosures", []) or [])
    for item in candidates:
        url = item.get("url") or item.get("href")
        kind = (item.get("type") or "").lower()
        medium = (item.get("medium") or "").lower()
        if url and url.startswith("https://") and (kind.startswith("video/") or medium == "video"):
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


class LiveBlogParser(HTMLParser):
    """Extract update cards from a liveblog page without assuming a site CSS class."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.current = None
        self.capture = None
        self.capture_parts = []
        self.in_article = 0
        self.pending_date = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "article":
            self.in_article += 1
        if tag in ("h2", "h3", "h4"):
            self._finish_capture()
            self._finish_block()
            self.current = {"title": "", "parts": [], "date": self.pending_date, "image": None, "video": None}
            self.pending_date = ""
            self.capture, self.capture_parts = "title", []
        elif tag == "time":
            self._finish_capture()
            self.capture = "date"
            self.capture_parts = []
            if attrs.get("datetime"):
                if self.current is not None:
                    self.current["date"] = attrs["datetime"]
                else:
                    self.pending_date = attrs["datetime"]
        elif tag == "p" and self.current is not None:
            self._finish_capture()
            self.capture = "text"
            self.capture_parts = []
        elif tag == "img" and self.current is not None:
            src = attrs.get("src") or attrs.get("data-src") or attrs.get("data-lazy-src")
            if src and src.startswith("http") and not self.current["image"]:
                self.current["image"] = src
        elif tag in ("video", "source") and self.current is not None:
            src = attrs.get("src")
            if src and src.startswith("http") and (src.lower().split("?",1)[0].endswith((".mp4", ".webm", ".mov"))):
                self.current["video"] = src

    def handle_endtag(self, tag):
        if tag in ("h2", "h3", "h4", "p", "time"):
            self._finish_capture()
        if tag == "article":
            self.in_article = max(0, self.in_article - 1)
            if self.current:
                self._finish_block()

    def handle_data(self, data):
        if not data.strip():
            return
        if self.capture:
            self.capture_parts.append(data.strip())
        elif self.current is not None:
            # Some liveblog layouts put update text in nested divs, not <p> tags.
            self.current["parts"].append(data.strip())

    def _finish_capture(self):
        if not self.capture:
            return
        value = re.sub(r"\s+", " ", " ".join(self.capture_parts)).strip()
        if self.current and value:
            if self.capture == "title":
                self.current["title"] = value
            elif self.capture == "date":
                if self.current is not None and not self.current["date"]:
                    self.current["date"] = value
                elif self.current is None:
                    self.pending_date = value
            elif self.capture == "text":
                self.current["parts"].append(value)
        self.capture, self.capture_parts = None, []

    def _finish_block(self):
        if self.current:
            self.blocks.append(self.current)
            self.current = None

    def finish(self):
        self._finish_capture()
        self._finish_block()
        return self.blocks


def liveblog_timestamp(value):
    if not value:
        return int(time.time())
    try:
        from email.utils import parsedate_to_datetime
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        try:
            dt = parsedate_to_datetime(value)
            return int(dt.timestamp())
        except Exception:
            return int(time.time())


async def fetch_liveblog(session, source):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; VynexaNewsBot/1.0)"}
    async with session.get(source["url"], headers=headers, timeout=aiohttp.ClientTimeout(total=22)) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status}")
        raw = await resp.text(errors="replace")
    parser = LiveBlogParser()
    parser.feed(raw)
    blocks = parser.finish()
    results, seen_titles = [], set()
    for block in blocks:
        title = clean_html(block.get("title", ""))
        summary = " ".join(block.get("parts", []))
        if len(title) < 12 or not summary or title.casefold() in seen_titles:
            continue
        seen_titles.add(title.casefold())
        # Ignore the page-level heading and generic navigation headings.
        if title.casefold() in ("تغطية مباشرة", "آخرین اخبار", "آخرین خبرها"):
            continue
        stamp = liveblog_timestamp(block.get("date"))
        identity = "|".join((source["url"], block.get("date", ""), title, summary[:180]))
        guid = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        results.append({
            "guid": guid, "source_id": source["id"], "source_name": source["name"],
            "source_category": source["category"], "title": title[:500],
            "summary": summary[:1200], "link": source["url"],
            "image": block.get("image"), "video": block.get("video"), "timestamp": stamp,
        })
    if not results:
        raise RuntimeError("Liveblog page loaded, but no update cards could be parsed")
    return source["id"], results[:40], None


class MetaMediaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.image = None
        self.video = None
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            prop = (a.get("property") or a.get("name") or "").lower()
            content = a.get("content", "").strip()
            if content.startswith("http"):
                if prop in ("og:image", "twitter:image", "twitter:image:src") and not self.image:
                    self.image = content
                if prop in ("og:video", "og:video:url", "og:video:secure_url") and not self.video:
                    self.video = content
        elif tag == "video" and not self.video:
            self.video = a.get("src")


async def enrich_media(session, item):
    if item.get("image") or item.get("video"):
        return item
    link = item.get("link", "")
    if not link.startswith("http"):
        return item
    try:
        async with session.get(link, headers={"User-Agent": "Mozilla/5.0 VynexaNewsBot"},
                              timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status >= 400:
                return item
            body = await resp.text(errors="replace")
        parser = MetaMediaParser()
        parser.feed(body[:1500000])
        item["image"] = parser.image
        item["video"] = parser.video
    except Exception as exc:
        log.debug("Media metadata unavailable for %s: %s", link, str(exc)[:100])
    return item


def concise_summary(text, limit=360):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # Prefer a complete sentence; if there is no sentence boundary, end at a word.
    ends = [cut.rfind(mark) for mark in (".", "!", "?", "؟", "؛")]
    end = max(ends)
    if end >= int(limit * 0.55):
        return cut[:end + 1].strip()
    return cut.rsplit(" ", 1)[0].rstrip(" ،؛:") + "…"


def is_important_item(item):
    text = (item.get("title", "") + " " + item.get("summary", "")).casefold()
    if any(term.casefold() in text for term in LOW_VALUE_TERMS):
        return False
    return (is_protest_item(item) or any(term.casefold() in text for term in IMPORTANT_TERMS)
            or item.get("source_id") == "iranintl_liveblog")


def source_display_name(item):
    sid = item.get("source_id", "")
    if sid.startswith("iranintl"):
        return "Iran International"
    return item.get("source_name") or sid


async def fetch_source(session: aiohttp.ClientSession, source):
    if source.get("type") == "liveblog":
        try:
            return await fetch_liveblog(session, source)
        except Exception as exc:
            return source["id"], [], f"{type(exc).__name__}: {str(exc)[:180]}"
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
            # Prefer the publisher's full RSS content, then its summary/description.
            # Many feeds put the useful explanation in content:encoded rather than summary.
            summary_parts = []
            for content_item in (entry.get("content", []) or []):
                value = clean_html(content_item.get("value", ""))
                if value and value not in summary_parts:
                    summary_parts.append(value)
            for field in (entry.get("summary", ""), entry.get("description", ""),
                          entry.get("subtitle", "")):
                value = clean_html(field)
                if value and value not in summary_parts:
                    summary_parts.append(value)
            summary = max(summary_parts, key=len, default="")
            results.append({
                "guid": entry_guid(source["id"], entry),
                "source_id": source["id"],
                "source_name": source["name"],
                "source_category": source["category"],
                "title": title[:500],
                "summary": summary[:1200],
                "link": link,
                "image": extract_image(entry),
                "video": extract_video(entry),
                "timestamp": entry_timestamp(entry),
            })
        return source["id"], results, None
    except Exception as exc:
        return source["id"], [], f"{type(exc).__name__}: {str(exc)[:180]}"


async def cached_translation(text: str):
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT translated_text FROM translation_cache WHERE cache_key=?", (key,)
        )).fetchone()
    return row[0] if row else None


async def save_translation(text: str, translated: str):
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO translation_cache(cache_key,source_text,translated_text,created_at) "
            "VALUES(?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET translated_text=excluded.translated_text",
            (key, text, translated, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def translate_with_openai(text: str) -> str:
    """Optional AI translation. It is used only when OPENAI_API_KEY is configured."""
    timeout = aiohttp.ClientTimeout(total=35)
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_MODEL,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "Translate the supplied news text into fluent, neutral Persian. Preserve names, numbers and facts. Do not add facts or commentary. Return only the Persian translation."},
            {"role": "user", "content": text[:3000]},
        ],
    }
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload) as resp:
            if resp.status == 429:
                raise RuntimeError("OpenAI rate limit (429)")
            resp.raise_for_status()
            data = await resp.json()
    result = data["choices"][0]["message"]["content"].strip()
    if not result:
        raise RuntimeError("Empty AI translation")
    return result


async def translate_with_mymemory(text: str) -> str:
    """Secondary no-key translation provider used when Google is rate-limited."""
    timeout = aiohttp.ClientTimeout(total=25)
    params = {"q": text[:450], "langpair": "en|fa"}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get("https://api.mymemory.translated.net/get", params=params) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    if data.get("responseStatus") != 200:
        raise RuntimeError("MyMemory translation unavailable")
    result = (data.get("responseData") or {}).get("translatedText", "").strip()
    result = html.unescape(result)
    if not result or result.lower() == text.lower():
        raise RuntimeError("MyMemory returned no translation")
    return result


async def translate_text(text: str) -> str:
    global TRANSLATION_LAST_CALL
    text = (text or "").strip()
    if not text:
        return ""
    text = text[:3000]
    cached = await cached_translation(text)
    if cached:
        return cached

    async with TRANSLATION_LOCK:
        # Re-check after acquiring the lock; another coroutine may have filled it.
        cached = await cached_translation(text)
        if cached:
            return cached

        if OPENAI_API_KEY:
            try:
                translated = await translate_with_openai(text)
                await save_translation(text, translated)
                return translated
            except Exception as exc:
                log.warning("AI translation failed; falling back to Google: %s", str(exc)[:150])

        # Serialize public Google requests and back off automatically on 429/5xx.
        url = "https://translate.googleapis.com/translate_a/single"
        params = {"client": "gtx", "sl": "auto", "tl": "fa", "dt": "t", "q": text}
        timeout = aiohttp.ClientTimeout(total=20)
        last_error = None
        for attempt in range(4):
            wait = max(0.0, 2.5 - (time.monotonic() - TRANSLATION_LAST_CALL))
            if wait:
                await asyncio.sleep(wait)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, params=params) as resp:
                        TRANSLATION_LAST_CALL = time.monotonic()
                        if resp.status == 429 or resp.status >= 500:
                            retry_after = resp.headers.get("Retry-After", "")
                            try:
                                delay = min(45.0, max(5.0, float(retry_after)))
                            except (TypeError, ValueError):
                                delay = min(45.0, 5.0 * (2 ** attempt))
                            last_error = RuntimeError(f"Google Translate HTTP {resp.status}")
                            if attempt < 3:
                                log.warning("Translation rate limited; retrying in %.0fs", delay)
                                await asyncio.sleep(delay)
                                continue
                            raise last_error
                        resp.raise_for_status()
                        data = await resp.json(content_type=None)
                translated = "".join(part[0] for part in data[0] if part and part[0]).strip()
                if translated:
                    await save_translation(text, translated)
                    return translated
                raise RuntimeError("Empty translation response")
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                last_error = exc
                if attempt < 3 and not (isinstance(exc, RuntimeError) and "HTTP 429" in str(exc)):
                    await asyncio.sleep(min(20, 2 ** (attempt + 1)))
        # Last no-key fallback. MyMemory has smaller limits, so keep requests short.
        try:
            translated = await translate_with_mymemory(text[:450])
            await save_translation(text, translated)
            return translated
        except Exception as exc:
            log.warning("MyMemory translation fallback failed: %s", str(exc)[:140])
        raise RuntimeError(f"Translation unavailable: {last_error}")


async def make_persian_copy(item):
    # Translate title and excerpt together when AI is configured to reduce API calls.
    title = item["title"]
    excerpt = (item.get("summary") or "").strip()[:900]
    if OPENAI_API_KEY:
        combined = "HEADLINE: " + title + ("\nEXCERPT: " + excerpt if excerpt else "")
        try:
            translated = await translate_text(combined)
            if "\n" in translated:
                first, rest = translated.split("\n", 1)
                first = re.sub(r"^(عنوان|HEADLINE)\s*:\s*", "", first, flags=re.I).strip()
                rest = re.sub(r"^(خلاصه|متن|EXCERPT)\s*:\s*", "", rest, flags=re.I).strip()
                return first[:320] or title, concise_summary(rest, 360)
            return translated[:320] or title, ""
        except Exception as exc:
            log.warning("Combined AI translation failed: %s", str(exc)[:140])

    try:
        title_fa = await translate_text(title)
    except Exception as exc:
        log.warning("Title translation unavailable; publishing original title: %s", str(exc)[:140])
        title_fa = title
    summary_fa = ""
    if excerpt:
        try:
            summary_fa = await translate_text(excerpt)
        except Exception as exc:
            log.warning("RSS excerpt translation unavailable; publishing without excerpt: %s", str(exc)[:140])
    return (title_fa or title)[:320], concise_summary(summary_fa, 360)


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
    # HTML link keeps the original URL hidden behind the short word «مشاهده».
    safe_title = html.escape(title_fa)
    safe_summary = html.escape(summary_fa)
    safe_source = html.escape(source_display_name(item))
    safe_link = html.escape(item["link"], quote=True)
    # Automatically read the destination channel username/ID and append it under every post.
    try:
        channel_chat = await bot.get_chat(destination)
        channel_id_text = "@" + channel_chat.username if channel_chat.username else str(channel_chat.id)
    except Exception:
        channel_id_text = destination
    safe_channel_id = html.escape(channel_id_text)
    caption = f"{label}\n\n📰 {safe_title}"
    if safe_summary:
        caption += f"\n\n{html.escape(concise_summary(summary_fa, 360))}"
    caption += f"\n\nمنبع: {safe_source}\n🔗 <a href=\"{safe_link}\">مشاهده</a>\n\n🛑 {safe_channel_id}"

    # If RSS omitted media, inspect the publisher article page for OpenGraph image/video.
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=16)) as media_session:
        await enrich_media(media_session, item)
    video_url = item.get("video")
    image_url = item.get("image")
    try:
        # Download media to memory first; this avoids Telegram failing on publisher hotlink URLs.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35)) as media_session:
            if video_url:
                async with media_session.get(video_url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    resp.raise_for_status()
                    data = await resp.read()
                if len(data) <= 48 * 1024 * 1024:
                    await bot.send_video(destination, BufferedInputFile(data, filename="news.mp4"), caption=caption, parse_mode="HTML")
                elif image_url:
                    video_url = None
            if not video_url and image_url:
                async with media_session.get(image_url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    resp.raise_for_status()
                    data = await resp.read()
                if len(data) <= 9 * 1024 * 1024:
                    await bot.send_photo(destination, BufferedInputFile(data, filename="news.jpg"), caption=caption, parse_mode="HTML")
                else:
                    await bot.send_message(destination, caption, parse_mode="HTML", disable_web_page_preview=True)
            elif not video_url and not image_url:
                await bot.send_message(destination, caption, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as exc:
        log.warning("Media upload failed for %s: %s", item.get("link"), str(exc)[:180])
        # Do not pretend a media upload succeeded; use a link preview only as last resort.
        try:
            await bot.send_message(destination, caption, parse_mode="HTML", disable_web_page_preview=False)
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
            if item.get("source_id") != "iranintl_liveblog":
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

    # Reject low-value items (celebrity/food/oddity filler) and keep important politics,
    # protests, security, diplomacy and major public-affairs stories.
    candidates = [item for item in candidates if is_important_item(item)]

    # The same story can appear in the official RSS and several discovery feeds.
    # Deduplicate by canonical article URL before selecting posts.
    unique = {}
    for item in candidates:
        # Every update in one liveblog shares the page URL; dedupe those by update GUID.
        canonical = (item["guid"] if item.get("source_id") == "iranintl_liveblog"
                     else item["link"].split("?", 1)[0].rstrip("/"))
        current = unique.get(canonical)
        official_rank = 0 if item["source_id"].startswith("iranintl") else 1
        current_rank = (0 if current and current["source_id"].startswith("iranintl") else 1) if current else 9
        if current is None or official_rank < current_rank:
            unique[canonical] = item
    candidates = list(unique.values())

    # Priority: Iran protests first across every outlet; then major Iran political news;
    # then major international political/current-affairs stories. Do not let one outlet monopolize.
    def rank(item):
        protest = is_protest_item(item)
        iran = item["category"] == "iran"
        if protest and iran:
            return 0
        if iran:
            return 1
        return 2

    candidates.sort(key=lambda x: (rank(x), -x["timestamp"]))
    chosen = candidates[:MAX_POSTS_PER_CYCLE]

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
