"""
Telegram бот для редагування персональних даних у виписках Monobank PDF.

Залежності:
    pip install python-telegram-bot pikepdf reportlab pypdf

Запуск:
    BOT_TOKEN=<твій_токен> python bot.py
    або помісти токен в .env файл
"""

import os
import io
import re
import logging
import tempfile
from pathlib import Path

import pikepdf
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from pypdf import PdfReader, PdfWriter

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# ── Логування ──────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── Конфіг ─────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Шрифт для overlay (LiberationSans поставляється з Linux)
_FONT_PATHS = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",          # macOS
    "C:/Windows/Fonts/arialbd.ttf",           # Windows
]
FONT_PATH = next((p for p in _FONT_PATHS if Path(p).exists()), None)
if FONT_PATH:
    pdfmetrics.registerFont(TTFont("CustomBold", FONT_PATH))
    PDF_FONT = "CustomBold"
else:
    PDF_FONT = "Helvetica-Bold"   # fallback (вбудований у reportlab)

PDF_FONT_SIZE = 9

# ── Стани розмови ──────────────────────────────────────────────────────────
(
    WAIT_PDF,
    ASK_NAME,
    ASK_DOB,
    ASK_TIN,
    ASK_DOC_NUMBER,
    ASK_ISSUED_BY,
    ASK_ISSUE_DATE,
    ASK_ADDRESS,
    ASK_IBAN,
    CONFIRM,
) = range(10)

# Ключ для зберігання даних у context.user_data
UD_PDF   = "pdf_bytes"
UD_FIELDS = "fields"

# ── Поля ───────────────────────────────────────────────────────────────────
FIELD_DEFS = [
    # (ключ,              питання,                              приклад)
    ("name",        "Введіть нове ПІБ (латиницею):",        "KOVALENKO PETRO"),
    ("dob",         "Дата народження (ДД.ММ.РРРР):",        "15.03.1990"),
    ("tin",         "ІПН (10 цифр):",                        "1234567890"),
    ("doc_number",  "Серія/номер документа:",                 "987654"),
    ("issued_by",   "Ким виданий (код):",                     "1234"),
    ("issue_date",  "Дата видачі (ДД.ММ.РРРР):",             "01.01.2020"),
    ("address",     "Адреса реєстрації:",
                    "Ukraine, city Lviv, street Franka, build 5, 79000"),
    ("iban",        "IBAN:",                                  "UA123456789012345678901234567"),
]

STATE_ORDER = [ASK_NAME, ASK_DOB, ASK_TIN, ASK_DOC_NUMBER,
               ASK_ISSUED_BY, ASK_ISSUE_DATE, ASK_ADDRESS, ASK_IBAN]

# ── PDF editing ─────────────────────────────────────────────────────────────
PAGE_H = 839.055
PAGE_W = 595.275

# Позиції для overlay (x, y в системі координат reportlab — від лівого нижнього кута)
# Визначені аналізом content streams оригінального PDF
FIELD_POSITIONS = {
    "name":        (59,  718),
    "dob":         (87,  705),
    "tin":         (49,  691),
    "doc_number":  (221, 663),
    "issued_by":   (76,  650),
    "issue_date":  (87,  636),
    "address":     (124, 623),
    "iban":        (56,  582),
}

# Індекси content-streams першої сторінки, що містять персональні дані
PERSONAL_STREAM_INDICES = list(range(10, 19))


def edit_pdf(pdf_bytes: bytes, fields: dict) -> bytes:
    """
    Приймає оригінальний PDF як bytes і словник нових значень.
    Повертає відредагований PDF без оригінальних персональних даних.
    """
    # ── 1. pikepdf: очищаємо оригінальні персональні streams ──────────────
    pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    page = pdf.pages[0]
    content_obj = page["/Contents"]

    for idx in PERSONAL_STREAM_INDICES:
        try:
            item = content_obj[idx]
            resolved = pdf.get_object(item.objgen)
            # Замінюємо на порожній stream
            resolved.write(b"", filter=pikepdf.Name("/FlateDecode"))
        except (IndexError, Exception) as e:
            log.warning("Не вдалося очистити stream %d: %s", idx, e)

    # ── 2. Чистимо XMP/DocInfo метадані ───────────────────────────────────
    try:
        with pdf.open_metadata() as meta:
            meta.clear()
    except Exception as e:
        log.warning("XMP metadata clear failed: %s", e)

    try:
        if "/Info" in pdf.trailer:
            info_obj = pdf.get_object(pdf.trailer["/Info"].objgen)
            for key in list(info_obj.keys()):
                try:
                    del info_obj[key]
                except Exception:
                    pass
    except Exception as e:
        log.warning("DocInfo clear failed: %s", e)

    buf = io.BytesIO()
    pdf.save(buf)
    buf.seek(0)

    # ── 3. reportlab: overlay з новими значеннями ─────────────────────────
    overlay_buf = io.BytesIO()
    c = canvas.Canvas(overlay_buf, pagesize=(PAGE_W, PAGE_H))
    c.setFont(PDF_FONT, PDF_FONT_SIZE)
    c.setFillColorRGB(0, 0, 0)

    for key, (x, y) in FIELD_POSITIONS.items():
        value = fields.get(key, "")
        if value:
            c.drawString(x, y, value)

    c.save()
    overlay_buf.seek(0)

    # ── 4. pypdf: merge overlay поверх очищеного PDF ──────────────────────
    base_reader    = PdfReader(buf)
    overlay_reader = PdfReader(overlay_buf)

    writer = PdfWriter()
    for i, pg in enumerate(base_reader.pages):
        if i == 0:
            pg.merge_page(overlay_reader.pages[0])
        writer.add_page(pg)

    # Очищаємо metadata ще раз через pypdf
    writer.add_metadata({
        "/Producer": "",
        "/Creator":  "",
        "/Author":   "",
        "/Title":    "",
        "/Subject":  "",
        "/Keywords": "",
    })

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ── Handlers ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "👋 Привіт! Надішли мені PDF виписку Monobank, і я допоможу замінити "
        "персональні дані у файлі.\n\n"
        "Відправ PDF-файл ⬇️"
    )
    return WAIT_PDF


