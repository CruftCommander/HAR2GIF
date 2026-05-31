#!/usr/bin/env python3
"""
upload.py
---------
Interactive review queue for publishing converted GIFs to Klipy.

Klipy is one of the GIF providers Discord is testing as a Tenor replacement.
This is the third stage of the pipeline (main.py -> convert.py -> upload.py).

Usage:
    python upload.py <gif_dir> [options]

For each GIF the tool shows a card, suggests tags from the filename, and on
confirmation performs an "assisted publish". An SHA-256-keyed manifest makes the
process idempotent so re-runs skip already-published files.

Exit codes:
    0  Completed (everything published, skipped, or already done)
    1  Partial failure (one or more files errored)
    2  Fatal error (bad directory)

----------------------------------------------------------------------------
Phase 0 findings baked into this file (full notes in CLAUDE.md):
  * Klipy API: as of 2026-05 Klipy's PUBLIC API is read-only (search / trending
    / get / recent, plus engagement POSTs). There is NO documented GIF-upload
    endpoint; user uploads happen only through the klipy.com web UI. So the
    "[U] publish" action is an ASSISTED MANUAL PUBLISH: it opens the GIF, surfaces
    the tags (best-effort clipboard copy), and opens the Klipy "GIF maker" page.
    A `_upload_via_api()` stub is kept (raising NotImplementedError) so a real
    wire call has an obvious home if Klipy ever ships an upload API.
  * 0C TUI rendering: no terminal reliably ANIMATES GIFs on Windows Terminal
    (sixel is static/glitchy, kitty unsupported, term-image/textual-image don't
    animate there). The preview therefore opens each GIF in the OS default viewer
    (os.startfile / open / xdg-open). The review UI itself is a plain stdlib
    print/input loop -- no TUI framework, zero pip dependencies.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import config

log = logging.getLogger("upload")

# Trailing 6-hex collision suffix added by convert.py / main.py (e.g. _b758c2).
_HASH_SUFFIX = re.compile(r"_[0-9a-f]{6}$")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class UploadResult:
    path: Path
    sha256: str
    action: str                 # "published" | "skipped" | "already" | "failed"
    tags: list = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Manifest (SHA-256-keyed, versioned, atomic)
# ---------------------------------------------------------------------------

def manifest_load(path: Path) -> dict:
    """Load the manifest, or return a fresh empty one if it does not exist."""
    if not path.exists():
        return {"version": config.MANIFEST_VERSION, "entries": {}}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("version", config.MANIFEST_VERSION)
    data.setdefault("entries", {})
    return data


def manifest_save(path: Path, data: dict) -> None:
    """Write the manifest atomically (write .tmp, then os.replace)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def file_sha256(path: Path) -> str:
    """Stream the file in 1 MiB chunks and return its hex SHA-256."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def already_uploaded(manifest: dict, sha: str) -> bool:
    return sha in manifest.get("entries", {})


# ---------------------------------------------------------------------------
# Tag suggestion heuristic
# ---------------------------------------------------------------------------

def suggest_tags(filename: str) -> list:
    """Derive suggested tags from a filename stem.

    Strips a trailing 6-hex collision suffix, then treats the first dash segment
    as one tag and joins the remaining segments into a second tag, e.g.
    ``dancing-al-bundy_b758c2.gif`` -> ``["dancing", "al bundy"]``.
    """
    stem = _HASH_SUFFIX.sub("", Path(filename).stem)
    segs = [s for s in stem.split("-") if s]
    if not segs:
        return []
    if len(segs) == 1:
        return [segs[0]]
    return [segs[0], " ".join(segs[1:])]


# ---------------------------------------------------------------------------
# Best-effort side effects (never raise to the caller, except open_in_viewer)
# ---------------------------------------------------------------------------

def copy_to_clipboard(text: str) -> bool:
    """Copy text to the OS clipboard. Returns False (never raises) if no
    clipboard tool is available."""
    if sys.platform == "win32":
        cmd = ["clip"]
    elif sys.platform == "darwin":
        cmd = ["pbcopy"]
    else:
        cmd = ["xclip", "-selection", "clipboard"]
    try:
        proc = subprocess.run(cmd, input=text.encode("utf-8"),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def open_in_viewer(path: Path) -> None:
    """Open a file in the OS default application. May raise OSError."""
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]  # Windows-only
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def open_klipy() -> None:
    """Open the Klipy GIF-maker page in the default browser."""
    webbrowser.open(config.KLIPY_CREATE_URL)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def publish_assisted(path: Path, tags: list, *, dry_run: bool) -> None:
    """Perform the assisted-publish side effects for one GIF.

    Opens the GIF in the system viewer, copies the tags to the clipboard
    (printing them if that fails), and opens the Klipy create page. A dry run
    performs none of these. May raise OSError (treated as a hard failure).
    """
    if dry_run:
        return
    open_in_viewer(path)
    if not copy_to_clipboard(", ".join(tags)):
        print(f"  (Could not copy tags to clipboard.) Tags: {', '.join(tags)}")
    open_klipy()


def _upload_via_api(path: Path, tags: list, api_key: str):
    # ASSUMPTION: As of 2026-05, Klipy exposes NO documented public GIF-upload
    # endpoint. Its public API is read-only (search / trending / get / recent),
    # plus engagement POSTs (share-trigger / report) and a hide-from-recent
    # DELETE -- none of which upload media. User-generated GIFs are created only
    # through the klipy.com web UI. We therefore do not invent a wire format;
    # the assisted-publish flow above is the supported path. If Klipy ships an
    # upload endpoint, implement the real request here.
    raise NotImplementedError(
        "Klipy has no public GIF-upload API; upload.py uses assisted manual "
        f"publish instead (finish at {config.KLIPY_CREATE_URL})."
    )


# ---------------------------------------------------------------------------
# JSONL records
# ---------------------------------------------------------------------------

def _rec_published(gif: Path, sha: str, tags: list, dry_run: bool) -> dict:
    return {"event": "published", "filename": gif.name, "sha256": sha,
            "tags": tags, "dry_run": dry_run, "ts": time.time()}


def _rec_skipped(gif: Path, sha: str, reason: str) -> dict:
    return {"event": "skipped_upload", "filename": gif.name, "sha256": sha,
            "reason": reason, "ts": time.time()}


def _rec_failed(gif: Path, sha: Optional[str], error: str) -> dict:
    return {"event": "failed", "filename": gif.name, "sha256": sha,
            "error": error, "ts": time.time()}


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------

def _print_card(idx: int, total: int, gif: Path, tags: list) -> None:
    size_mb = gif.stat().st_size / 1_048_576
    print("\n" + "-" * 67)
    print(f"  HAR2GIF - Klipy Upload Review            [{idx} of {total}]")
    print(f"  File: {gif.name}   ({size_mb:.1f} MB)")
    print("-" * 67)
    print(f"  Suggested tags: {', '.join(tags) if tags else '(none)'}")


def _prompt_tags(suggested: list) -> list:
    try:
        raw = input(f"  Tags [{', '.join(suggested)}] (Enter to accept): ").strip()
    except EOFError:
        return suggested
    if not raw:
        return suggested
    return [t.strip() for t in raw.split(",") if t.strip()]


def _prompt_action() -> str:
    try:
        raw = input("  [U]pload  [S]kip  [A]uto-all  [X]skip-remaining  [Q]uit: ").strip().lower()
    except EOFError:
        return "q"
    return raw[:1] if raw else "s"


def _do_publish(gif: Path, sha: str, tags: list, manifest: dict,
                manifest_path: Path, dry_run: bool, log_file: Optional[Path]) -> UploadResult:
    try:
        publish_assisted(gif, tags, dry_run=dry_run)
    except OSError as exc:
        config.emit_jsonl(log_file, _rec_failed(gif, sha, str(exc)))
        return UploadResult(gif, sha, "failed", tags, error=str(exc))
    if not dry_run:
        manifest["entries"][sha] = {
            "filename": gif.name, "tags": tags, "published_ts": time.time(),
        }
        manifest_save(manifest_path, manifest)
    config.emit_jsonl(log_file, _rec_published(gif, sha, tags, dry_run))
    return UploadResult(gif, sha, "published", tags)


def review_queue(gifs: list, manifest: dict, manifest_path: Path,
                 cfg: config.UploadConfig, *, dry_run: bool, skip_already: bool,
                 log_file: Optional[Path]) -> list:
    """Drive the review queue over ``gifs`` and return per-file results."""
    results: list = []
    auto = bool(cfg.auto_upload)
    stop = False
    total = len(gifs)

    for idx, gif in enumerate(gifs, 1):
        try:
            sha = file_sha256(gif)
        except OSError as exc:
            config.emit_jsonl(log_file, _rec_failed(gif, None, str(exc)))
            results.append(UploadResult(gif, "", "failed", [], error=str(exc)))
            continue

        if skip_already and already_uploaded(manifest, sha):
            config.emit_jsonl(log_file, _rec_skipped(gif, sha, "already"))
            results.append(UploadResult(gif, sha, "already", []))
            continue

        if stop:
            config.emit_jsonl(log_file, _rec_skipped(gif, sha, "auto_skip_all"))
            results.append(UploadResult(gif, sha, "skipped", []))
            continue

        tags = suggest_tags(gif.name)
        _print_card(idx, total, gif, tags)

        if auto:
            results.append(_do_publish(gif, sha, tags, manifest, manifest_path,
                                       dry_run, log_file))
            continue

        tags = _prompt_tags(tags)
        action = _prompt_action()
        if action == "u":
            results.append(_do_publish(gif, sha, tags, manifest, manifest_path,
                                       dry_run, log_file))
        elif action == "a":
            auto = True
            results.append(_do_publish(gif, sha, tags, manifest, manifest_path,
                                       dry_run, log_file))
        elif action == "s":
            config.emit_jsonl(log_file, _rec_skipped(gif, sha, "user_skip"))
            results.append(UploadResult(gif, sha, "skipped", []))
        else:  # "x" or "q": stop prompting, skip this and all remaining
            stop = True
            config.emit_jsonl(log_file, _rec_skipped(gif, sha, "auto_skip_all"))
            results.append(UploadResult(gif, sha, "skipped", []))

    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_summary(results: list) -> None:
    published = sum(1 for r in results if r.action == "published")
    skipped = sum(1 for r in results if r.action in ("skipped", "already"))
    failed = sum(1 for r in results if r.action == "failed")
    print("\n" + "=" * 60)
    print("  UPLOAD SUMMARY")
    print("=" * 60)
    print(f"  Published / sent to Klipy : {published}")
    print(f"  Skipped                   : {skipped}")
    print(f"  Failed                    : {failed}")
    if failed:
        print("\n  Failed files:")
        for r in results:
            if r.action == "failed":
                print(f"    [{r.error}]  {r.path.name}")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Review and assisted-publish converted GIFs to Klipy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("gif_dir", type=Path, help="Directory of .gif files to review")
    p.add_argument("--manifest", type=Path, default=None,
                   help="Manifest path (default: <gif_dir>/klipy-manifest.json)")
    p.add_argument("--auto-upload", dest="auto_upload", action="store_const",
                   const=True, default=None,
                   help="Publish every file without prompting")
    p.add_argument("--dry-run", action="store_true",
                   help="Walk the queue but perform no side effects")
    p.add_argument("--log-file", type=Path, default=None, metavar="PATH",
                   help="JSONL upload log")
    p.add_argument("--api-key", dest="api_key", default=None,
                   help="Klipy API key (else KLIPY_API_KEY env var or config)")
    p.add_argument("--filter-unconverted", action="store_true",
                   help="Only show files not already in the manifest")
    p.add_argument("--no-skip-uploaded", dest="skip_uploaded", action="store_const",
                   const=False, default=None,
                   help="Review files even if already in the manifest")
    p.add_argument("--config", type=Path, default=None, metavar="PATH",
                   help=f"Path to {config.CONFIG_FILENAME} (default: ./{config.CONFIG_FILENAME})")
    p.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return p


def main() -> None:
    args = build_parser().parse_args()
    config.setup_logging(None, args.verbose)  # console only; --log-file is JSONL-only

    raw = config.load_toml(config.find_config(args.config))
    cfg = config.build_upload_config(raw, args)

    gif_dir: Path = args.gif_dir
    if not gif_dir.is_dir():
        log.critical("GIF directory not found: %s", gif_dir)
        sys.exit(config.EXIT_FATAL)

    manifest_path = Path(cfg.manifest_path)
    if not manifest_path.is_absolute():
        manifest_path = gif_dir / manifest_path

    gifs = sorted(gif_dir.glob("*.gif"))
    if not gifs:
        log.warning("No .gif files found in %s", gif_dir)
        sys.exit(config.EXIT_OK)

    manifest = manifest_load(manifest_path)
    skip_already = cfg.skip_uploaded or args.filter_unconverted

    print("\nHAR2GIF assisted Klipy publish")
    print("  Klipy has no public upload API, so [U] opens each GIF in your")
    print(f"  viewer, copies its tags, and opens {config.KLIPY_CREATE_URL}")
    print("  for you to finish the upload there.")
    print(f"  Reviewing {len(gifs)} GIF(s) from {gif_dir}")
    print(f"  Manifest: {manifest_path}   Dry run: {args.dry_run}")

    results = review_queue(
        gifs, manifest, manifest_path, cfg,
        dry_run=args.dry_run, skip_already=skip_already, log_file=args.log_file,
    )
    print_summary(results)

    any_failed = any(r.action == "failed" for r in results)
    sys.exit(config.EXIT_PARTIAL if any_failed else config.EXIT_OK)


if __name__ == "__main__":
    main()
