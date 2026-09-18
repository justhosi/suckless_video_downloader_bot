import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import yt_dlp
from telegram import Update, InputMediaPhoto
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from ptbcontrib.aiohttp_request import AiohttpRequest

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
COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "/opt/videobot/cookies.txt"))
AUDIO_FORMAT = os.environ.get("AUDIO_FORMAT", "mp3")
AUDIO_BITRATE = os.environ.get("AUDIO_BITRATE", "192")
GALLERY_DL_TIMEOUT = int(os.environ.get("GALLERY_DL_TIMEOUT", "300"))

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
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
            for domain in ("youtube.com", "youtu.be", "instagram.com", "tiktok.com", "twitter.com", "x.com")
        ):
            return candidate
    return None


async def safe_call(coro_func, *args, retries=2, delay=1, **kwargs):
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
    if not update.message:
        return
    await safe_call(
        update.message.reply_text,
        "Send me a YouTube, Instagram, or TikTok link and I'll send it back.\n\n"
        "Commands:\n"
        "/audio <url> — extract only the audio (MP3)\n"
        "/status — admin-only bot state",
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else None
    if not ADMIN_USER_ID or str(user_id) != ADMIN_USER_ID:
        return
    active_jobs = min(pending_jobs, MAX_CONCURRENT_DOWNLOADS)
    cookies_state = "present" if COOKIES_FILE.is_file() else "missing"
    await safe_call(
        update.message.reply_text,
        f"Running. Jobs: {pending_jobs} pending, {active_jobs} active. "
        f"Capacity: {MAX_CONCURRENT_DOWNLOADS} active, {MAX_QUEUED_JOBS} queued. "
        f"Cookies: {cookies_state}.",
    )


def _base_ydl_opts(directory: Path) -> dict:
    opts = {
        "outtmpl": str(directory / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": MAX_FILESIZE_MB * 1024 * 1024,
        "socket_timeout": 30,
        "retries": 3,
        "cachedir": False,
    }
    if COOKIES_FILE.is_file():
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts


def _gallery_dl_bin() -> str:
    """Return the gallery-dl executable, preferring the one beside this Python."""
    candidate = Path(sys.executable).with_name("gallery-dl")
    if candidate.is_file():
        return str(candidate)
    return shutil.which("gallery-dl") or "gallery-dl"


def download_images_with_gallery_dl(url: str, directory: Path) -> list[Path]:
    """Download the images of an image-only post with gallery-dl.

    yt-dlp raises "No video formats found" for posts such as Instagram
    carousels that contain only images, so gallery-dl is used as a fallback.
    """
    cmd = [_gallery_dl_bin(), "--directory", str(directory), "--quiet", "--no-input"]
    if COOKIES_FILE.is_file():
        cmd += ["--cookies", str(COOKIES_FILE)]
    cmd.append(url)
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=GALLERY_DL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("gallery-dl could not run (%s): %s", type(e).__name__, e)
        return []
    if proc.returncode != 0:
        logger.info(
            "gallery-dl exited rc=%s host=%s stderr=%s",
            proc.returncode,
            urlsplit(url).hostname,
            (proc.stderr or "").strip()[-500:],
        )
    return sorted(
        p
        for p in directory.rglob("*")
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in IMAGE_EXTS
    )


def download_media(url: str, directory: Path, audio_only: bool = False) -> tuple[list[Path], str, str]:
    opts = _base_ydl_opts(directory)
    if audio_only:
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": AUDIO_FORMAT,
                "preferredquality": AUDIO_BITRATE,
            }],
        })
    else:
        opts["format"] = "mp4/best[ext=mp4]/best"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            entries = info.get("entries") if info.get("_type") == "playlist" else None
            if entries:
                paths = []
                for entry in entries:
                    if not entry:
                        continue
                    p = Path(ydl.prepare_filename(entry))
                    if audio_only:
                        p = p.with_suffix(f".{AUDIO_FORMAT}")
                    if p.exists():
                        paths.append(p)
                title = info.get("title") or (entries[0].get("title", "") if entries else "")
            else:
                p = Path(ydl.prepare_filename(info))
                if audio_only:
                    p = p.with_suffix(f".{AUDIO_FORMAT}")
                paths = [p] if p.exists() else []
                title = info.get("title", "")
    except yt_dlp.utils.ExtractorError as e:
        if "no video formats found" not in str(e).lower():
            raise
        logger.info(
            "yt-dlp found no video formats host=%s; retrying with gallery-dl",
            urlsplit(url).hostname,
        )
        paths = download_images_with_gallery_dl(url, directory)
        if not paths:
            raise yt_dlp.utils.DownloadError("No video formats or images found") from e
        return paths, "", "image"

    if not paths:
        raise yt_dlp.utils.DownloadError("No downloadable media found")

    if audio_only:
        kind = "audio"
    else:
        kind = "image" if paths[0].suffix.lower() in IMAGE_EXTS else "video"

    return paths, title, kind


