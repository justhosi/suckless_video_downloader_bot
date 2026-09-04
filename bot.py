import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path

import yt_dlp
from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
MAX_FILESIZE_MB = int(os.environ.get("MAX_FILESIZE_MB", "50"))  # Telegram bot API hard limit
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/opt/videobot/downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

URL_PATTERN = re.compile(
    r"(https?://)?(www\.)?(youtube\.com|youtu\.be|instagram\.com|tiktok\.com|vm\.tiktok\.com)/\S+",
    re.IGNORECASE,
)


async def safe_call(coro_func, *args, retries=3, delay=3, **kwargs):
    """Call a Telegram API coroutine, retrying on transient network errors."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            return await coro_func(*args, **kwargs)
        except (TimedOut, NetworkError) as e:
            last_err = e
            if attempt < retries:
                logger.warning("Network hiccup (attempt %d/%d): %s", attempt + 1, retries, e)
                await asyncio.sleep(delay)
    raise last_err


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_call(
        update.message.reply_text,
        "Send me a YouTube, Instagram, or TikTok link and I'll send the video back.",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    match = URL_PATTERN.search(text)
    if not match:
        await safe_call(update.message.reply_text, "Send me a valid YouTube, Instagram, or TikTok link.")
        return

    url = match.group(0)
    status_msg = await safe_call(update.message.reply_text, "Downloading…")

    with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR) as tmpdir:
        outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")
        ydl_opts = {
            "format": "mp4/best[ext=mp4]/best",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "max_filesize": MAX_FILESIZE_MB * 1024 * 1024,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = ydl.prepare_filename(info)
        except yt_dlp.utils.DownloadError as e:
            await safe_call(status_msg.edit_text, f"Couldn't download that video: {e}")
            return
        except Exception:
            logger.exception("Unexpected error downloading %s", url)
            await safe_call(status_msg.edit_text, "Something went wrong downloading that video.")
            return

        if not os.path.exists(filepath):
            await safe_call(
                status_msg.edit_text,
                f"Download failed, or the video is over the {MAX_FILESIZE_MB}MB limit.",
            )
            return

        filesize_mb = os.path.getsize(filepath) // (1024 * 1024)
        if filesize_mb > MAX_FILESIZE_MB:
            await safe_call(
                status_msg.edit_text,
                f"Video is {filesize_mb}MB, over the {MAX_FILESIZE_MB}MB Telegram bot limit.",
            )
            return

        await safe_call(status_msg.edit_text, "Uploading…")
        try:
            with open(filepath, "rb") as f:
                await safe_call(
                    update.message.reply_video, video=f, caption=info.get("title", "")[:1024]
                )
            await safe_call(status_msg.delete)
        except Exception:
            logger.exception("Error sending video for %s", url)
            await safe_call(status_msg.edit_text, "Downloaded it, but couldn't send it back.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update %s caused error: %s", update, context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    # Longer timeouts so brief network hiccups don't kill requests outright
    request = HTTPXRequest(connect_timeout=20, read_timeout=20, write_timeout=20, pool_timeout=20)
    get_updates_request = HTTPXRequest(
        connect_timeout=20, read_timeout=40, write_timeout=20, pool_timeout=20
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(get_updates_request)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    logger.info("Bot starting…")
    app.run_polling()


if __name__ == "__main__":
    main()
