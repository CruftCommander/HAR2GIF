# HAR2GIF

Rescue your Discord-favorited GIFs and migrate them off Tenor.

## Why This Exists

Google is **shutting down the Tenor API on June 30, 2026**, and Discord is changing how its GIF picker works (it has been testing **Klipy** and **Giphy** as Tenor replacements). Your saved favorites live on Discord's servers, so they probably won't vanish overnight — but once the Tenor API is gone, GIF *search* breaks and there's no guarantee your old favorites keep resolving long-term.

HAR2GIF lets you **export your favorited GIFs now** from a browser capture, convert them to real `.gif` files you own, and (optionally) re-publish them to Klipy — so you're not depending on a deprecated service.

## What It Does

Three small, independent scripts you run in sequence:

- **`main.py`** — reads a browser HAR capture of your Discord favorites and downloads every GIF/MP4/WebP/WebM, resolving Discord's proxy URLs back to their real Tenor/Giphy/Discord-CDN origins.
- **`convert.py`** — converts the downloaded MP4/WebM/WebP files into proper animated GIFs with ffmpeg (high-quality two-pass palette), tucking the originals into an `originals/` folder.
- **`upload.py`** — an interactive review queue: previews each GIF, suggests tags from the filename, and helps you publish it to Klipy.

## Requirements

- **Python 3.11+** (uses the stdlib `tomllib`; on Python 3.10 install `tomli` — `pip install tomli`). No other Python packages required.
- **ffmpeg** — needed by `convert.py` only. Install with `winget install ffmpeg` / `choco install ffmpeg` (Windows), `sudo apt install ffmpeg` (Linux), `brew install ffmpeg` (macOS), or drop `ffmpeg.exe` into `bin/` (see [ffmpeg Setup](#ffmpeg-setup)).
- **A Klipy account** — only if you want to use `upload.py` to re-publish. Everything up to that point works without one.

## Quick Start

```bash
# 1. Export your favorites from a HAR capture (see "HAR Capture" below)
python main.py favorites.har ./gifs

# 2. Convert the MP4/WebM/WebP files to GIF
python convert.py ./gifs

# 3. Review and publish to Klipy (optional)
python upload.py ./gifs
```

**Step 1** downloads every favorited item into `./gifs`. **Step 2** turns the non-GIF media into animated GIFs and moves the originals into `./gifs/originals`. **Step 3** walks you through each GIF, suggests tags, and opens it alongside Klipy's uploader.

## HAR Capture Instructions

A HAR file is just a recording of your browser's network traffic. You only need to capture it **once** — save the `.har` and you can re-run the tools against it anytime.

1. Open **Discord in your browser** (Edge or Chrome) and log in.
2. Press **F12** to open DevTools, then click the **Network** tab.
3. Make sure recording is on (the round button is red) and tick **Preserve log**.
4. Open your **GIF favorites** panel in Discord and **scroll through all of them** so every thumbnail loads.
5. In the Network tab, click the **export icon** (a down-arrow, "Export HAR…") and save the file, e.g. `favorites.har`.
6. Run `python main.py favorites.har ./gifs`.

> Tip: scroll slowly to the very bottom of your favorites so every GIF actually loads into the network log — HAR2GIF can only export what your browser fetched.

## Configuration

All settings have sensible defaults, so a config file is **optional**. To change defaults, drop a `har2gif.toml` in the folder you run from:

```toml
[convert]
quality = "balanced"   # "max" | "balanced" | "small"
concurrency = 4        # parallel ffmpeg workers

[upload]
auto_upload = false    # if true, skip the per-GIF prompt
skip_uploaded = true   # skip files already recorded in the manifest
```

The Klipy API key, if you set one, comes from the `KLIPY_API_KEY` environment variable (or `--api-key`). A CLI flag overrides the config file, which overrides the defaults.

## Flags Reference

**`main.py`** (`python main.py <har_file> <output_dir>`)

| Flag | What it does |
|------|--------------|
| `--dry-run` | List what would be downloaded; download nothing |
| `--source tenor.com` | Only download from one source domain |
| `--concurrency N` | Parallel download threads |
| `--retry-failed LOG` | Retry the failures from a previous run's log |

**`convert.py`** (`python convert.py <media_dir>`)

| Flag | What it does |
|------|--------------|
| `--quality max\|balanced\|small` | GIF quality preset (default: balanced) |
| `--fps N` | Cap the frame rate |
| `--dry-run` | Show the conversion plan; convert nothing |
| `--yes` | Skip the confirmation prompt |
| `--ffmpeg-path PATH` | Use a specific ffmpeg binary |

**`upload.py`** (`python upload.py <gif_dir>`)

| Flag | What it does |
|------|--------------|
| `--dry-run` | Walk the queue without opening anything |
| `--auto-upload` | Don't prompt for each GIF |
| `--filter-unconverted` | Only show GIFs not already published |
| `--api-key KEY` | Klipy API key (else `KLIPY_API_KEY`) |

## ffmpeg Setup

`convert.py` looks for ffmpeg in this order: `--ffmpeg-path` → your system `PATH` → `bin/ffmpeg.exe` (checked relative to the script).

- **Easiest:** install system-wide (`winget install ffmpeg`, `choco install ffmpeg`, `apt install ffmpeg`, or `brew install ffmpeg`) and it's found automatically.
- **No install:** download the "essentials" Windows build from <https://www.gyan.dev/ffmpeg/builds/> and copy `ffmpeg.exe` into `bin/`.

See [`bin/README.md`](bin/README.md) for details. The binary is git-ignored — don't commit it.

> A note on `upload.py`: Klipy doesn't (yet) offer a public GIF-*upload* API, so the publish step is an **assisted** one — it opens each GIF in your viewer, copies the tags to your clipboard, and opens Klipy's GIF-maker page so you can finish the upload there. A SHA-256 manifest keeps track of what you've already done so re-runs skip it.