async def _run_job(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, audio_only: bool):
    global pending_jobs
    message = update.message
    user_id = update.effective_user.id if update.effective_user else 0
    now = time.monotonic()
    elapsed = now - user_last_job.get(user_id, float("-inf"))
    if elapsed < USER_COOLDOWN_SECONDS:
        await safe_call(message.reply_text, f"Please wait {int(USER_COOLDOWN_SECONDS - elapsed) + 1}s before requesting another file.")
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
                    paths, title, kind = await asyncio.to_thread(download_media, url, Path(tmpdir), audio_only)
                except yt_dlp.utils.DownloadError as e:
                    err_text = str(e).lower()
                    if "age" in err_text or "login" in err_text or "not available to everyone" in err_text:
                        user_msg = "That content is age-restricted or login-gated. I can't download it."
                    elif "private" in err_text:
                        user_msg = "That content is private, so I can't download it."
                    else:
                        user_msg = "I couldn't download that. It may be private or unsupported."
                    logger.info("Download failed user=%s host=%s err=%s", user_id, urlsplit(url).hostname, e)
                    await safe_call(status_msg.edit_text, user_msg)
                    return
                except Exception:
                    logger.exception("Unexpected download error user=%s host=%s", user_id, urlsplit(url).hostname)
                    await safe_call(status_msg.edit_text, "Something went wrong while downloading that.")
                    return

                total_mb = sum(p.stat().st_size for p in paths) // (1024 * 1024)
                if total_mb > MAX_FILESIZE_MB:
                    await safe_call(status_msg.edit_text, f"File is {total_mb}MB, over the {MAX_FILESIZE_MB}MB limit.")
                    return

                await safe_call(status_msg.edit_text, "Uploading…")
                try:
                    caption = title[:1024] if title else None
                    if kind == "audio":
                        with paths[0].open("rb") as f:
                            await safe_call(message.reply_audio, audio=f, caption=caption)
                    elif kind == "image":
                        if len(paths) == 1:
                            with paths[0].open("rb") as f:
                                await safe_call(message.reply_photo, photo=f, caption=caption)
                        else:
                            handles = [p.open("rb") for p in paths[:10]]
                            try:
                                media = [InputMediaPhoto(h) for h in handles]
                                await safe_call(message.reply_media_group, media=media)
                            finally:
                                for h in handles:
                                    h.close()
                    else:
                        with paths[0].open("rb") as f:
                            await safe_call(message.reply_video, video=f, caption=caption)
                    await safe_call(status_msg.delete)
                    logger.info("Completed user=%s host=%s kind=%s size_mb=%s", user_id, urlsplit(url).hostname, kind, total_mb)
                except Exception:
                    logger.exception("Upload failed user=%s", user_id)
                    await safe_call(status_msg.edit_text, "I downloaded it, but couldn't send it back.")
    finally:
        pending_jobs -= 1


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    url = extract_supported_url(message.text or "")
    if not url:
        await safe_call(message.reply_text, "Send me a valid YouTube, Instagram, or TikTok link.")
        return
    await _run_job(update, context, url, audio_only=False)


async def audio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    text = " ".join(context.args or [])
    url = extract_supported_url(text)
    if not url:
        await safe_call(message.reply_text, "Usage: /audio <link>")
        return
    await _run_job(update, context, url, audio_only=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update %s caused error: %s", update, context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set")

    # httpx timeouts are per-operation, but the aiohttp ClientTimeout is a total
    # budget per request, so these values are set on the request objects instead
    # of on the builder (the builder rejects a request instance plus its timeout
    # knobs at the same time).
    request = AiohttpRequest(
        connection_pool_size=256,
        client_timeout=aiohttp.ClientTimeout(
            total=45, connect=8, sock_connect=8, sock_read=30
        ),
        # An upload is a single long request, so give it a generous total budget
        # (httpx media_write_timeout was per write, not per upload).
        media_total_timeout=600,
    )
    # getUpdates only ever needs one long-lived connection.
    get_updates_request = AiohttpRequest(
        client_timeout=aiohttp.ClientTimeout(
            total=60, connect=8, sock_connect=8, sock_read=45
        )
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
    app.add_handler(CommandHandler("audio", audio_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    logger.info("Bot starting…")
    app.run_polling()


if __name__ == "__main__":
    main()
