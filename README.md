# videobot

Telegram bot that downloads YouTube, Instagram, and TikTok videos from a
link and sends them back in chat. Built with `python-telegram-bot` and
`yt-dlp`.

## Setup (Ubuntu 24.04 VPS)

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git ffmpeg

sudo useradd -r -m -d /opt/videobot -s /usr/sbin/nologin videobot

# clone directly into /opt/videobot, then:
sudo git clone https://github.com/justhosi/suckless_video_downloader_bot.git /opt/videobot
sudo mkdir -p /opt/videobot/downloads
sudo chown -R videobot:videobot /opt/videobot
cd /opt/videobot
sudo -u videobot python3 -m venv venv
sudo -u videobot ./venv/bin/pip install -r requirements.txt

sudo cp .env.example .env
sudo nano .env   # set BOT_TOKEN=your_token_from_botfather
sudo chmod 600 .env
sudo chown videobot:videobot .env

sudo cp videobot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now videobot
sudo systemctl status videobot
```

## Config

Copy `.env.example` to `.env` and fill in:

- `BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
- `MAX_FILESIZE_MB` — default 50 (Telegram bot API hard limit)
- `DOWNLOAD_DIR` — default `/opt/videobot/downloads`
- `MAX_CONCURRENT_DOWNLOADS` — default 2. Limits simultaneous downloads.
- `MAX_QUEUED_JOBS` — default 4. Rejects excess requests instead of queuing forever.
- `USER_COOLDOWN_SECONDS` — default 30. Minimum time between jobs from one user.
- `ADMIN_USER_ID` — optional numeric Telegram user ID that enables `/status`.

## Network note

If the VPS has broken IPv6 connectivity, disable IPv6 or fix it before running
the bot. Telegram's DNS can return IPv6 first, causing request timeouts when
IPv6 TLS connections hang.

## Logs

```bash
sudo journalctl -u videobot -f
```
