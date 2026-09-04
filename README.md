# videobot

Telegram bot that downloads YouTube, Instagram, and TikTok videos from a
link and sends them back in chat. Built with `python-telegram-bot` and
`yt-dlp`.

## Setup (Ubuntu 24.04 VPS)

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git ffmpeg

sudo useradd -r -m -d /opt/videobot -s /usr/sbin/nologin videobot
sudo mkdir -p /opt/videobot/downloads

# clone this repo into /opt/videobot, then:
cd /opt/videobot
sudo python3 -m venv venv
sudo ./venv/bin/pip install -r requirements.txt

sudo cp .env.example .env
sudo nano .env   # set BOT_TOKEN=your_token_from_botfather
sudo chown -R videobot:videobot /opt/videobot

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

## Logs

```bash
sudo journalctl -u videobot -f
```
