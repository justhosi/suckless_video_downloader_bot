import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import yt_dlp
from telegram import Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
# HTTPX includes the complete request URL in INFO logs. Telegram's API token is
# part of that URL, so keep transport logging out of the journal.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
MAX_FILESIZE_MB = int(os.environ.get("MAX_FILESIZE_MB", "50"))
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/opt/videobot/downloads"))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))
MAX_QUEUED_JOBS = int(os.environ.get("MAX_QUEUED_JOBS", "4"))
USER_COOLDOWN_SECONDS = int(os.environ.get("USER_COOLDOWN_SECONDS", "30"))
ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

download_slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
pending_jobs = 0
user_last_job: dict[int, float] = {}


def extract_supported_url(text: str) -> str | None:
    """Return the first supported URL in text, normalized with an HTTPS scheme."""
    for word in text.split():
        url = word.strip("<>[](){}.,!?\"'")
        candidate = url if "://" in url else f"https://{url}"
        parts = urlsplit(candidate)
        hostname = (parts.hostname or "").lower()
        if parts.scheme not in {"http", "https"} or not parts.path:
            continue
        if any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in ("youtube.com", "youtu.be", "instagram.com", "tiktok.com")
        ):
            return candidate
    return None


async def safe_call(coro_func, *args, retries=6, delay=3, **kwargs):
    """Call a Telegram API coroutine, retrying on transient network errors."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            return await coro_func(*args, **kwargs)
        except (TimedOut, NetworkError) as e:
            if isinstance(e, BadRequest):
                raise
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


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if not ADMIN_USER_ID or str(user_id) != ADMIN_USER_ID:
        return
    active_jobs = min(pending_jobs, MAX_CONCURRENT_DOWNLOADS)
    await safe_call(
        update.message.reply_text,
        f"Running. Jobs: {pending_jobs} pending, {active_jobs} active. "
        f"Capacity: {MAX_CONCURRENT_DOWNLOADS} active, {MAX_QUEUED_JOBS} queued.",
    )


def download_video(url: str, directory: Path) -> tuple[Path, str]:
    """Blocking yt-dlp work, called in a worker thread by the handler."""
    ydl_opts = {
        "format": "mp4/best[ext=mp4]/best",
        "outtmpl": str(directory / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": MAX_FILESIZE_MB * 1024 * 1024,
        "socket_timeout": 30,
        "retries": 3,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info)), info.get("title", "")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global pending_jobs

    message = update.message
    if not message:
        return
    url = extract_supported_url(message.text or "")
    if not url:
        await safe_call(message.reply_text, "Send me a valid YouTube, Instagram, or TikTok link.")
        return

    user_id = update.effective_user.id if update.effective_user else 0
    now = time.monotonic()
    elapsed = now - user_last_job.get(user_id, float("-inf"))
    if elapsed < USER_COOLDOWN_SECONDS:
        await safe_call(message.reply_text, f"Please wait {int(USER_COOLDOWN_SECONDS - elapsed) + 1}s before requesting another video.")
        return
    if pending_jobs >= MAX_CONCURRENT_DOWNLOADS + MAX_QUEUED_JOBS:
        await safe_call(message.reply_text, "I'm busy right now. Please try again in a few minutes.")
        return

    user_last_job[user_id] = now
    pending_jobs += 1
    status_msg = None
    try:
        was_queued = pending_jobs > MAX_CONCURRENT_DOWNLOADS
        status_msg = await safe_call(message.reply_text, "Queued…" if was_queued else "Downloading…")
        async with download_slots:
            if was_queued:
                await safe_call(status_msg.edit_text, "Downloading…")
            with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR) as tmpdir:
                try:
                    filepath, title = await asyncio.to_thread(download_video, url, Path(tmpdir))
                except yt_dlp.utils.DownloadError:
                    logger.info("Download failed for user=%s host=%s", user_id, urlsplit(url).hostname)
                    await safe_call(status_msg.edit_text, "I couldn't download that video. It may be private or unsupported.")
                    return
                except Exception:
                    logger.exception("Unexpected download error for user=%s host=%s", user_id, urlsplit(url).hostname)
                    await safe_call(status_msg.edit_text, "Something went wrong while downloading that video.")
                    return

                if not filepath.exists():
                    await safe_call(status_msg.edit_text, f"Download failed, or the video is over the {MAX_FILESIZE_MB}MB limit.")
                    return

                filesize_mb = filepath.stat().st_size // (1024 * 1024)
                if filesize_mb > MAX_FILESIZE_MB:
                    await safe_call(status_msg.edit_text, f"Video is {filesize_mb}MB, over the {MAX_FILESIZE_MB}MB limit.")
                    return

                await safe_call(status_msg.edit_text, "Uploading…")
                try:
                    with filepath.open("rb") as video:
                        await safe_call(message.reply_video, video=video, caption=title[:1024])
                    await safe_call(status_msg.delete)
                    logger.info("Completed job user=%s host=%s size_mb=%s", user_id, urlsplit(url).hostname, filesize_mb)
                except Exception:
                    logger.exception("Upload failed for user=%s", user_id)
                    await safe_call(status_msg.edit_text, "I downloaded it, but couldn't send it back.")
    finally:
        pending_jobs -= 1


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update %s caused error: %s", update, context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    request = HTTPXRequest(connect_timeout=8, read_timeout=30, write_timeout=30, pool_timeout=8)
    get_updates_request = HTTPXRequest(
        connect_timeout=8, read_timeout=45, write_timeout=30, pool_timeout=8
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(get_updates_request)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    logger.info("Bot starting…")
    app.run_polling()


if __name__ == "__main__":
    main()
