"""Telegram bot that compresses PDFs and sends them back.

Send (or forward) a PDF to the bot, pick a compression level, and it replies
with a smaller version. Compression is done by PyMuPDF (see compress.py).
"""

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

# Read settings (TELEGRAM_BOT_TOKEN etc.) from a .env file next to this script.
# Real environment variables still win if both are set.
load_dotenv(Path(__file__).with_name(".env"))

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("pdf-shrinker")

MB = 1024 * 1024

# Optional: URL of a self-hosted Telegram Bot API server, e.g. http://localhost:8081
# Needed for PDFs over 20 MB (the public Bot API refuses to hand those to bots).
LOCAL_API_URL = os.getenv("TELEGRAM_API_URL", "").rstrip("/")
MAX_DOWNLOAD = 2000 * MB if LOCAL_API_URL else 20 * MB
MAX_UPLOAD = 2000 * MB if LOCAL_API_URL else 50 * MB

# Only these Telegram user IDs may use the bot. Empty = anyone.
ALLOWED_USER_IDS = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()
}

# How many compressions may run at once (compression is CPU/RAM-heavy).
MAX_PARALLEL_JOBS = int(os.getenv("MAX_PARALLEL_JOBS", "2"))
JOB_TIMEOUT_SECONDS = int(os.getenv("JOB_TIMEOUT_SECONDS", "900"))
_jobs = asyncio.Semaphore(MAX_PARALLEL_JOBS)

# level key (matches LEVELS in compress.py) -> button text
PRESETS = {
    "light": "Light (best quality)",
    "medium": "Medium (recommended)",
    "strong": "Strong (smallest)",
}
COMPRESS_SCRIPT = Path(__file__).with_name("compress.py")
from compress import (  # noqa: E402
    TIMEOUT_EXIT_CODE,
    choose_engine,
    find_ghostscript,
    pymupdf_available,
)


def human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=f"c:{key}")] for key, text in PRESETS.items()]
    )


def allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


async def compress_pdf(src: Path, dst: Path, level: str) -> None:
    # Separate process so a stuck or huge file can be killed on timeout.
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(COMPRESS_SCRIPT), str(src), str(dst), level,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), JOB_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode == TIMEOUT_EXIT_CODE:
        raise asyncio.TimeoutError
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError(err.decode(errors="replace")[-500:])
    log.info("Compressed (%s) %s", level, out.decode(errors="replace").strip())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    limit = "up to 2 GB" if LOCAL_API_URL else "up to 20 MB"
    await update.effective_message.reply_text(
        "Send me a PDF (or forward one from any chat) and I'll shrink it for you.\n\n"
        f"I accept PDFs {limit}. Forwarding is free on your data: the file goes "
        "straight to me on Telegram's servers, so you only download the small version."
    )


async def on_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    msg = update.effective_message
    doc = msg.document
    is_pdf = doc.mime_type == "application/pdf" or (doc.file_name or "").lower().endswith(".pdf")
    if not is_pdf:
        await msg.reply_text("I only compress PDF files for now.")
        return
    if doc.file_size and doc.file_size > MAX_DOWNLOAD:
        await msg.reply_text(
            f"That file is {human(doc.file_size)}, but I can only fetch files up to "
            f"{human(MAX_DOWNLOAD)} (a Telegram Bot API limit)."
        )
        return
    await msg.reply_text(
        f"Got it ({human(doc.file_size or 0)}). How much should I compress it?",
        reply_markup=choice_keyboard(),
        do_quote=True,
    )


async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not allowed(update):
        await query.answer()
        return
    await query.answer()

    key = (query.data or "").split(":", 1)[-1]
    if key not in PRESETS:
        return
    label = PRESETS[key]

    original = getattr(query.message, "reply_to_message", None)
    if original is None or original.document is None:
        await query.edit_message_text("I lost track of that file. Please send the PDF again.")
        return
    doc = original.document

    await query.edit_message_text(f"Compressing with {label.split(' (')[0]}... big files can take a few minutes.")
    chat_id = original.chat_id

    try:
        async with _jobs:
            with tempfile.TemporaryDirectory() as tmp:
                src, dst = Path(tmp) / "in.pdf", Path(tmp) / "out.pdf"
                tg_file = await doc.get_file()
                await tg_file.download_to_drive(src)

                before = src.stat().st_size
                await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
                await compress_pdf(src, dst, key)
                after = dst.stat().st_size

                if after >= before * 0.97:
                    hint = "" if key == "strong" else " Try a stronger level."
                    await query.edit_message_text(
                        f"I couldn't make it meaningfully smaller ({human(before)} -> {human(after)}); "
                        f"it's probably already optimized.{hint}",
                        reply_markup=None if key == "strong" else choice_keyboard(),
                    )
                    return
                if after > MAX_UPLOAD:
                    await query.edit_message_text(
                        f"Compressed to {human(after)}, but I can only send files up to "
                        f"{human(MAX_UPLOAD)}. Try a stronger level.",
                        reply_markup=choice_keyboard(),
                    )
                    return

                saved = round((1 - after / before) * 100)
                name = f"{Path(doc.file_name or 'document').stem}_compressed.pdf"
                with dst.open("rb") as fh:
                    await original.reply_document(
                        document=fh,
                        filename=name,
                        caption=f"{human(before)} -> {human(after)} ({saved}% smaller)",
                    )
        await query.edit_message_text("Done!")
    except asyncio.TimeoutError:
        await query.edit_message_text("That took too long and I had to stop. Try a smaller file.")
    except Exception as exc:  # noqa: BLE001
        log.exception("compression failed")
        await query.edit_message_text(
            "I couldn't process that PDF. It may be password-protected or damaged.\n"
            f"Details: {str(exc)[:200]}"
        )


def main() -> None:
    if not COMPRESS_SCRIPT.exists():
        raise SystemExit("compress.py must be in the same folder as bot.py.")
    try:
        engine = choose_engine()
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    if engine == "pymupdf" and not pymupdf_available():
        raise SystemExit(
            "No compression engine found. Install Ghostscript, or run: pip install -r requirements.txt"
        )
    log.info("Compression engine: %s (%s)", engine, find_ghostscript() or "PyMuPDF")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN (get one from @BotFather).")

    builder = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(True)  # don't block other users while one PDF compresses
        .connect_timeout(30)
        .read_timeout(600)
        .write_timeout(600)
    )
    if LOCAL_API_URL:
        builder = (
            builder.base_url(f"{LOCAL_API_URL}/bot")
            .base_file_url(f"{LOCAL_API_URL}/file/bot")
            .local_mode(True)
        )
    app = builder.build()

    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(MessageHandler(filters.Document.ALL, on_pdf))
    app.add_handler(CallbackQueryHandler(on_choice, pattern=r"^c:"))

    log.info("Bot started (local API: %s)", LOCAL_API_URL or "no")
    app.run_polling()


if __name__ == "__main__":
    main()