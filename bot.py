"""
Telegram бот для редагування персональних даних у банківських PDF виписках.
Підтримувані банки: Monobank, UnexBank, PrivatBank
"""

import os, io, re, logging, random, copy
from pathlib import Path
from datetime import datetime, timedelta

import pikepdf
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from pypdf import PdfReader, PdfWriter

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, CallbackQueryHandler,
    filters, ContextTypes,
)

# ── Логування ──────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

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
    CHOOSE_MODE, CHOOSE_BANK, WAIT_PDF,
    ASK_NAME, ASK_DOB, ASK_TIN, ASK_DOC_NUMBER,
    ASK_ISSUED_BY, ASK_ISSUE_DATE, ASK_ADDRESS, ASK_IBAN,
    ASK_MOCK_TXN, CONFIRM,
) = range(13)

# ── Шаблони банків (вбудовані PDF як base64 або None якщо треба завантажити) ──
# Тут ми тримаємо шляхи до шаблонів (копіюємо при старті)
TEMPLATE_DIR = Path("/app/templates")
TEMPLATE_DIR.mkdir(exist_ok=True)

BANK_TEMPLATES = {
    "monobank":  {"name": "Monobank",   "emoji": "🟡", "file": "monobank.pdf"},
    "unex":      {"name": "UnexBank",   "emoji": "🔵", "file": "unex.pdf"},
    "privatbank":{"name": "PrivatBank", "emoji": "🟢", "file": "privatbank.pdf"},
}

