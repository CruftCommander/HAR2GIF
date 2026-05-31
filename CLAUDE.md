# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A single-file Python CLI that exports Discord favorited GIFs/media from a browser HAR file. It parses HAR exports captured from Discord's favorites panel, resolves Discord's external image proxy URLs to their real origins (Tenor, Giphy, Discord CDN, etc.), deduplicates, and downloads media concurrently.

## Running

```bash
# No external dependencies — stdlib only (Python 3.10+)
python main.py <har_file> <output_dir>

# Useful flags
python main.py export.har ./gifs --dry-run            # list without downloading
python main.py export.har ./gifs --source tenor.com   # filter by source domain
python main.py export.har ./gifs --concurrency 8      # parallel download threads
python main.py export.har ./gifs --log-file dl.jsonl  # structured JSON Lines log
python main.py export.har ./gifs --no-skip-existing   # re-download even if on disk

# Retry mode: re-download only the failures recorded in a prior run's log.
# Argument shape differs — there is NO har_file; output_dir is the sole
# positional and --retry-failed supplies the JSONL log to read back.
python main.py ./gifs --retry-failed dl.jsonl
```

`har_file` and `--retry-failed` are mutually exclusive, and exactly one is required (enforced in `main()`, not by argparse). The `har_file` positional is optional (`nargs="?"`) precisely to make the retry-mode argument shape work.

Exit codes: 0 = success, 1 = partial failure (some downloads failed), 2 = fatal error (bad HAR, bad output dir, missing log file).

## Architecture

Everything lives in `main.py` (its internal docstring still calls it `discord_gif_exporter.py` — the file was renamed; invoke it as `main.py`). There are **two entry paths** into the same download stage, branched in `main()`:

- **HAR mode** (default) — parse a HAR file into a deduplicated list of `MediaItem`s (steps 1–3 below).
- **Retry mode** (`--retry-failed`) — skip HAR parsing entirely; `load_failed_items` reconstructs `MediaItem`s from the `failed` records of a previous run's JSONL log.

The HAR-mode pipeline is linear:

1. **HAR parsing** (`load_har`, `extract_media_items`) — reads HAR JSON, filters entries by `TARGET_EXTENSIONS` (.gif, .mp4, .webp, .webm, .png)
2. **Proxy resolution** (`resolve_real_url`) — Discord wraps external media through `images-ext-*/external/{hash}/` proxy URLs. `DISCORD_PROXY_PATTERN` regex extracts the real origin URL including encoded query params.
3. **Deduplication** — `MediaItem.dedupe_key` is an MD5 of the normalized (no query string, lowercased) URL. Duplicates are dropped during extraction.
4. **Filename collision handling** (`resolve_output_path`) — if two different URLs produce the same filename, a 6-char hash suffix is appended.
5. **Concurrent download** (`download_item` via `ThreadPoolExecutor`) — uses `urllib.request` with Discord referer header. No third-party HTTP library.

Both entry paths converge at steps 4–5, and `--source` filtering (`apply_source_filter`) applies to either.

Key dataclasses: `MediaItem` (canonical media record used throughout) and `DownloadResult` (per-file outcome).

### The JSONL log contract

`--log-file` and `--retry-failed` are two halves of one feature, so keep their record shape in sync. When `--log-file` is set, every download emits one JSON object per line via `emit_jsonl`: `{"event": "downloaded", "filename", "url", "source", "bytes", "ts"}` on success, or `{"event": "failed", "filename", "url", "error", "ts"}` on failure. `--retry-failed` reads that same file back, keeps only `event == "failed"` records, and rebuilds `MediaItem`s from each record's `url`/`filename` (re-deriving `extension` from the filename, re-deduping by `dedupe_key`). Changing these record fields affects both the producer and the consumer.

## Design Decisions

