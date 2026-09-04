import logging
import os
import re
import tempfile
from pathlib import Path

import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a YouTube, Instagram, or TikTok link and I'll send the video back."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    match = URL_PATTERN.search(text)
    if not match:
        await update.message.reply_text("Send me a valid YouTube, Instagram, or TikTok link.")
        return

    url = match.group(0)
    status_msg = await update.message.reply_text("Downloading…")

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
            await status_msg.edit_text(f"Couldn't download that video: {e}")
            return
        except Exception:
            logger.exception("Unexpected error downloading %s", url)
            await status_msg.edit_text("Something went wrong downloading that video.")
            return

        if not os.path.exists(filepath):
            await status_msg.edit_text(
                f"Download failed, or the video is over the {MAX_FILESIZE_MB}MB limit."
            )
            return

        filesize_mb = os.path.getsize(filepath) // (1024 * 1024)
        if filesize_mb > MAX_FILESIZE_MB:
            await status_msg.edit_text(
                f"Video is {filesize_mb}MB, over the {MAX_FILESIZE_MB}MB Telegram bot limit."
            )
            return

        await status_msg.edit_text("Uploading…")
        try:
            with open(filepath, "rb") as f:
                await update.message.reply_video(video=f, caption=info.get("title", "")[:1024])
            await status_msg.delete()
        except Exception:
            logger.exception("Error sending video for %s", url)
            await status_msg.edit_text("Downloaded it, but couldn't send it back.")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot starting…")
    app.run_polling()


if __name__ == "__main__":
    main()
