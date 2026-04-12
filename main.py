#!/usr/bin/env python3
"""
discord_gif_exporter.py
-----------------------
Ingests a browser HAR export from Discord's favorites panel and downloads
all GIFs, MP4s, WEBPs, and WEBMs to a user-defined output directory.

Usage:
    python discord_gif_exporter.py <har_file> <output_dir> [options]
    python discord_gif_exporter.py <output_dir> --retry-failed <log_file> [options]

Options:
    --dry-run           Print URLs and filenames without downloading
    --concurrency N     Max parallel downloads (default: 4)
    --skip-existing     Skip files already present in output_dir (default: True)
    --no-skip-existing  Re-download files even if they already exist
    --log-file PATH     Write structured log to this file (JSON Lines)
    --source FILTER     Only download from this source domain (e.g. tenor.com)
    --retry-failed LOG  Retry failed downloads from a previous JSONL log

Exit codes:
    0  All downloads succeeded (or dry-run completed)
    1  Some downloads failed (partial success)
    2  Fatal error (HAR parse failure, bad output dir, etc.)

Designed for extension:
    - Add a --upload-klipy flag in a future pass
    - MediaItem dataclass is the canonical transfer object across all phases
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TARGET_EXTENSIONS = {".gif", ".mp4", ".webp", ".webm", ".png"}

# Discord serves media through an external image proxy. The real origin URL
# is embedded in the proxy path.
DISCORD_PROXY_PATTERN = re.compile(
    r"^/external/[^/]+/"       # /external/{hash}/
    r"([^/]*)/??"              # optional encoded query segment (e.g. %3Fc%3D...)
    r"(https?)"                # protocol
    r"/(.+)$"                  # rest of the real URL path
)

# Friendly source labels for reporting
SOURCE_LABELS = {
    "media.tenor.com": "Tenor",
    "c.tenor.com": "Tenor",
    "media.tenor.co": "Tenor",
    "media.discordapp.net": "Discord CDN",
    "images.discordapp.net": "Discord CDN",
}

def source_label(netloc: str) -> str:
    for key, label in SOURCE_LABELS.items():
        if netloc.endswith(key):
            return label
    if "giphy" in netloc:
        return "Giphy"
    return netloc


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MediaItem:
    """Canonical representation of a single media file to download."""
    real_url: str               # Resolved origin URL (no Discord proxy)
    filename: str               # Target filename on disk
    extension: str              # e.g. ".mp4"
    source_domain: str          # e.g. "media.tenor.com"
    proxy_url: str = ""         # Original URL as seen in HAR (may be proxied)
    dedupe_key: str = ""        # Used for deduplication before download

    def __post_init__(self):
        if not self.dedupe_key:
            self.dedupe_key = self._compute_dedupe_key()

    def _compute_dedupe_key(self) -> str:
        # Normalise: strip query string, lowercase path
        parsed = urlparse(self.real_url)
        normalised = f"{parsed.netloc}{parsed.path}".lower()
        return hashlib.md5(normalised.encode()).hexdigest()


@dataclass
class DownloadResult:
    item: MediaItem
    success: bool
    output_path: Optional[Path] = None
    error: Optional[str] = None
    skipped: bool = False
    bytes_written: int = 0


# ---------------------------------------------------------------------------
# HAR parsing
# ---------------------------------------------------------------------------

def load_har(har_path: Path) -> list[dict]:
    """Load and return HAR entries. Raises SystemExit on parse failure."""
    try:
        with open(har_path, encoding="utf-8") as f:
            har = json.load(f)
        return har["log"]["entries"]
    except (json.JSONDecodeError, KeyError) as exc:
        logging.critical("Failed to parse HAR file: %s", exc)
        sys.exit(2)
    except OSError as exc:
        logging.critical("Cannot read HAR file: %s", exc)
        sys.exit(2)


def resolve_real_url(url: str) -> str:
    """
    Resolve a Discord proxy URL to its origin URL.
    Returns the input unchanged if it is already a direct URL.
    """
    parsed = urlparse(url)

    if "images-ext" not in parsed.netloc or "/external/" not in parsed.path:
        return url

    m = DISCORD_PROXY_PATTERN.match(parsed.path)
    if not m:
        return url

    encoded_query_segment = m.group(1)   # may be empty
    protocol = m.group(2)
    rest = m.group(3)

    real_url = f"{protocol}://{rest}"

    # The encoded segment may carry query params for the origin (e.g. ?c=V1_discord)
    if encoded_query_segment:
        decoded = unquote(encoded_query_segment)
        if decoded.startswith("?"):
            real_url += decoded

    return real_url


def extract_media_items(entries: list[dict]) -> list[MediaItem]:
    """
    Parse all HAR entries and return a deduplicated list of MediaItem objects.
    """
    seen_keys: set[str] = set()
    items: list[MediaItem] = []

    for entry in entries:
        raw_url: str = entry["request"]["url"]
        real_url = resolve_real_url(raw_url)

        parsed = urlparse(real_url)
        path_lower = parsed.path.lower()

        # Identify target extension
        ext = None
        for candidate in TARGET_EXTENSIONS:
            if path_lower.endswith(candidate):
                ext = candidate
                break
        if ext is None:
            continue

        # Build filename from the URL path segment
        raw_filename = Path(parsed.path).name
        if not raw_filename or raw_filename == "/":
            continue

        item = MediaItem(
            real_url=real_url,
            filename=raw_filename,
            extension=ext,
            source_domain=parsed.netloc,
            proxy_url=raw_url if raw_url != real_url else "",
        )

        if item.dedupe_key in seen_keys:
            continue

        seen_keys.add(item.dedupe_key)
        items.append(item)

    return items


def apply_source_filter(items: list[MediaItem], source_filter: Optional[str]) -> list[MediaItem]:
    if not source_filter:
        return items
    return [i for i in items if source_filter.lower() in i.source_domain.lower()]


def load_failed_items(log_path: Path) -> list[MediaItem]:
    """
    Read a JSONL log file and reconstruct MediaItem objects from 'failed' entries.
    Deduplicates by URL (keeps the last occurrence per dedupe_key).
    """
    items_by_key: dict[str, MediaItem] = {}

    with open(log_path, encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logging.warning("Skipping malformed JSONL line %d", line_num)
                continue

            if record.get("event") != "failed":
                continue

            url = record.get("url", "")
            filename = record.get("filename", "")
            if not url or not filename:
                logging.warning("Skipping incomplete failed entry at line %d", line_num)
                continue

            # Derive extension from filename
            ext = None
            for candidate in TARGET_EXTENSIONS:
                if filename.lower().endswith(candidate):
                    ext = candidate
                    break
            if ext is None:
                logging.warning("Skipping unrecognized extension in '%s' at line %d", filename, line_num)
                continue

            parsed = urlparse(url)
            item = MediaItem(
                real_url=url,
                filename=filename,
                extension=ext,
                source_domain=parsed.netloc,
            )
            items_by_key[item.dedupe_key] = item

    return list(items_by_key.values())


# ---------------------------------------------------------------------------
# Filename collision handling
# ---------------------------------------------------------------------------

def resolve_output_path(output_dir: Path, item: MediaItem, existing: set[str]) -> Path:
    """
    Return a collision-free output path.
    If the filename already exists (from a different URL), append a short hash.
    """
    stem = Path(item.filename).stem
    ext = item.extension

    candidate = output_dir / item.filename
    candidate_name = item.filename

    if candidate_name in existing:
        # Collision - disambiguate with first 6 chars of dedupe hash
        short_hash = item.dedupe_key[:6]
        candidate_name = f"{stem}_{short_hash}{ext}"
        candidate = output_dir / candidate_name

    existing.add(candidate_name)
    return candidate


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://discord.com/",
}

def download_item(
    item: MediaItem,
    output_path: Path,
    skip_existing: bool,
) -> DownloadResult:
    if skip_existing and output_path.exists():
        return DownloadResult(item=item, success=True, output_path=output_path, skipped=True)

    req = urllib.request.Request(item.real_url, headers=HEADERS)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()

        output_path.write_bytes(data)
        return DownloadResult(
            item=item,
            success=True,
            output_path=output_path,
            bytes_written=len(data),
        )

    except urllib.error.HTTPError as exc:
        return DownloadResult(item=item, success=False, error=f"HTTP {exc.code}: {exc.reason}")
    except urllib.error.URLError as exc:
        return DownloadResult(item=item, success=False, error=f"URL error: {exc.reason}")
    except OSError as exc:
        return DownloadResult(item=item, success=False, error=f"IO error: {exc}")


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

def setup_logging(log_file: Optional[Path], verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=handlers,
    )


def emit_jsonl(log_file: Optional[Path], record: dict) -> None:
    if not log_file:
        return
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_summary(results: list[DownloadResult]) -> None:
    total = len(results)
    succeeded = sum(1 for r in results if r.success and not r.skipped)
    skipped = sum(1 for r in results if r.skipped)
    failed = sum(1 for r in results if not r.success)
    total_bytes = sum(r.bytes_written for r in results)

    source_counts: Counter = Counter()
    for r in results:
        if r.success and not r.skipped:
            source_counts[source_label(r.item.source_domain)] += 1

    print("\n" + "=" * 60)
    print("  DOWNLOAD SUMMARY")
    print("=" * 60)
    print(f"  Total media found : {total}")
    print(f"  Downloaded        : {succeeded}")
    print(f"  Skipped (exist)   : {skipped}")
    print(f"  Failed            : {failed}")
    print(f"  Data written      : {total_bytes / 1_048_576:.1f} MB")

    if source_counts:
        print("\n  By source:")
        for src, count in source_counts.most_common():
            print(f"    {src:25s} {count}")

    if failed:
        print("\n  Failed downloads:")
        for r in results:
            if not r.success:
                print(f"    [{r.error}]  {r.item.filename}")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Export Discord favorited GIFs/MP4s from a HAR file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("har_file", nargs="?", type=Path, default=None,
                   help="Path to the .har file from browser DevTools")
    p.add_argument("output_dir", type=Path, help="Directory where media files will be saved")
    p.add_argument("--dry-run", action="store_true", help="List files without downloading")
    p.add_argument("--concurrency", type=int, default=4, metavar="N",
                   help="Parallel download threads (default: 4)")
    p.add_argument("--skip-existing", dest="skip_existing", action="store_true", default=True,
                   help="Skip files already on disk (default: on)")
    p.add_argument("--no-skip-existing", dest="skip_existing", action="store_false",
                   help="Re-download files even if they exist on disk")
    p.add_argument("--log-file", type=Path, metavar="PATH",
                   help="Append structured JSON Lines log to this file")
    p.add_argument("--source", metavar="DOMAIN",
                   help="Filter to only download from this source domain (e.g. tenor.com)")
    p.add_argument("--verbose", action="store_true", help="Enable debug logging")
    p.add_argument("--retry-failed", type=Path, metavar="LOG",
                   help="Retry failed downloads from a previous JSONL log file")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    setup_logging(args.log_file, args.verbose)
    log = logging.getLogger(__name__)

    # Validate: exactly one of har_file or --retry-failed
    if args.har_file and args.retry_failed:
        parser.error("Cannot use both har_file and --retry-failed")
    if not args.har_file and not args.retry_failed:
        parser.error("Either har_file or --retry-failed is required")

    if not args.dry_run:
        try:
            args.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.critical("Cannot create output directory: %s", exc)
            sys.exit(2)

    if args.retry_failed:
        # Retry mode: load failed items from JSONL log
        if not args.retry_failed.exists():
            log.critical("Log file not found: %s", args.retry_failed)
            sys.exit(2)
        log.info("Loading failed items from: %s", args.retry_failed)
        try:
            items = load_failed_items(args.retry_failed)
        except OSError as exc:
            log.critical("Cannot read log file: %s", exc)
            sys.exit(2)
        items = apply_source_filter(items, args.source)
        log.info("Found %d failed items to retry", len(items))
    else:
        # Normal mode: parse HAR file
        if not args.har_file.exists():
            log.critical("HAR file not found: %s", args.har_file)
            sys.exit(2)
        log.info("Parsing HAR file: %s", args.har_file)
        entries = load_har(args.har_file)
        log.info("HAR contains %d total entries", len(entries))
        items = extract_media_items(entries)
        items = apply_source_filter(items, args.source)
        log.info("Found %d unique media items", len(items))

    if not items:
        log.warning("No matching media found. Check --source filter or HAR content.")
        sys.exit(0)

    # Source breakdown
    source_counts: Counter = Counter(source_label(i.source_domain) for i in items)
    for src, count in source_counts.most_common():
        log.info("  %-25s %d items", src, count)

    # Dry run
    if args.dry_run:
        print(f"\n{'URL':<80}  FILENAME")
        print("-" * 110)
        for item in items:
            print(f"{item.real_url:<80}  {item.filename}")
        print(f"\nTotal: {len(items)} items (dry run - nothing downloaded)")
        return

    # Build output paths (collision-aware)
    used_names: set[str] = set()
    planned: list[tuple[MediaItem, Path]] = []
    for item in items:
        out_path = resolve_output_path(args.output_dir, item, used_names)
        planned.append((item, out_path))

    # Download
    log.info("Downloading to: %s  (concurrency=%d)", args.output_dir, args.concurrency)

    results: list[DownloadResult] = []
    completed = 0

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(download_item, item, out_path, args.skip_existing): (item, out_path)
            for item, out_path in planned
        }

        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1

            if result.skipped:
                log.debug("[SKIP] %s", result.item.filename)
            elif result.success:
                log.info(
                    "[%4d/%d] OK    %s  (%.1f KB)",
                    completed, len(planned),
                    result.item.filename,
                    result.bytes_written / 1024,
                )
                emit_jsonl(args.log_file, {
                    "event": "downloaded",
                    "filename": result.item.filename,
                    "url": result.item.real_url,
                    "source": result.item.source_domain,
                    "bytes": result.bytes_written,
                    "ts": time.time(),
                })
            else:
                log.warning(
                    "[%4d/%d] FAIL  %s  -- %s",
                    completed, len(planned),
                    result.item.filename,
                    result.error,
                )
                emit_jsonl(args.log_file, {
                    "event": "failed",
                    "filename": result.item.filename,
                    "url": result.item.real_url,
                    "error": result.error,
                    "ts": time.time(),
                })

    print_summary(results)

    any_failures = any(not r.success and not r.skipped for r in results)
    sys.exit(1 if any_failures else 0)


if __name__ == "__main__":
    main()