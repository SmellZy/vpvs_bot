"""
Telegram бот для редагування персональних даних у банківських PDF виписках.
Підтримувані банки: Monobank, UnexBank, PrivatBank
"""

import os, io, re, logging, sqlite3
from pathlib import Path
from datetime import datetime, date
from contextlib import contextmanager

import pikepdf
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from pypdf import PdfReader, PdfWriter

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, CallbackQueryHandler,
    filters, ContextTypes,
)

# ── Логування ──────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# ── Конфіг ─────────────────────────────────────────────────────────────────
# Канали для обов'язкової підписки.
# Формат env: "channel_id|Назва|https://t.me/...;channel_id2|Назва2|url2"
# Приклад: "@mychannel|Мій канал|https://t.me/mychannel;-1001234567890|Приватний|https://t.me/+xxx"
_CHANNELS_RAW = os.getenv("REQUIRED_CHANNELS", "")
REQUIRED_CHANNELS: list[tuple[str, str, str]] = []
for _entry in _CHANNELS_RAW.split(";"):
    _parts = _entry.strip().split("|")
    if len(_parts) == 3:
        REQUIRED_CHANNELS.append((_parts[0].strip(), _parts[1].strip(), _parts[2].strip()))

# ID адмінів через кому: "123456789,987654321"
ADMIN_IDS: set[int] = {
    int(i) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip().isdigit()
}

DB_PATH       = os.getenv("DB_PATH", "bot.db")
BASE_DAILY_LIMIT = 1  # базовий денний ліміт виписок

# ── Шрифт ──────────────────────────────────────────────────────────────────
_FONT_PATHS = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]
FONT_PATH = next((p for p in _FONT_PATHS if Path(p).exists()), None)
if FONT_PATH:
    pdfmetrics.registerFont(TTFont("CB", FONT_PATH))
    PDF_FONT = "CB"
else:
    PDF_FONT = "Helvetica-Bold"

_FONT_REG_PATHS = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
]
FONT_REG_PATH = next((p for p in _FONT_REG_PATHS if Path(p).exists()), None)
if FONT_REG_PATH:
    pdfmetrics.registerFont(TTFont("CR", FONT_REG_PATH))
    PDF_FONT_REG = "CR"
else:
    PDF_FONT_REG = "Helvetica"

# ── Стани ──────────────────────────────────────────────────────────────────
(
    MAIN_MENU,
    CHOOSE_MODE, CHOOSE_BANK, WAIT_PDF,
    ASK_NAME, ASK_DOB, ASK_TIN, ASK_DOC_NUMBER,
    ASK_ISSUED_BY, ASK_ISSUE_DATE, ASK_ADDRESS, ASK_IBAN,
    CONFIRM,
    ASK_PROMO_INPUT,
) = range(14)

# ── Кнопки головного меню ───────────────────────────────────────────────────
BTN_STATEMENT = "📄 Нова виписка"
BTN_REFERRAL  = "🎁 Реферали"
BTN_PROMO     = "💰 Промокод"
BTN_LIMIT     = "📊 Мій ліміт"
_MENU_BTNS    = {BTN_STATEMENT, BTN_REFERRAL, BTN_PROMO, BTN_LIMIT}

MAIN_KB = ReplyKeyboardMarkup(
    [[BTN_STATEMENT], [BTN_REFERRAL, BTN_PROMO, BTN_LIMIT]],
    resize_keyboard=True,
    is_persistent=True,
)

# ── Шаблони банків ──────────────────────────────────────────────────────────
TEMPLATE_DIR = Path("/app/templates")
TEMPLATE_DIR.mkdir(exist_ok=True)

BANK_TEMPLATES = {
    "monobank":   {"name": "Monobank",   "emoji": "🟡", "file": "monobank.pdf"},
    "unex":       {"name": "UnexBank",   "emoji": "🔵", "file": "unex.pdf"},
    "privatbank": {"name": "PrivatBank", "emoji": "🟢", "file": "privatbank.pdf"},
}

# ── Конфіг полів для кожного банку ─────────────────────────────────────────
BANK_FIELDS = {
    "monobank": {
        "page_h": 839.055,
        "page_w": 595.275,
        "personal_stream_indices": [],
        "fields": {
            "name":        (59,   718),
            "dob":         (87,   705),
            "tin":         (49,   691),
            "doc_number":  (221,  663),
            "issued_by":   (76,   650),
            "issue_date":  (90,   636),
            "address":     (124,  623),
            "iban":        (56,   582),
        },
        "field_rects": {
            "name":        (250, 12),
            "dob":         (90,  12),
            "tin":         (90,  12),
            "doc_number":  (100, 12),
            "issued_by":   (55,  12),
            "issue_date":  (90,  12),
            "address":     (445, 12),
            "iban":        (250, 12),
        },
        "font_size": 9,
        "use_stream_clear": False,
    },
    "unex": {
        "page_h": 842.4,
        "page_w": 595.4,
        "personal_stream_indices": [],
        "fields": {
            "name":        (87.9,  707.8),
            "dob":         (111.4, 697.0),
            "tin":         (85.2,  686.2),
            "doc_number":  (220.2, 658.6),
            "issued_by":   (99.4,  647.8),
            "issue_date":  (112.4, 637.0),
            "address":     (138.1, 625.9),
            "iban":        (88.8,  586.3),
        },
        "field_rects": {
            "name":        (250, 10),
            "dob":         (80,  10),
            "tin":         (80,  10),
            "doc_number":  (80,  10),
            "issued_by":   (60,  10),
            "issue_date":  (80,  10),
            "address":     (350, 10),
            "iban":        (200, 10),
        },
        "font_size": 8.5,
        "use_stream_clear": False,
    },
    "privatbank": {
        "page_h": 841.880,
        "page_w": 595.280,
        "personal_stream_indices": [],
        "stream_ys": [652.28, 640.2, 628.13, 612.3, 600.22, 588.15, 576.08, 520.27],
        "fields": {
            "name":        (86.51,  652.28),
            "dob":         (73.99,  640.20),
            "tin":         (39.49,  628.13),
            "doc_number":  (210.00, 612.30),
            "issued_by":   (90.49,  600.22),
            "issue_date":  (77.51,  588.15),
            "address":     (107.51, 576.08),
            "iban":        (45.98,  520.27),
        },
        "field_rects": {
            "name":        (200, 11),
            "dob":         (60,  11),
            "tin":         (65,  11),
            "doc_number":  (310, 11),
            "issued_by":   (40,  11),
            "issue_date":  (60,  11),
            "address":     (430, 11),
            "iban":        (165, 11),
        },
        "font_size": 9.0,
        "use_stream_clear": False,
        "use_stream_tj_remove": True,
    },
}

