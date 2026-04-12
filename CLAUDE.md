# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A single-file Python CLI that exports Discord favorited GIFs/media from a browser HAR file. It parses HAR exports captured from Discord's favorites panel, resolves Discord's external image proxy URLs to their real origins (Tenor, Giphy, Discord CDN, etc.), deduplicates, and downloads media concurrently.

## Running

```bash
# No external dependencies — stdlib only (Python 3.10+)
python main.py <har_file> <output_dir>

# Useful flags
python main.py export.har ./gifs --dry-run           # list without downloading
python main.py export.har ./gifs --source tenor.com   # filter by source domain
python main.py export.har ./gifs --concurrency 8      # parallel download threads
python main.py export.har ./gifs --log-file dl.jsonl   # structured JSON Lines log
```

Exit codes: 0 = success, 1 = partial failure (some downloads failed), 2 = fatal error (bad HAR, bad output dir).

## Architecture

Everything lives in `main.py`. The pipeline is linear:

1. **HAR parsing** (`load_har`, `extract_media_items`) — reads HAR JSON, filters entries by `TARGET_EXTENSIONS` (.gif, .mp4, .webp, .webm, .png)
2. **Proxy resolution** (`resolve_real_url`) — Discord wraps external media through `images-ext-*/external/{hash}/` proxy URLs. `DISCORD_PROXY_PATTERN` regex extracts the real origin URL including encoded query params.
3. **Deduplication** — `MediaItem.dedupe_key` is an MD5 of the normalized (no query string, lowercased) URL. Duplicates are dropped during extraction.
4. **Filename collision handling** (`resolve_output_path`) — if two different URLs produce the same filename, a 6-char hash suffix is appended.
5. **Concurrent download** (`download_item` via `ThreadPoolExecutor`) — uses `urllib.request` with Discord referer header. No third-party HTTP library.

Key dataclasses: `MediaItem` (canonical media record used throughout) and `DownloadResult` (per-file outcome).

## Design Decisions

- **No dependencies**: intentional — uses only `urllib.request`, `concurrent.futures`, `argparse`, `json`, `pathlib`, etc. Don't add `requests`, `aiohttp`, or similar without good reason.
- **`--upload-klipy` flag**: mentioned in the docstring as a planned future extension.
- **`MediaItem` as transfer object**: all pipeline stages operate on `MediaItem` instances. New features should extend this dataclass rather than introducing parallel data structures.