- **No dependencies**: intentional — uses only `urllib.request`, `concurrent.futures`, `argparse`, `json`, `pathlib`, etc. Don't add `requests`, `aiohttp`, or similar without good reason. This stdlib-only stance also holds for the sibling scripts (`convert.py`, `upload.py`) — see [Dependency Decisions](#dependency-decisions).
- **`--upload-klipy` flag**: was a planned future extension; it is now realized as the separate **`upload.py`** script (assisted publish — see [Implementation Notes](#implementation-notes)). `main.py` itself still has no Klipy code.
- **`MediaItem` as transfer object**: all `main.py` pipeline stages operate on `MediaItem` instances. New features in `main.py` should extend this dataclass rather than introducing parallel data structures. (`convert.py`/`upload.py` work on files on disk and intentionally do **not** use `MediaItem` — they have their own small result dataclasses.)

## The Multi-Script Pipeline

The repo is now three independent, stdlib-only CLIs run in sequence, plus one shared module:

```
main.py  (HAR -> downloaded media)
   -> convert.py  (mp4/webm/webp -> .gif via ffmpeg, originals moved to originals/)
   -> upload.py   (interactive review -> assisted publish to Klipy)
config.py = the ONLY shared module (config dataclasses + loaders, thread-safe
            emit_jsonl, setup_logging, collision helper, binary discovery).
```

Hard rule: **`convert.py` and `upload.py` must not import each other**; shared code lives only in `config.py`. They reuse `main.py`'s *patterns* (6-char-hash collision idiom, JSONL logging, exit codes 0/1/2, `ThreadPoolExecutor`) by mirroring them, not by importing from `main.py` (which is left unmodified).

Each new script has its own JSONL event vocabulary (non-conflicting with `main.py`'s `downloaded`/`failed`): `convert.py` emits `converted`/`moved`/`failed`/`skipped`; `upload.py` emits `published`/`skipped_upload`/`failed`. `config.emit_jsonl` is thread-safe (a module-level `threading.Lock`), unlike `main.py`'s copy.

Tests live in `tests/` (stdlib `unittest`, no pytest): `python -m unittest discover -s tests`. They mock `subprocess.run`/viewer/browser so neither ffmpeg nor a network/Klipy is required.

## Platform Notes

Audit of platform-sensitive constructs, to keep a future Linux/macOS port tractable. The codebase is overwhelmingly portable; the few exceptions are isolated and branch on `sys.platform`.

- `[PLATFORM-SAFE]` All filesystem paths go through `pathlib`; all text I/O uses explicit `encoding="utf-8"`; `ThreadPoolExecutor` and `subprocess` are cross-platform. `convert.py` writes output atomically with `os.replace` (chosen over `Path.rename`, which raises on an existing target on Windows).
- `[WINDOWS-SPECIFIC]` `main.py` HEADERS User-Agent (`main.py:294-299`) is a hardcoded Windows Chrome UA string — cosmetic, but on other OSes it misrepresents the client. `config.EXE` appends `.exe` only on Windows for the bundled `bin/ffmpeg.exe`. `upload.py:open_in_viewer` uses `os.startfile` on Windows (and `clip` for the clipboard).
- `[NEEDS-VERIFICATION]` `urllib` TLS validation uses the OS cert store on Windows vs OpenSSL elsewhere (relevant to `main.py` downloads). `upload.py`'s clipboard/viewer fall back to `pbcopy`/`open` (macOS) and `xclip`/`xdg-open` (Linux) — those external tools may be absent; clipboard failure is handled (best-effort, returns False), but `xdg-open` absence raises `OSError` and is treated as a publish failure.
- Runtime log/print strings are kept ASCII (no em-dashes) so redirecting stderr/stdout to a non-UTF-8 file on Windows can't raise `UnicodeEncodeError`.

## Dependency Decisions

- **Zero pip dependencies added.** `convert.py`, `upload.py`, `config.py`, and the tests are stdlib-only, consistent with `main.py`.
- **ffmpeg via `subprocess`, not `ffmpeg-python`.** Direct binary invocation adds no pip dependency, is the most portable option, and matches the project's stdlib-only stance. The typed-call-graph convenience of `ffmpeg-python` isn't worth a dependency that can also lag ffmpeg releases.
- **Config parsing via stdlib `tomllib`** (Python 3.11+). A `tomli` fallback import exists for 3.10 but is dead code on this machine (Python 3.14); if neither is importable and a config file is present, the tool fails fast with an install hint.
- **Tests use stdlib `unittest`** (pytest is not installed in the venv).
- **TUI/preview adds no dependency** — see TUI Rendering Decision.

## TUI Rendering Decision

`upload.py` needs to show the user each GIF for review, primarily on Windows Terminal. Investigation found **no reliable way to animate a GIF in Windows Terminal**: sixel support is recent (WT 1.22+), static, and glitchy; the kitty graphics protocol is unsupported; `term-image`/`textual-image` either don't animate or are "limited support" on Windows; Rich/Textual have no native animated-GIF widget.

**Decision:** open each GIF in the **OS default viewer** (`os.startfile` on Windows, `open` on macOS, `xdg-open` on Linux) — smooth animation, zero dependencies, cross-platform. The review UI itself is a plain stdlib `print`/`input` loop (no curses, no TUI framework). An in-terminal `chafa` preview could be added later as an *optional* path only if `chafa` is already on PATH, but the system viewer is the primary and only required path.

## Implementation Notes

- **Phase 0 findings that changed the design:** (1) Klipy has no public upload API → `upload.py` is an *assisted manual publish*, not an API client (see below). (2) No reliable in-terminal GIF animation on Windows → system-viewer preview, no TUI framework. (3) Only `pip` in the venv → stdlib `unittest`, zero new deps.
- **`ASSUMPTION` marker / `NotImplementedError` stub:** `upload.py:_upload_via_api` documents that, as of 2026-05, Klipy's public API is read-only (search/trending/get/recent + engagement POSTs) with no documented GIF-upload endpoint, and raises `NotImplementedError`. The user-facing `[U]` action is instead `publish_assisted` (open GIF + copy tags + open `klipy.com/create/gif-maker`). The stub is where a real wire call should land if Klipy ever ships an upload API.
- **Deviations from the original spec (deliberate):**
  - `upload.py` is *assisted publish* rather than a true API upload (user-confirmed), because no upload API exists.
  - Under `--dry-run`, `convert.py` treats a **missing ffmpeg as warn-only** (still prints the plan, exits 0) instead of the strict "exit 2 on ffmpeg-not-found"; dry-run must be usable without ffmpeg.
  - **Quality preset + explicit override model:** the `quality` preset is the base; `fps_cap`/`scale_width`/`palette_size` override the preset *only when explicitly set* (CLI flag or present in the TOML `[convert]` table). Effective settings are always concrete (never silent `None`). Default quality is `balanced`.
  - **Intentional duplication:** thread-safe `emit_jsonl` and `setup_logging` live in `config.py` rather than being imported from `main.py` (which is left unmodified); `main.py` keeps its own non-thread-safe copies.
  - **No `models.py`:** `MediaItem`/`DownloadResult` belong to the HAR pipeline; the new scripts operate on files on disk and define their own `ConvertResult`/`UploadResult`, so no shared model module was created.
- **Upload "failure" semantics:** only hard errors (sha-256 hashing or the viewer raising `OSError`) emit a `failed` record; clipboard/browser misses are best-effort and never count as failures.
- **Note:** `__pycache__/main.cpython-314.pyc` was already git-tracked before this work; `.gitignore` now excludes `__pycache__/` going forward but does not retroactively untrack that file.