FIELD_DEFS = [
    ("name",       "👤 ПІБ (латиницею):",              "KOVALENKO PETRO"),
    ("dob",        "🎂 Дата народження (ДД.ММ.РРРР):", "15.03.1990"),
    ("tin",        "🔢 ІПН (10 цифр):",                "1234567890"),
    ("doc_number", "📄 Серія/номер документа:",         "987654"),
    ("issued_by",  "🏛️ Ким виданий (код):",             "1234"),
    ("issue_date", "📅 Дата видачі (ДД.ММ.РРРР):",     "01.01.2020"),
    ("address",    "🏠 Адреса реєстрації:",
                   "Ukraine, city Lviv, street Franka, build 5, 79000"),
    ("iban",       "🏦 IBAN:",                          "UA123456789012345678901234567"),
]

FIELD_KEYS   = [f[0] for f in FIELD_DEFS]
FIELD_STATES = [
    ASK_NAME, ASK_DOB, ASK_TIN, ASK_DOC_NUMBER,
    ASK_ISSUED_BY, ASK_ISSUE_DATE, ASK_ADDRESS, ASK_IBAN,
]

_MENU_FILTER = filters.Regex(
    f"^({'|'.join(re.escape(b) for b in _MENU_BTNS)})$"
)

# ── Database ────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT    DEFAULT '',
                first_name  TEXT    DEFAULT '',
                extra_limit INTEGER DEFAULT 0,
                created_at  TEXT    DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS referrals (
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER PRIMARY KEY,
                created_at  TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS daily_usage (
                user_id INTEGER NOT NULL,
                date    TEXT    NOT NULL,
                count   INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date)
            );
            CREATE TABLE IF NOT EXISTS promo_codes (
                code       TEXT    PRIMARY KEY,
                bonus      INTEGER NOT NULL,
                uses_left  INTEGER DEFAULT -1,
                created_at TEXT    DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS user_promos (
                user_id INTEGER NOT NULL,
                code    TEXT    NOT NULL,
                bonus   INTEGER NOT NULL,
                used_at TEXT    DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, code)
            );
        """)


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def db_ensure_user(user_id: int, username: str, first_name: str):
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?,?,?)",
            (user_id, username, first_name),
        )
        conn.execute(
            "UPDATE users SET username=?, first_name=? WHERE user_id=?",
            (username, first_name, user_id),
        )


def db_record_referral(referrer_id: int, referred_id: int) -> bool:
    """Returns True if referral was newly recorded."""
    if referrer_id == referred_id:
        return False
    with _db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM referrals WHERE referred_id=?", (referred_id,)
        ).fetchone()
        if exists:
            return False
        conn.execute(
            "INSERT OR IGNORE INTO referrals (referrer_id, referred_id) VALUES (?,?)",
            (referrer_id, referred_id),
        )
        return True


def db_get_referral_count(user_id: int) -> int:
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM referrals WHERE referrer_id=?", (user_id,)
        ).fetchone()
        return row["cnt"] if row else 0


def db_get_user_extra(user_id: int) -> int:
    with _db() as conn:
        row = conn.execute(
            "SELECT extra_limit FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        return row["extra_limit"] if row else 0


def db_set_user_extra(user_id: int, value: int):
    with _db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, extra_limit) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET extra_limit=excluded.extra_limit",
            (user_id, value),
        )


def db_add_user_extra(user_id: int, delta: int):
    with _db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, extra_limit) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET extra_limit=MAX(0, extra_limit+?)",
            (user_id, max(0, delta), delta),
        )


def db_get_promo_bonus(user_id: int) -> int:
    with _db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(bonus), 0) AS total FROM user_promos WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return row["total"] if row else 0


def db_get_daily_limit(user_id: int) -> int:
    refs  = db_get_referral_count(user_id)
    extra = db_get_user_extra(user_id)
    promo = db_get_promo_bonus(user_id)
    return BASE_DAILY_LIMIT + refs + extra + promo


def db_get_daily_usage(user_id: int) -> int:
    today = date.today().isoformat()
    with _db() as conn:
        row = conn.execute(
            "SELECT count FROM daily_usage WHERE user_id=? AND date=?",
            (user_id, today),
        ).fetchone()
        return row["count"] if row else 0


def db_increment_daily_usage(user_id: int):
    today = date.today().isoformat()
    with _db() as conn:
        conn.execute(
            "INSERT INTO daily_usage (user_id, date, count) VALUES (?,?,1) "
            "ON CONFLICT(user_id, date) DO UPDATE SET count=count+1",
            (user_id, today),
        )


def db_apply_promo(user_id: int, code: str) -> tuple[bool, str, int]:
    """Returns (success, message, bonus)."""
    code = code.upper().strip()
    with _db() as conn:
        promo = conn.execute(
            "SELECT bonus, uses_left FROM promo_codes WHERE code=?", (code,)
        ).fetchone()
        if not promo:
            return False, "Промокод не знайдено.", 0
        already = conn.execute(
            "SELECT 1 FROM user_promos WHERE user_id=? AND code=?", (user_id, code)
        ).fetchone()
        if already:
            return False, "Ви вже використовували цей промокод.", 0
        if promo["uses_left"] == 0:
            return False, "Промокод вичерпано (ліміт використань закінчився).", 0

        conn.execute(
            "INSERT INTO user_promos (user_id, code, bonus) VALUES (?,?,?)",
            (user_id, code, promo["bonus"]),
        )
        if promo["uses_left"] > 0:
            conn.execute(
                "UPDATE promo_codes SET uses_left=uses_left-1 WHERE code=?", (code,)
            )
    return True, f"Промокод активовано! +{promo['bonus']} виписок до денного ліміту.", promo["bonus"]


def db_create_promo(code: str, bonus: int, uses_left: int = -1):
    code = code.upper().strip()
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO promo_codes (code, bonus, uses_left) VALUES (?,?,?)",
            (code, bonus, uses_left),
        )


def db_delete_promo(code: str) -> bool:
    code = code.upper().strip()
    with _db() as conn:
        cur = conn.execute("DELETE FROM promo_codes WHERE code=?", (code,))
        return cur.rowcount > 0


def db_list_promos() -> list:
    with _db() as conn:
        return conn.execute(
            "SELECT code, bonus, uses_left, created_at FROM promo_codes ORDER BY created_at DESC"
        ).fetchall()


def db_get_user_stats(user_id: int) -> dict:
    with _db() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        refs = conn.execute(
            "SELECT COUNT(*) AS cnt FROM referrals WHERE referrer_id=?", (user_id,)
        ).fetchone()
        promo_total = conn.execute(
            "SELECT COALESCE(SUM(bonus), 0) AS total FROM user_promos WHERE user_id=?",
            (user_id,),
        ).fetchone()
        usage_today = conn.execute(
            "SELECT COALESCE(count, 0) AS cnt FROM daily_usage WHERE user_id=? AND date=?",
            (user_id, date.today().isoformat()),
        ).fetchone()
    return {
        "user":        dict(user) if user else {},
        "referrals":   refs["cnt"]         if refs         else 0,
        "promo_bonus": promo_total["total"] if promo_total  else 0,
        "today_usage": usage_today["cnt"]   if usage_today  else 0,
        "daily_limit": db_get_daily_limit(user_id),
    }


def db_list_users(limit: int = 20) -> list:
    with _db() as conn:
        return conn.execute(
            "SELECT user_id, username, first_name, extra_limit, created_at "
            "FROM users ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


# ── Subscription gate ───────────────────────────────────────────────────────

async def get_unsubscribed_channels(bot, user_id: int) -> list[dict]:
    """Returns list of channels the user is NOT subscribed to."""
    if not REQUIRED_CHANNELS:
        return []
    not_subbed = []
    for ch_id, ch_name, ch_url in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(ch_id, user_id)
            if member.status in ("left", "kicked", "banned"):
                not_subbed.append({"id": ch_id, "name": ch_name, "url": ch_url})
        except Exception as e:
            log.warning("Can't check subscription for %s: %s", ch_id, e)
            not_subbed.append({"id": ch_id, "name": ch_name, "url": ch_url})
    return not_subbed


async def _send_subscription_gate(message, not_subbed: list[dict]):
    lines = ["📢 *Для доступу до бота підпишись на канали:*\n"]
    for ch in not_subbed:
        lines.append(f"• {ch['name']}")
    lines.append("\nПісля підписки натисни кнопку нижче ↓")

    kb = [[InlineKeyboardButton(f"📣 {ch['name']}", url=ch["url"])] for ch in not_subbed]
    kb.append([InlineKeyboardButton("✅ Я підписався", callback_data="check_sub")])

    await message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )


# ── PDF editing ─────────────────────────────────────────────────────────────

def _remove_tj_at_ys(pdf: pikepdf.Pdf, page_idx: int, stream_ys: list) -> None:
    """Видаляє перший Tj (значення поля) для кожного Y у content stream."""
    page = pdf.pages[page_idx]
    content = page["/Contents"]
    raw = content.read_bytes().decode("latin-1", errors="replace")

    modified = raw
    for sy in stream_ys:
        sy_pattern = str(sy).rstrip("0").rstrip(".")
        pattern = rf"(1 0 0 1 [\d.]+ {re.escape(sy_pattern)}0* Tm\n)(\([^)]*\)Tj)"
        modified = re.sub(pattern, lambda m: m.group(1) + "()Tj", modified, count=1)

    content.write(modified.encode("latin-1", errors="replace"))


def edit_pdf(pdf_bytes: bytes, bank: str, fields: dict) -> bytes:
    cfg    = BANK_FIELDS[bank]
    PAGE_H = cfg["page_h"]
    PAGE_W = cfg["page_w"]

    pdf = pikepdf.open(io.BytesIO(pdf_bytes))

    if cfg["use_stream_clear"] and cfg["personal_stream_indices"]:
        page        = pdf.pages[0]
        content_obj = page["/Contents"]
        streams     = list(content_obj) if isinstance(content_obj, pikepdf.Array) else [content_obj]
        total       = len(streams)
        for idx in cfg["personal_stream_indices"]:
            if idx >= total:
                log.warning("Stream %d out of range (total=%d), skip", idx, total)
                continue
            try:
                pdf.get_object(streams[idx].objgen).write(b"", filter=pikepdf.Name("/FlateDecode"))
            except Exception as e:
                log.warning("Stream %d clear failed: %s", idx, e)

    if cfg.get("use_stream_tj_remove") and cfg.get("stream_ys"):
        try:
            _remove_tj_at_ys(pdf, 0, cfg["stream_ys"])
        except Exception as e:
            log.warning("Tj removal failed: %s", e)

    try:
        with pdf.open_metadata() as meta:
            meta.clear()
    except Exception:
        pass
    try:
        if "/Info" in pdf.trailer:
            info = pdf.get_object(pdf.trailer["/Info"].objgen)
            for key in list(info.keys()):
                try:
                    del info[key]
                except Exception:
                    pass
    except Exception:
        pass

    buf = io.BytesIO()
    pdf.save(buf)
    buf.seek(0)

    field_positions = cfg["fields"]
    field_rects     = cfg.get("field_rects", {})
    font_size       = cfg["font_size"]

    # Малюємо overlay тільки якщо є хоча б одне непусте поле
    has_values = any(fields.get(k, "") for k in field_positions)

    base_reader = PdfReader(buf)
    writer      = PdfWriter()

    if has_values:
        overlay_buf = io.BytesIO()
        c = canvas.Canvas(overlay_buf, pagesize=(PAGE_W, PAGE_H))

        for key, (x, y) in field_positions.items():
            value = fields.get(key, "")
            if not value:
                continue
            if not cfg["use_stream_clear"] and key in field_rects:
                w_r, h_r = field_rects[key]
                c.setFillColorRGB(1, 1, 1)
                c.rect(x, y - 2, w_r, h_r + 2, fill=1, stroke=0)
            c.setFillColorRGB(0, 0, 0)
            c.setFont(PDF_FONT_REG, font_size)
            c.drawString(x, y, value)

        c.save()
        overlay_buf.seek(0)
        overlay_reader = PdfReader(overlay_buf)

        for i, pg in enumerate(base_reader.pages):
            if i == 0:
                pg.merge_page(overlay_reader.pages[0])
            writer.add_page(pg)
    else:
        for pg in base_reader.pages:
            writer.add_page(pg)

    writer.add_metadata({
        "/Producer": "", "/Creator": "", "/Author": "",
        "/Title": "", "/Subject": "", "/Keywords": "",
    })

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ── Helpers ──────────────────────────────────────────────────────────────────

def detect_bank(pdf_bytes: bytes) -> str | None:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text   = (reader.pages[0].extract_text() or "").lower()
        if "universal bank" in text or "monobank" in text:
            return "monobank"
        if "юнекс банк" in text or "unexbank" in text or "unex bank" in text:
            return "unex"
        if "privatbank" in text or "приватбанк" in text or "privat24" in text:
            return "privatbank"
    except Exception:
        pass
    return None


def _ask_field_text(idx: int) -> str:
    key, question, example = FIELD_DEFS[idx]
    step  = idx + 1
    total = len(FIELD_DEFS)
    return (
        f"[{step}/{total}] {question}\n"
        f"_Приклад: {example}_\n\n"
        f"Надішли /skip щоб пропустити."
    )


async def _ref_link(ctx: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    me = await ctx.bot.get_me()
    return f"https://t.me/{me.username}?start=ref_{user_id}"


# ── Handlers ─────────────────────────────────────────────────────────────────

async def _show_main_menu(message, user) -> int:
    """Надсилає головне меню з постійною клавіатурою."""
    used  = db_get_daily_usage(user.id)
    limit = db_get_daily_limit(user.id)
    await message.reply_text(
        f"👋 Привіт, *{user.first_name}*!\n\n"
        f"📊 Ліміт сьогодні: {used}/{limit} виписок\n\n"
        "Вибери дію 👇",
        reply_markup=MAIN_KB,
        parse_mode="Markdown",
    )
    return MAIN_MENU


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    db_ensure_user(user.id, user.username or "", user.first_name or "")

    # Реферальне посилання /start ref_<id>
    for arg in (ctx.args or []):
        if arg.startswith("ref_"):
            try:
                referrer_id = int(arg[4:])
                if db_record_referral(referrer_id, user.id):
                    log.info("Referral recorded: %d → %d", referrer_id, user.id)
            except (ValueError, Exception) as e:
                log.warning("Referral parse error: %s", e)

    # Перевірка підписки на канали
    not_subbed = await get_unsubscribed_channels(ctx.bot, user.id)
    if not_subbed:
        await _send_subscription_gate(update.message, not_subbed)
        return MAIN_MENU

    ctx.user_data.clear()
    return await _show_main_menu(update.message, user)


async def cb_check_sub(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробляє натискання '✅ Я підписався'."""
    q    = update.callback_query
    user = update.effective_user
    db_ensure_user(user.id, user.username or "", user.first_name or "")

    not_subbed = await get_unsubscribed_channels(ctx.bot, user.id)
    if not_subbed:
        await q.answer("❌ Ти ще не підписаний на всі канали!", show_alert=True)
        return MAIN_MENU

    await q.answer("✅ Підписку підтверджено!")
    await q.edit_message_text("✅ Підписку підтверджено!")
    return await _show_main_menu(q.message, user)


