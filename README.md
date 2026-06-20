# telegram-immich-bot

A Telegram bot that uploads photos, videos, and archives directly to your [Immich](https://immich.app/) instance. Supports album management, photographer tagging, JDownloader integration, URL downloads, and automatic folder watching.

## Features

- **Upload from Telegram** — send photos, videos, or documents directly to your Immich library
- **Archive extraction** — ZIP, RAR, 7z, TAR archives are automatically extracted before upload
- **Album tagging** — use `#album <name>` in captions or messages to assign assets to a named album
- **Photographer tagging** — use `#fotografo <name>` to tag the photographer; a dedicated album is created automatically
- **Tag memory** — active tags are remembered for 30 minutes across multiple uploads
- **URL download** — send a URL to download and import the file (supports `wget`, `yt-dlp`, WeTransfer)
- **JDownloader integration** — links to Mega, Google Drive, Dropbox, Instagram, and other sites are forwarded to JDownloader automatically
- **Watch folder** — files dropped in the import directory are automatically imported every 30 seconds
- **Duplicate detection** — duplicate assets are detected via SHA-1 checksum and skipped gracefully
- **Access control** — only whitelisted Telegram user IDs can interact with the bot

## Requirements

- Docker (recommended) or Python 3.11+
- A running [Immich](https://immich.app/) instance
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- (Optional) A [MyJDownloader](https://my.jdownloader.org/) account for remote download support

## Quick Start with Docker

```yaml
# docker-compose.yml
services:
  tg-immich-bot:
    build: .
    restart: unless-stopped
    environment:
      TELEGRAM_TOKEN: "your-telegram-bot-token"
      IMMICH_URL: "http://your-immich-host:2283"
      IMMICH_API_KEY: "your-immich-api-key"
      ALBUM_ID: "optional-default-album-id"
      ALLOWED_USER_IDS: "123456789,987654321"
      IMPORT_DIR: "/import"
      MYJD_USER: "optional-myjdownloader-email"
      MYJD_PASSWORD: "optional-myjdownloader-password"
      MYJD_DEVICE: "jdownloader"
    volumes:
      - /path/to/import:/import
```

```bash
docker compose up -d
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | Yes | Telegram bot token from @BotFather |
| `IMMICH_URL` | Yes | Base URL of your Immich instance (e.g. `http://192.168.1.10:2283`) |
| `IMMICH_API_KEY` | Yes | Immich API key (Settings → API Keys) |
| `ALBUM_ID` | No | Default album ID where all uploads are added |
| `ALLOWED_USER_IDS` | Yes | Comma-separated list of Telegram user IDs allowed to use the bot |
| `IMPORT_DIR` | No | Directory to watch for files (default: `/import`) |
| `MYJD_USER` | No | MyJDownloader account email |
| `MYJD_PASSWORD` | No | MyJDownloader account password |
| `MYJD_DEVICE` | No | JDownloader device name (default: `jdownloader`) |

## Usage

### Sending files

Send any photo, video, or document to the bot. Archives (ZIP, RAR, 7z) are automatically extracted.

### Tagging

Add tags in the message caption or as a standalone text message:

```
#album Vacation 2024
#fotografo John Doe
```

Tags remain active for 30 minutes. Use the `/tags` command to view or change active tags via inline buttons.

### Sending URLs

Send a URL to download and import the content:

- **Direct links / WeTransfer** — downloaded with `wget` or `transferwee`
- **YouTube / Instagram / TikTok** — downloaded with `yt-dlp`
- **Mega / Google Drive / Dropbox / etc.** — forwarded to JDownloader (requires `MYJD_*` variables)

Append `/jd` anywhere in the message to force forwarding to JDownloader.

### Commands

| Command | Description |
|---|---|
| `/start` | Show the tag management menu |
| `/tags` | Show the tag management menu |

## How It Works

1. The bot receives a file or URL from an authorized Telegram user.
2. Files are downloaded to a temporary directory.
3. Archives are recursively extracted.
4. Each supported file is uploaded to Immich via the REST API.
5. Assets are added to the configured albums (default album, `#album` tag, `#fotografo` album).
6. Duplicates are detected by SHA-1 checksum and skipped.
7. The watch folder task runs every 30 seconds and imports any files found in `IMPORT_DIR`.

## Supported File Types

Photos: `jpg`, `jpeg`, `png`, `heic`, `webp`  
Videos: `mp4`, `mov`, `avi`  
RAW: `raf`, `cr2`, `cr3`, `nef`, `arw`, `dng`, `rw2`, `orf`, `raw`  
Archives: `zip`, `rar`, `7z`, `tar`, `gz`

## License

MIT