async def receive_pdf(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❗ Надішли, будь ласка, PDF-файл.")
        return WAIT_PDF

    file = await ctx.bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    ctx.user_data[UD_PDF]    = buf.getvalue()
    ctx.user_data[UD_FIELDS] = {}

    await update.message.reply_text(
        "✅ PDF отримано. Зараз заповнимо нові дані.\n"
        "На кожному кроці можна надіслати /skip, щоб залишити поле порожнім.\n\n"
        + FIELD_DEFS[0][1] + f"\n_Приклад: {FIELD_DEFS[0][2]}_",
        parse_mode="Markdown",
    )
    return ASK_NAME


def _ask_next(field_idx: int):
    """Повертає текст питання для поля з індексом field_idx."""
    key, question, example = FIELD_DEFS[field_idx]
    return f"{question}\n_Приклад: {example}_"


async def _save_and_next(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    field_key: str,
    next_state: int,
    next_field_idx: int,
) -> int:
    text = update.message.text.strip()
    if text != "/skip":
        ctx.user_data[UD_FIELDS][field_key] = text

    if next_state == CONFIRM:
        return await show_confirm(update, ctx)

    await update.message.reply_text(
        _ask_next(next_field_idx), parse_mode="Markdown"
    )
    return next_state


# Генеруємо handlers для кожного поля
async def ask_name(u, c):       return await _save_and_next(u, c, "name",        ASK_DOB,        1)
async def ask_dob(u, c):        return await _save_and_next(u, c, "dob",         ASK_TIN,        2)
async def ask_tin(u, c):        return await _save_and_next(u, c, "tin",         ASK_DOC_NUMBER, 3)
async def ask_doc_number(u, c): return await _save_and_next(u, c, "doc_number",  ASK_ISSUED_BY,  4)
async def ask_issued_by(u, c):  return await _save_and_next(u, c, "issued_by",   ASK_ISSUE_DATE, 5)
async def ask_issue_date(u, c): return await _save_and_next(u, c, "issue_date",  ASK_ADDRESS,    6)
async def ask_address(u, c):    return await _save_and_next(u, c, "address",     ASK_IBAN,       7)
async def ask_iban(u, c):       return await _save_and_next(u, c, "iban",        CONFIRM,        -1)


async def show_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    fields = ctx.user_data.get(UD_FIELDS, {})
    lines  = ["📋 *Перевір нові дані:*\n"]
    for key, question, _ in FIELD_DEFS:
        val = fields.get(key) or "_(не змінено)_"
        label = question.rstrip(":")
        lines.append(f"• *{label}:* {val}")
    lines.append("\nВсе вірно? /confirm або /restart")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return CONFIRM


async def cmd_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    pdf_bytes = ctx.user_data.get(UD_PDF)
    fields    = ctx.user_data.get(UD_FIELDS, {})

    if not pdf_bytes:
        await update.message.reply_text("❗ PDF не знайдено. Починай спочатку /start")
        return ConversationHandler.END

    await update.message.reply_text("⏳ Обробляю файл...")

    try:
        result_bytes = edit_pdf(pdf_bytes, fields)
    except Exception as e:
        log.exception("PDF edit failed")
        await update.message.reply_text(f"❌ Помилка при обробці PDF:\n{e}")
        return ConversationHandler.END

    await update.message.reply_document(
        document=io.BytesIO(result_bytes),
        filename="edited_statement.pdf",
        caption="✅ Готово! Персональні дані замінено, метадані очищено.",
    )
    ctx.user_data.clear()
    return ConversationHandler.END


async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text(
        "🔄 Починаємо спочатку. Надішли PDF-файл.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return WAIT_PDF


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text(
        "❌ Скасовано.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start),
                      MessageHandler(filters.Document.PDF, receive_pdf)],
        states={
            WAIT_PDF:       [MessageHandler(filters.Document.PDF, receive_pdf)],
            ASK_NAME:       [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name),
                             CommandHandler("skip", ask_name)],
            ASK_DOB:        [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_dob),
                             CommandHandler("skip", ask_dob)],
            ASK_TIN:        [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_tin),
                             CommandHandler("skip", ask_tin)],
            ASK_DOC_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_doc_number),
                             CommandHandler("skip", ask_doc_number)],
            ASK_ISSUED_BY:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_issued_by),
                             CommandHandler("skip", ask_issued_by)],
            ASK_ISSUE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_issue_date),
                             CommandHandler("skip", ask_issue_date)],
            ASK_ADDRESS:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_address),
                             CommandHandler("skip", ask_address)],
            ASK_IBAN:       [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_iban),
                             CommandHandler("skip", ask_iban)],
            CONFIRM:        [CommandHandler("confirm", cmd_confirm),
                             CommandHandler("restart", cmd_restart)],
        },
        fallbacks=[
            CommandHandler("cancel",  cmd_cancel),
            CommandHandler("restart", cmd_restart),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)

    log.info("Бот запущено. Ctrl+C для зупинки.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