# ── Головне меню — обробка кнопок ───────────────────────────────────────────

async def main_menu_dispatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Диспетчер кнопок головного меню (у стані MAIN_MENU і як fallback)."""
    text = (update.message.text or "").strip()
    user = update.effective_user
    db_ensure_user(user.id, user.username or "", user.first_name or "")

    # Перевірка підписки
    not_subbed = await get_unsubscribed_channels(ctx.bot, user.id)
    if not_subbed:
        await _send_subscription_gate(update.message, not_subbed)
        return MAIN_MENU

    if text == BTN_STATEMENT:
        ctx.user_data.clear()
        kb = [
            [InlineKeyboardButton("📁 Завантажити свій PDF", callback_data="mode_upload")],
            [InlineKeyboardButton("🏦 Вибрати шаблон банку", callback_data="mode_template")],
        ]
        await update.message.reply_text(
            "📄 *Нова виписка*\n\nЯк хочеш отримати PDF?",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown",
        )
        return CHOOSE_MODE

    elif text == BTN_REFERRAL:
        refs  = db_get_referral_count(user.id)
        limit = db_get_daily_limit(user.id)
        used  = db_get_daily_usage(user.id)
        ref   = await _ref_link(ctx, user.id)
        await update.message.reply_text(
            f"🎁 *Реферальна програма*\n\n"
            f"За кожного запрошеного друга — *+1 виписка на день*.\n\n"
            f"👥 Запрошено: *{refs} осіб*\n"
            f"📊 Твій ліміт: *{limit}* (використано сьогодні: {used})\n\n"
            f"🔗 Твоє посилання:\n`{ref}`",
            reply_markup=MAIN_KB,
            parse_mode="Markdown",
        )
        return MAIN_MENU

    elif text == BTN_PROMO:
        await update.message.reply_text(
            "💰 *Активація промокоду*\n\nВведи код:",
            reply_markup=MAIN_KB,
            parse_mode="Markdown",
        )
        return ASK_PROMO_INPUT

    elif text == BTN_LIMIT:
        stats = db_get_user_stats(user.id)
        refs  = stats["referrals"]
        extra = stats["user"].get("extra_limit", 0)
        promo = stats["promo_bonus"]
        limit = stats["daily_limit"]
        used  = stats["today_usage"]
        ref   = await _ref_link(ctx, user.id)

        lines = [
            "📊 *Твій денний ліміт*\n",
            f"• Базовий: {BASE_DAILY_LIMIT}",
            f"• Реферали ({refs} осіб): +{refs}",
        ]
        if extra: lines.append(f"• Від адміна: +{extra}")
        if promo: lines.append(f"• Промокоди: +{promo}")
        lines += [
            f"\n✅ *Всього: {limit} виписок/день*",
            f"📅 Сьогодні використано: {used}/{limit}",
            f"\n🔗 Реферальне посилання:\n`{ref}`",
        ]
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=MAIN_KB,
            parse_mode="Markdown",
        )
        return MAIN_MENU

    # Невідомий текст — показуємо меню знову
    return await _show_main_menu(update.message, user)


async def handle_promo_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробляє введений промокод у стані ASK_PROMO_INPUT."""
    text = (update.message.text or "").strip()
    user = update.effective_user

    # Якщо натиснув кнопку меню — передаємо далі
    if text in _MENU_BTNS:
        return await main_menu_dispatch(update, ctx)

    success, msg, _ = db_apply_promo(user.id, text)
    if success:
        new_limit = db_get_daily_limit(user.id)
        await update.message.reply_text(
            f"✅ {msg}\n📊 Новий денний ліміт: *{new_limit} виписок*",
            reply_markup=MAIN_KB,
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"❌ {msg}\n\nСпробуй ще або натисни кнопку меню.",
            reply_markup=MAIN_KB,
        )
    return MAIN_MENU