# ── Конфіг полів для кожного банку ─────────────────────────────────────────
# Формат: (x0, top, x1, bottom) в координатах pdfplumber (top від верху)
BANK_FIELDS = {
    "monobank": {
        "page_h": 839.055,
        "page_w": 595.275,
        "personal_stream_indices": list(range(10, 19)),
        "fields": {
            # (rl_x, rl_y) в reportlab координатах (від низу)
            "name":        (59,  718),
            "dob":         (87,  705),
            "tin":         (49,  691),
            "doc_number":  (221, 663),
            "issued_by":   (76,  650),
            "issue_date":  (87,  636),
            "address":     (124, 623),
            "iban":        (56,  582),
        },
        "font_size": 9,
        "use_stream_clear": True,
    },
    "unex": {
        "page_h": 842.4,
        "page_w": 595.4,
        "personal_stream_indices": [],
        "fields": {
            "name":        (87.9,  705.3),
            "dob":         (111.4, 694.5),
            "tin":         (85.2,  683.7),
            "doc_number":  (220.2, 656.1),
            "issued_by":   (99.4,  645.3),
            "issue_date":  (112.4, 634.5),
            "address":     (138.1, 623.4),
            "iban":        (88.8,  583.8),
        },
        # Розміри білих прямокутників (width, height)
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
            # X і Y взяті напряму з content stream (baseline)
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

FIELD_KEYS  = [f[0] for f in FIELD_DEFS]
FIELD_STATES = [ASK_NAME, ASK_DOB, ASK_TIN, ASK_DOC_NUMBER,
                ASK_ISSUED_BY, ASK_ISSUE_DATE, ASK_ADDRESS, ASK_IBAN]

# ── Mock транзакції ─────────────────────────────────────────────────────────
MOCK_DESCRIPTIONS_MONO = [
    ("Cafes and restaurants", "5812"),
    ("Products and supermarkets", "5411"),
    ("Products and supermarkets", "5499"),
    ("Money transfer", "4829"),
    ("Card top-up", "6012"),
    ("Card top-up", "4829"),
    ("Clothing and shoes", "5651"),
    ("Beauty and health", "5912"),
    ("Transport", "4111"),
    ("Other", "4215"),
]

def generate_mock_transactions_monobank(count: int = 5):
    """Генерує список mock транзакцій для Monobank з правильним балансом."""
    txns = []
    balance = round(random.uniform(500, 3000), 2)
    
    now = datetime.now()
    for i in range(count):
        date = now - timedelta(days=random.randint(1, 60))
        date_str = date.strftime("%d.%m.%Y\n%H:%M:%S")
        
        desc, mcc = random.choice(MOCK_DESCRIPTIONS_MONO)
        
        if "top-up" in desc or "transfer" in desc and random.random() > 0.5:
            amount = round(random.uniform(500, 5000), 2)
            sign = 1
        else:
            amount = round(random.uniform(50, 800), 2)
            sign = -1
        
        op_amount = amount * sign
        balance += op_amount
        balance = round(balance, 2)
        
        txns.append({
            "date": date_str,
            "description": desc,
            "mcc": mcc,
            "amount": f"{op_amount:,.2f}".replace(',', ' '),
            "balance": f"{balance:,.2f}".replace(',', ' '),
        })
    
    return txns, round(balance, 2)

# ── PDF editing ─────────────────────────────────────────────────────────────

def _draw_mock_transactions_monobank(c, page_h, page_w):
    """Перекриває першу сторінку таблиці транзакцій Monobank новими даними."""
    # Таблиця транзакцій на першій сторінці починається приблизно з y=520 (rl)
    # і закінчується внизу. Заливаємо білим і малюємо нові рядки.
    TABLE_TOP    = 520.0   # reportlab y (від низу) де починається таблиця
    TABLE_BOTTOM = 30.0
    TABLE_LEFT   = 28.0
    TABLE_RIGHT  = 567.0

    # Білий прямокутник поверх всієї таблиці
    c.setFillColorRGB(1, 1, 1)
    c.rect(TABLE_LEFT, TABLE_BOTTOM, TABLE_RIGHT - TABLE_LEFT,
           TABLE_TOP - TABLE_BOTTOM, fill=1, stroke=0)

    # Генеруємо транзакції
    txns, final_balance = generate_mock_transactions_monobank(random.randint(8, 14))

    # Малюємо рядки
    row_h  = 28.0
    y_cur  = TABLE_TOP - row_h
    c.setFillColorRGB(0, 0, 0)

    COL_DATE  = TABLE_LEFT + 2
    COL_DESC  = TABLE_LEFT + 95
    COL_MCC   = TABLE_LEFT + 260
    COL_AMT   = TABLE_LEFT + 310
    COL_BAL   = TABLE_LEFT + 420

    for txn in txns:
        if y_cur < TABLE_BOTTOM + 15:
            break

        # Лінія розділювача
        c.setStrokeColorRGB(0.85, 0.85, 0.85)
        c.line(TABLE_LEFT, y_cur + row_h, TABLE_RIGHT, y_cur + row_h)
        c.setStrokeColorRGB(0, 0, 0)

        c.setFont(PDF_FONT_REG, 7.5)
        c.setFillColorRGB(0, 0, 0)

        # Дата (два рядки)
        date_parts = txn["date"].split("\n")
        c.drawString(COL_DATE, y_cur + 14, date_parts[0])
        c.drawString(COL_DATE, y_cur + 5,  date_parts[1] if len(date_parts) > 1 else "")

        # Опис
        desc = txn["description"]
        if len(desc) > 22:
            desc = desc[:22] + "…"
        c.drawString(COL_DESC, y_cur + 9, desc)

        # MCC
        c.drawString(COL_MCC, y_cur + 9, txn["mcc"])

        # Сума (червона якщо мінус)
        amt = txn["amount"]
        if amt.startswith("-"):
            c.setFillColorRGB(0.8, 0, 0)
        else:
            c.setFillColorRGB(0, 0.5, 0)
        c.drawRightString(COL_AMT + 60, y_cur + 9, amt)
        c.setFillColorRGB(0, 0, 0)

        # Баланс
        c.drawRightString(COL_BAL + 80, y_cur + 9, txn["balance"])

        y_cur -= row_h

    # Оновлюємо суму витрат/надходжень у шапці (білий прямокутник + нові цифри)
    # "Balance at the end of the period" ≈ y=482 rl
    c.setFillColorRGB(1, 1, 1)
    c.rect(28, 478, 200, 12, fill=1, stroke=0)
    c.setFillColorRGB(0, 0, 0)
    c.setFont(PDF_FONT, 8)
    bal_str = f"{final_balance:,.2f}".replace(",", " ") + " UAH"
    c.drawString(180, 480, bal_str)

def _remove_tj_at_ys(pdf: pikepdf.Pdf, page_idx: int, stream_ys: list) -> None:
    """Видаляє перший Tj (значення поля) для кожного Y у content stream."""
    page = pdf.pages[page_idx]
    content = page['/Contents']
    raw = content.read_bytes().decode('latin-1', errors='replace')
    
    modified = raw
    for sy in stream_ys:
        # Матчимо Y як ціле або з десятковим (640.2 або 640.20)
        sy_pattern = str(sy).rstrip('0').rstrip('.')
        pattern = rf'(1 0 0 1 [\d.]+ {re.escape(sy_pattern)}0* Tm\n)(\([^)]*\)Tj)'
        modified = re.sub(pattern, lambda m: m.group(1) + '()Tj', modified, count=1)
    
    content.write(modified.encode('latin-1', errors='replace'))


def edit_pdf(pdf_bytes: bytes, bank: str, fields: dict, add_mock_txn: bool = False) -> bytes:
    cfg = BANK_FIELDS[bank]
    PAGE_H = cfg["page_h"]
    PAGE_W = cfg["page_w"]

    pdf = pikepdf.open(io.BytesIO(pdf_bytes))

    # Monobank: очищаємо content streams
    if cfg["use_stream_clear"] and cfg["personal_stream_indices"]:
        page = pdf.pages[0]
        content_obj = page["/Contents"]
        total_streams = len(content_obj)
        for idx in cfg["personal_stream_indices"]:
            if idx >= total_streams:
                log.warning("Stream %d out of range (total=%d), skip", idx, total_streams)
                continue
            try:
                resolved = pdf.get_object(content_obj[idx].objgen)
                resolved.write(b"", filter=pikepdf.Name("/FlateDecode"))
            except Exception as e:
                log.warning("Stream %d clear failed: %s", idx, e)

    # PrivatBank: видаляємо Tj оператори з персональними даними
    if cfg.get("use_stream_tj_remove") and cfg.get("stream_ys"):
        try:
            _remove_tj_at_ys(pdf, 0, cfg["stream_ys"])
        except Exception as e:
            log.warning("Tj removal failed: %s", e)

    # Чистимо метадані
    try:
        with pdf.open_metadata() as meta:
            meta.clear()
    except Exception: pass
    try:
        if "/Info" in pdf.trailer:
            info_obj = pdf.get_object(pdf.trailer["/Info"].objgen)
            for key in list(info_obj.keys()):
                try: del info_obj[key]
                except: pass
    except Exception: pass

    buf = io.BytesIO()
    pdf.save(buf)
    buf.seek(0)

    # Reportlab overlay з новими даними
    overlay_buf = io.BytesIO()
    c = canvas.Canvas(overlay_buf, pagesize=(PAGE_W, PAGE_H))

    field_positions = cfg["fields"]
    field_rects     = cfg.get("field_rects", {})
    font_size       = cfg["font_size"]

    for key, (x, y) in field_positions.items():
        value = fields.get(key, "")
        if not value:
            continue

        # Білий прямокутник для не-monobank банків
        if not cfg["use_stream_clear"] and key in field_rects:
            w_r, h_r = field_rects[key]
            c.setFillColorRGB(1, 1, 1)
            c.rect(x, y - 2, w_r, h_r + 2, fill=1, stroke=0)

        c.setFillColorRGB(0, 0, 0)
        c.setFont(PDF_FONT_REG, font_size)
        c.drawString(x, y, value)

    # Mock транзакції для Monobank
    if add_mock_txn and bank == "monobank":
        _draw_mock_transactions_monobank(c, PAGE_H, PAGE_W)

    c.save()
    overlay_buf.seek(0)

    base_reader    = PdfReader(buf)
    overlay_reader = PdfReader(overlay_buf)

    writer = PdfWriter()
    for i, pg in enumerate(base_reader.pages):
        if i == 0:
            pg.merge_page(overlay_reader.pages[0])
        writer.add_page(pg)

    writer.add_metadata({
        "/Producer": "", "/Creator": "", "/Author": "",
        "/Title": "", "/Subject": "", "/Keywords": "",
    })

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()

# ── Handlers ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    kb = [
        [InlineKeyboardButton("📁 Завантажити свій PDF", callback_data="mode_upload")],
        [InlineKeyboardButton("🏦 Вибрати шаблон банку", callback_data="mode_template")],
    ]
    await update.message.reply_text(
        "👋 *Редактор банківських виписок*\n\n"
        "Що хочеш зробити?",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )
    return CHOOSE_MODE


async def cb_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
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
        await q.edit_message_text(
            "🏦 Вибери банк:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return CHOOSE_BANK


async def cb_bank(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
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

    ctx.user_data["pdf_bytes"] = tpl_path.read_bytes()
    await q.edit_message_text(
        f"✅ Шаблон {BANK_TEMPLATES[bank]['emoji']} {BANK_TEMPLATES[bank]['name']} вибрано.\n\n"
        + _ask_field_text(0)
    )
    ctx.user_data["field_idx"] = 0
    ctx.user_data["fields"] = {}
    return FIELD_STATES[0]


async def receive_pdf(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❗ Надішли PDF-файл.")
        return WAIT_PDF

    file = await ctx.bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    pdf_bytes = buf.getvalue()

    # Автовизначення банку
    bank = detect_bank(pdf_bytes)
    ctx.user_data["pdf_bytes"] = pdf_bytes
    ctx.user_data["bank"] = bank
    ctx.user_data["fields"] = {}
    ctx.user_data["field_idx"] = 0

    bank_name = BANK_TEMPLATES.get(bank, {}).get("name", "Невідомий") if bank else "❓"
    if bank:
        await update.message.reply_text(
            f"✅ PDF отримано. Визначено банк: *{bank_name}*\n\n"
            + _ask_field_text(0),
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
    q = update.callback_query
    await q.answer()
    bank = q.data.split("_", 1)[1]
    ctx.user_data["bank"] = bank
    ctx.user_data["fields"] = {}
    ctx.user_data["field_idx"] = 0
    await q.edit_message_text(_ask_field_text(0))
    return FIELD_STATES[0]


def detect_bank(pdf_bytes: bytes) -> str | None:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = (reader.pages[0].extract_text() or "").lower()
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
    step = idx + 1
    total = len(FIELD_DEFS)
    return (
        f"[{step}/{total}] {question}\n"
        f"_Приклад: {example}_\n\n"
        f"Надішли /skip щоб пропустити."
    )


async def _handle_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    idx  = ctx.user_data.get("field_idx", 0)
    key  = FIELD_KEYS[idx]

    if text != "/skip":
        ctx.user_data.setdefault("fields", {})[key] = text

    next_idx = idx + 1
    ctx.user_data["field_idx"] = next_idx

    if next_idx >= len(FIELD_DEFS):
        # Питаємо про mock транзакції
        bank = ctx.user_data.get("bank", "")
        if bank == "monobank":
            kb = [
                [InlineKeyboardButton("✅ Так, додати рандомні транзакції", callback_data="mock_yes")],
                [InlineKeyboardButton("❌ Ні, залишити як є", callback_data="mock_no")],
            ]
            await update.message.reply_text(
                "🎲 *Додати рандомні mock-транзакції?*\n"
                "Це змінить суми витрат/надходжень у виписці для унікалізації.",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown",
            )
            return ASK_MOCK_TXN
        else:
            return await _show_confirm(update, ctx)
    else:
        await update.message.reply_text(_ask_field_text(next_idx), parse_mode="Markdown")
        return FIELD_STATES[next_idx]

# Генеруємо handlers для кожного поля
async def ask_name(u, c):       return await _handle_field(u, c)
async def ask_dob(u, c):        return await _handle_field(u, c)
async def ask_tin(u, c):        return await _handle_field(u, c)
async def ask_doc_number(u, c): return await _handle_field(u, c)
async def ask_issued_by(u, c):  return await _handle_field(u, c)
async def ask_issue_date(u, c): return await _handle_field(u, c)
async def ask_address(u, c):    return await _handle_field(u, c)
async def ask_iban(u, c):       return await _handle_field(u, c)


async def cb_mock(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    ctx.user_data["add_mock"] = (q.data == "mock_yes")
    await q.edit_message_reply_markup(reply_markup=None)

    fields = ctx.user_data.get("fields", {})
    bank   = ctx.user_data.get("bank", "?")
    mock   = ctx.user_data.get("add_mock", False)

    bank_name = BANK_TEMPLATES.get(bank, {}).get("name", bank)
    lines = [f"📋 *Перевір дані для {bank_name}:*\n"]
    for key, question, _ in FIELD_DEFS:
        val = fields.get(key) or "_не змінено_"
        lines.append(f"• *{question.rstrip(':')}:* {val}")
    if mock:
        lines.append("\n🎲 _Mock-транзакції: буде додано_")
    lines.append("\n/confirm — застосувати\n/restart — почати спочатку")

    await q.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return CONFIRM


async def _show_confirm(update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    fields = ctx.user_data.get("fields", {})
    bank   = ctx.user_data.get("bank", "?")
    mock   = ctx.user_data.get("add_mock", False)
    
    bank_name = BANK_TEMPLATES.get(bank, {}).get("name", bank)
    lines = [f"📋 *Перевір дані для {bank_name}:*\n"]
    for key, question, _ in FIELD_DEFS:
        val = fields.get(key) or "_не змінено_"
        lines.append(f"• *{question.rstrip(':')}:* {val}")
    if mock:
        lines.append("\n🎲 _Mock-транзакції: буде додано_")
    lines.append("\n/confirm — застосувати\n/restart — почати спочатку")

    try:
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except:
        await update.callback_query.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return CONFIRM


async def cmd_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    pdf_bytes = ctx.user_data.get("pdf_bytes")
    fields    = ctx.user_data.get("fields", {})
    bank      = ctx.user_data.get("bank", "monobank")
    add_mock  = ctx.user_data.get("add_mock", False)

    if not pdf_bytes:
        await update.message.reply_text("❗ PDF не знайдено. /start")
        return ConversationHandler.END

    await update.message.reply_text("⏳ Обробляю файл...")

    try:
        result_bytes = edit_pdf(pdf_bytes, bank, fields, add_mock)
    except Exception as e:
        log.exception("PDF edit failed")
        await update.message.reply_text(f"❌ Помилка:\n{e}")
        return ConversationHandler.END

    bank_name = BANK_TEMPLATES.get(bank, {}).get("name", bank)
    await update.message.reply_document(
        document=io.BytesIO(result_bytes),
        filename=f"edited_{bank}_{datetime.now().strftime('%d%m%Y_%H%M')}.pdf",
        caption=f"✅ Готово! Виписка {bank_name} відредагована.",
    )
    ctx.user_data.clear()
    return ConversationHandler.END


async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text("🔄 Починаємо спочатку.", reply_markup=ReplyKeyboardRemove())
    return await cmd_start(update, ctx)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text("❌ Скасовано.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    field_handlers = {
        ASK_NAME:       [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name),       CommandHandler("skip", ask_name)],
        ASK_DOB:        [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_dob),        CommandHandler("skip", ask_dob)],
        ASK_TIN:        [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_tin),        CommandHandler("skip", ask_tin)],
        ASK_DOC_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_doc_number), CommandHandler("skip", ask_doc_number)],
        ASK_ISSUED_BY:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_issued_by),  CommandHandler("skip", ask_issued_by)],
        ASK_ISSUE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_issue_date), CommandHandler("skip", ask_issue_date)],
        ASK_ADDRESS:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_address),    CommandHandler("skip", ask_address)],
        ASK_IBAN:       [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_iban),       CommandHandler("skip", ask_iban)],
    }

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            MessageHandler(filters.Document.PDF, receive_pdf),
        ],
        states={
            CHOOSE_MODE:  [CallbackQueryHandler(cb_mode, pattern="^mode_")],
            CHOOSE_BANK:  [
                CallbackQueryHandler(cb_bank,        pattern="^bank_"),
                CallbackQueryHandler(cb_detect_bank, pattern="^detect_"),
            ],
            WAIT_PDF:     [MessageHandler(filters.Document.PDF, receive_pdf)],
            ASK_MOCK_TXN: [CallbackQueryHandler(cb_mock, pattern="^mock_")],
            CONFIRM:      [CommandHandler("confirm", cmd_confirm), CommandHandler("restart", cmd_restart)],
            **field_handlers,
        },
        fallbacks=[
            CommandHandler("cancel",  cmd_cancel),
            CommandHandler("restart", cmd_restart),
            CommandHandler("start",   cmd_start),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    log.info("🤖 Бот запущено.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