async def cb_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q    = update.callback_query
    await q.answer()
    mode = q.data.split("_")[1]
    ctx.user_data["mode"] = mode

    if mode == "upload":
        await q.edit_message_text("📎 Надішли PDF-файл виписки.")
        return WAIT_PDF
    else:
        kb = [
            [InlineKeyboardButton("🟡 Monobank",   callback_data="bank_monobank")],
            [InlineKeyboardButton("🔵 UnexBank",   callback_data="bank_unex")],
            [InlineKeyboardButton("🟢 PrivatBank", callback_data="bank_privatbank")],
        ]
        await q.edit_message_text("🏦 Вибери банк:", reply_markup=InlineKeyboardMarkup(kb))
        return CHOOSE_BANK


async def cb_bank(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q    = update.callback_query
    await q.answer()
    bank = q.data.split("_", 1)[1]
    ctx.user_data["bank"] = bank

    tpl_path = TEMPLATE_DIR / BANK_TEMPLATES[bank]["file"]
    if not tpl_path.exists():
        await q.edit_message_text(
            f"❌ Шаблон {BANK_TEMPLATES[bank]['name']} не знайдено на сервері.\n"
            "Завантаж свій PDF через /start → 'Завантажити свій PDF'."
        )
        return ConversationHandler.END

    ctx.user_data["pdf_bytes"]  = tpl_path.read_bytes()
    ctx.user_data["field_idx"]  = 0
    ctx.user_data["fields"]     = {}
    await q.edit_message_text(
        f"✅ Шаблон {BANK_TEMPLATES[bank]['emoji']} {BANK_TEMPLATES[bank]['name']} вибрано.\n\n"
        + _ask_field_text(0)
    )
    return FIELD_STATES[0]


async def receive_pdf(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    db_ensure_user(user.id, user.username or "", user.first_name or "")

    # Перевірка підписки
    not_subbed = await get_unsubscribed_channels(ctx.bot, user.id)
    if not_subbed:
        await _send_subscription_gate(update.message, not_subbed)
        return MAIN_MENU

    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❗ Надішли PDF-файл.")
        return WAIT_PDF

    file = await ctx.bot.get_file(doc.file_id)
    buf  = io.BytesIO()
    await file.download_to_memory(buf)
    pdf_bytes = buf.getvalue()

    bank = detect_bank(pdf_bytes)
    ctx.user_data["pdf_bytes"] = pdf_bytes
    ctx.user_data["bank"]      = bank
    ctx.user_data["fields"]    = {}
    ctx.user_data["field_idx"] = 0

    bank_name = BANK_TEMPLATES.get(bank, {}).get("name", "Невідомий") if bank else "❓"
    if bank:
        await update.message.reply_text(
            f"✅ PDF отримано. Визначено банк: *{bank_name}*\n\n" + _ask_field_text(0),
            parse_mode="Markdown",
        )
        return FIELD_STATES[0]
    else:
        kb = [
            [InlineKeyboardButton("🟡 Monobank",   callback_data="detect_monobank")],
            [InlineKeyboardButton("🔵 UnexBank",   callback_data="detect_unex")],
            [InlineKeyboardButton("🟢 PrivatBank", callback_data="detect_privatbank")],
        ]
        await update.message.reply_text(
            "🤔 Не вдалося визначити банк. Вибери вручну:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return CHOOSE_BANK


async def cb_detect_bank(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q    = update.callback_query
    await q.answer()
    bank = q.data.split("_", 1)[1]
    ctx.user_data["bank"]      = bank
    ctx.user_data["fields"]    = {}
    ctx.user_data["field_idx"] = 0
    await q.edit_message_text(_ask_field_text(0))
    return FIELD_STATES[0]


async def _handle_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    idx  = ctx.user_data.get("field_idx", 0)
    key  = FIELD_KEYS[idx]

    if text != "/skip":
        ctx.user_data.setdefault("fields", {})[key] = text

    next_idx = idx + 1
    ctx.user_data["field_idx"] = next_idx

    if next_idx >= len(FIELD_DEFS):
        return await _show_confirm(update, ctx)

    await update.message.reply_text(_ask_field_text(next_idx), parse_mode="Markdown")
    return FIELD_STATES[next_idx]


async def ask_name(u, c):       return await _handle_field(u, c)
async def ask_dob(u, c):        return await _handle_field(u, c)
async def ask_tin(u, c):        return await _handle_field(u, c)
async def ask_doc_number(u, c): return await _handle_field(u, c)
async def ask_issued_by(u, c):  return await _handle_field(u, c)
async def ask_issue_date(u, c): return await _handle_field(u, c)
async def ask_address(u, c):    return await _handle_field(u, c)
async def ask_iban(u, c):       return await _handle_field(u, c)


async def _show_confirm(update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    fields    = ctx.user_data.get("fields", {})
    bank      = ctx.user_data.get("bank", "?")
    bank_name = BANK_TEMPLATES.get(bank, {}).get("name", bank)

    lines = [f"📋 Перевір дані для {bank_name}:\n"]
    for key, question, _ in FIELD_DEFS:
        val = fields.get(key) or "— не змінено —"
        lines.append(f"• {question.rstrip(':')}: {val}")
    lines.append("\n/confirm — застосувати\n/restart — почати спочатку")

    msg = update.message if update.message else update.callback_query.message
    await msg.reply_text("\n".join(lines))
    return CONFIRM


async def cmd_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user      = update.effective_user
    pdf_bytes = ctx.user_data.get("pdf_bytes")
    fields    = ctx.user_data.get("fields", {})
    bank      = ctx.user_data.get("bank", "monobank")

    if not pdf_bytes:
        await update.message.reply_text("❗ PDF не знайдено. /start")
        return ConversationHandler.END

    # Перевірка денного ліміту
    used_today  = db_get_daily_usage(user.id)
    daily_limit = db_get_daily_limit(user.id)
    if used_today >= daily_limit:
        ref = await _ref_link(ctx, user.id)
        await update.message.reply_text(
            f"❌ *Денний ліміт вичерпано* ({used_today}/{daily_limit} виписок)\n\n"
            f"🎁 *Як отримати більше виписок:*\n"
            f"• Запрошуй друзів — кожен дає +1 виписку на день\n"
            f"• Активуй промокод: /promo КОД\n\n"
            f"🔗 Реферальне посилання:\n`{ref}`",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    await update.message.reply_text("⏳ Обробляю файл...")

    try:
        result_bytes = edit_pdf(pdf_bytes, bank, fields)
    except Exception as e:
        log.exception("PDF edit failed")
        await update.message.reply_text(f"❌ Помилка:\n{e}")
        return ConversationHandler.END

    # Зараховуємо використання тільки після успіху
    db_increment_daily_usage(user.id)

    bank_name = BANK_TEMPLATES.get(bank, {}).get("name", bank)
    new_used  = used_today + 1
    remaining = daily_limit - new_used

    caption = f"✅ Готово! Виписка {bank_name} відредагована."
    if remaining > 0:
        caption += f"\n📊 Ліміт: {new_used}/{daily_limit} (залишилось {remaining})"
    else:
        caption += f"\n⚠️ Денний ліміт вичерпано ({daily_limit}/{daily_limit})"

    await update.message.reply_document(
        document=io.BytesIO(result_bytes),
        filename=f"edited_{bank}_{datetime.now().strftime('%d%m%Y_%H%M')}.pdf",
        caption=caption,
    )
    ctx.user_data.clear()
    return await _show_main_menu(update.message, user)


async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    return await _show_main_menu(update.message, update.effective_user)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    return await _show_main_menu(update.message, update.effective_user)


# ── Команди користувача (legacy — залишені для сумісності) ───────────────────

async def cmd_promo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Активує промокод: /promo КОД (legacy command)"""
    user = update.effective_user
    db_ensure_user(user.id, user.username or "", user.first_name or "")
    args = ctx.args
    if not args:
        await update.message.reply_text("💰 Введи: `/promo КОД`", parse_mode="Markdown")
        return
    success, msg, _ = db_apply_promo(user.id, args[0])
    if success:
        new_limit = db_get_daily_limit(user.id)
        await update.message.reply_text(
            f"✅ {msg}\n📊 Новий ліміт: *{new_limit} виписок*", parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"❌ {msg}")


# ── Адмін-панель ──────────────────────────────────────────────────────────────

_ADMIN_HELP = (
    "🔧 *Адмін-панель*\n\n"
    "Управління лімітами:\n"
    "`/admin setlimit USER\\_ID N` — встановити екстра-ліміт\n"
    "`/admin addlimit USER\\_ID N` — додати до ліміту (N може бути від'ємним)\n\n"
    "Промокоди:\n"
    "`/admin addpromo КОД БОНУС [USES]` — створити промокод\n"
    "  USES: к-сть використань, -1 = без ліміту (за замовч.)\n"
    "`/admin delpromo КОД` — видалити промокод\n"
    "`/admin listpromos` — список всіх промокодів\n\n"
    "Статистика:\n"
    "`/admin stats USER\\_ID` — статистика конкретного користувача\n"
    "`/admin users` — останні 20 користувачів"
)


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Немає доступу.")
        return

    args = ctx.args or []
    if not args:
        await update.message.reply_text(_ADMIN_HELP, parse_mode="Markdown")
        return

    cmd = args[0].lower()

    # ── setlimit ──────────────────────────────────────────────────────────────
    if cmd == "setlimit":
        if len(args) < 3:
            await update.message.reply_text("Використання: /admin setlimit USER_ID N")
            return
        try:
            target, value = int(args[1]), int(args[2])
            db_set_user_extra(target, value)
            await update.message.reply_text(
                f"✅ extra\\_limit={value} для `{target}`\n"
                f"Денний ліміт: {db_get_daily_limit(target)}",
                parse_mode="Markdown",
            )
        except (ValueError, Exception) as e:
            await update.message.reply_text(f"❌ Помилка: {e}")

    # ── addlimit ──────────────────────────────────────────────────────────────
    elif cmd == "addlimit":
        if len(args) < 3:
            await update.message.reply_text("Використання: /admin addlimit USER_ID N")
            return
        try:
            target, delta = int(args[1]), int(args[2])
            db_add_user_extra(target, delta)
            await update.message.reply_text(
                f"✅ Додано {delta:+d} до extra\\_limit для `{target}`\n"
                f"Денний ліміт: {db_get_daily_limit(target)}",
                parse_mode="Markdown",
            )
        except (ValueError, Exception) as e:
            await update.message.reply_text(f"❌ Помилка: {e}")

    # ── addpromo ──────────────────────────────────────────────────────────────
    elif cmd == "addpromo":
        if len(args) < 3:
            await update.message.reply_text("Використання: /admin addpromo КОД БОНУС [USES]")
            return
        try:
            code  = args[1].upper()
            bonus = int(args[2])
            uses  = int(args[3]) if len(args) >= 4 else -1
            db_create_promo(code, bonus, uses)
            uses_str = str(uses) if uses >= 0 else "∞"
            await update.message.reply_text(
                f"✅ Промокод `{code}` створено\n"
                f"Бонус: +{bonus} виписок, Використань: {uses_str}",
                parse_mode="Markdown",
            )
        except (ValueError, Exception) as e:
            await update.message.reply_text(f"❌ Помилка: {e}")

    # ── delpromo ──────────────────────────────────────────────────────────────
    elif cmd == "delpromo":
        if len(args) < 2:
            await update.message.reply_text("Використання: /admin delpromo КОД")
            return
        code = args[1].upper()
        ok   = db_delete_promo(code)
        await update.message.reply_text(
            f"✅ Промокод `{code}` видалено." if ok else f"❌ Промокод `{code}` не знайдено.",
            parse_mode="Markdown",
        )

    # ── listpromos ────────────────────────────────────────────────────────────
    elif cmd == "listpromos":
        promos = db_list_promos()
        if not promos:
            await update.message.reply_text("Промокодів немає.")
            return
        lines = ["📋 *Промокоди:*\n"]
        for p in promos:
            uses_str = str(p["uses_left"]) if p["uses_left"] >= 0 else "∞"
            lines.append(f"• `{p['code']}` — +{p['bonus']} вип., залишилось: {uses_str}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    # ── stats ─────────────────────────────────────────────────────────────────
    elif cmd == "stats":
        if len(args) < 2:
            await update.message.reply_text("Використання: /admin stats USER_ID")
            return
        try:
            target = int(args[1])
            stats  = db_get_user_stats(target)
            u      = stats["user"]
            if not u:
                await update.message.reply_text(f"❌ Користувача {target} не знайдено.")
                return
            lines = [
                f"👤 *Статистика `{target}`*\n",
                f"Username: @{u.get('username', '—')}",
                f"Ім'я: {u.get('first_name', '—')}",
                f"Реєстрація: {str(u.get('created_at', ''))[:10]}",
                f"Рефералів: {stats['referrals']}",
                f"Промо-бонус: +{stats['promo_bonus']}",
                f"Адмін-бонус: +{u.get('extra_limit', 0)}",
                f"Денний ліміт: {stats['daily_limit']}",
                f"Сьогодні використано: {stats['today_usage']}",
            ]
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        except (ValueError, Exception) as e:
            await update.message.reply_text(f"❌ Помилка: {e}")

    # ── users ─────────────────────────────────────────────────────────────────
    elif cmd == "users":
        users = db_list_users()
        if not users:
            await update.message.reply_text("Користувачів немає.")
            return
        lines = ["👥 *Останні 20 користувачів:*\n"]
        for u in users:
            name = f"@{u['username']}" if u["username"] else (u["first_name"] or "—")
            lines.append(
                f"• `{u['user_id']}` {name}"
                f" (extra: {u['extra_limit']}, {str(u['created_at'])[:10]})"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    else:
        await update.message.reply_text("❓ Невідома команда.\n\n" + _ADMIN_HELP, parse_mode="Markdown")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    init_db()
    log.info("DB initialized at %s", DB_PATH)

    app = Application.builder().token(BOT_TOKEN).build()

    # Хендлери для кожного поля (меню-кнопки як fallback всередині поля)
    def _field_handlers(fn):
        return [
            MessageHandler(_MENU_FILTER,                     main_menu_dispatch),
            MessageHandler(filters.TEXT & ~filters.COMMAND,  fn),
            CommandHandler("skip", fn),
        ]

    field_handlers = {
        ASK_NAME:       _field_handlers(ask_name),
        ASK_DOB:        _field_handlers(ask_dob),
        ASK_TIN:        _field_handlers(ask_tin),
        ASK_DOC_NUMBER: _field_handlers(ask_doc_number),
        ASK_ISSUED_BY:  _field_handlers(ask_issued_by),
        ASK_ISSUE_DATE: _field_handlers(ask_issue_date),
        ASK_ADDRESS:    _field_handlers(ask_address),
        ASK_IBAN:       _field_handlers(ask_iban),
    }

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            MessageHandler(filters.Document.PDF,  receive_pdf),
            MessageHandler(_MENU_FILTER,           main_menu_dispatch),
        ],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(cb_check_sub,  pattern="^check_sub$"),
                MessageHandler(_MENU_FILTER,         main_menu_dispatch),
                MessageHandler(filters.Document.PDF, receive_pdf),
            ],
            CHOOSE_MODE: [
                CallbackQueryHandler(cb_check_sub, pattern="^check_sub$"),
                CallbackQueryHandler(cb_mode,      pattern="^mode_"),
                MessageHandler(_MENU_FILTER,        main_menu_dispatch),
            ],
            CHOOSE_BANK: [
                CallbackQueryHandler(cb_bank,        pattern="^bank_"),
                CallbackQueryHandler(cb_detect_bank, pattern="^detect_"),
                MessageHandler(_MENU_FILTER,          main_menu_dispatch),
            ],
            WAIT_PDF: [
                MessageHandler(filters.Document.PDF, receive_pdf),
                MessageHandler(_MENU_FILTER,          main_menu_dispatch),
            ],
            CONFIRM: [
                CommandHandler("confirm", cmd_confirm),
                CommandHandler("restart", cmd_restart),
                MessageHandler(_MENU_FILTER, main_menu_dispatch),
            ],
            ASK_PROMO_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_promo_input),
            ],
            **field_handlers,
        },
        fallbacks=[
            CommandHandler("cancel",  cmd_cancel),
            CommandHandler("restart", cmd_restart),
            CommandHandler("start",   cmd_start),
            MessageHandler(_MENU_FILTER, main_menu_dispatch),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    # /admin залишається поза ConversationHandler (адмін команда)
    app.add_handler(CommandHandler("admin", cmd_admin))

    log.info("🤖 Бот запущено. Адміни: %s", ADMIN_IDS)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
