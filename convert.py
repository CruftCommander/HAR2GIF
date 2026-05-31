#!/usr/bin/env python3
"""
convert.py
----------
Convert already-downloaded non-GIF media (.mp4 / .webm / .webp) to GIF using a
two-pass ffmpeg palettegen->paletteuse pipeline, move the originals aside, and
write a structured JSON Lines conversion log.

This operates ONLY on files already on disk (typically the output of main.py).
It never re-invokes main.py and never touches HAR files.

Usage:
    python convert.py <media_dir> [options]

Options (see --help for the full list):
    --quality max|balanced|small   GIF quality preset (overrides config)
    --fps N                        Cap frames per second (overrides preset)
    --concurrency N                Parallel ffmpeg workers
    --dry-run                      Print the plan; convert nothing, move nothing
    --yes                          Skip the confirmation prompt
    --extensions mp4,webm,webp     Source extensions to process
    --ffmpeg-path PATH             Explicit ffmpeg binary (else PATH, else ./bin)

Exit codes:
    0  All files converted or skipped successfully (or dry-run)
    1  Partial failure (some conversions failed)
    2  Fatal error (ffmpeg not found, bad config, bad directory)

----------------------------------------------------------------------------
Phase 0 findings baked into this file (full notes in CLAUDE.md):
  * 0A platform audit: every path here goes through pathlib; the atomic write
    uses os.replace (overwrites cross-platform, unlike Path.rename on Windows);
    ThreadPoolExecutor and subprocess are platform-safe. No Windows-only calls.
  * 0B ffmpeg integration: the binary is invoked directly via `subprocess`
    (NOT the `ffmpeg-python` pip wrapper). Rationale: zero pip dependencies,
    maximum portability, and consistency with the project's stdlib-only stance.
----------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import config

log = logging.getLogger("convert")

SCRIPT_DIR = Path(__file__).resolve().parent

# Max chars of ffmpeg stderr to keep in a failure log record (keeps logs sane).
STDERR_LOG_LIMIT = 4000

FFMPEG_INSTALL_MESSAGE = """\
ERROR: ffmpeg not found.

To install ffmpeg:
  Windows (recommended): Place ffmpeg.exe in ./bin/ffmpeg.exe
                         OR run: winget install ffmpeg
                         OR run: choco install ffmpeg
  Linux/macOS:           sudo apt install ffmpeg
                         OR brew install ffmpeg

For manual download: https://www.gyan.dev/ffmpeg/builds/"""


# ---------------------------------------------------------------------------
# Quality presets (named constants, not magic numbers)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Preset:
    palette_size: int
    fps: Optional[int]   # None = source fps (no cap)
    scale_width: int     # -1 = preserve source width
    dither: str


PRESETS: dict[str, Preset] = {
    "max":      Preset(palette_size=256, fps=None, scale_width=-1,  dither="sierra2_4a"),
    "balanced": Preset(palette_size=256, fps=24,   scale_width=640, dither="floyd_steinberg"),
    "small":    Preset(palette_size=128, fps=15,   scale_width=480, dither="bayer:bayer_scale=3"),
}


def effective_settings(cfg: config.ConvertConfig) -> Preset:
    """Resolve the concrete render settings: preset as base, explicit config
    fields override their preset counterpart. Result is always fully concrete."""
    base = PRESETS[cfg.quality]
    return Preset(
        palette_size=cfg.palette_size if cfg.palette_size is not None else base.palette_size,
        fps=cfg.fps_cap if cfg.fps_cap is not None else base.fps,
        scale_width=cfg.scale_width if cfg.scale_width is not None else base.scale_width,
        dither=base.dither,
    )


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ConvertResult:
    src: Path
    dst: Optional[Path] = None
    success: bool = False
    skipped: bool = False
    skip_reason: Optional[str] = None
    moved_original_to: Optional[Path] = None
    error: Optional[str] = None
    ffmpeg_stderr: Optional[str] = None


# ---------------------------------------------------------------------------
# ffmpeg filter / command construction
# ---------------------------------------------------------------------------

def _fps_token(fps: Optional[int]) -> Optional[str]:
    return None if fps is None else f"fps={fps}"


def _scale_token(width: int) -> Optional[str]:
    return None if width == -1 else f"scale={width}:-1:flags=lanczos"


def _vf_pass1(p: Preset) -> str:
    """Pass-1 -vf string: optional fps/scale, then palettegen."""
    parts = [t for t in (_fps_token(p.fps), _scale_token(p.scale_width)) if t]
    parts.append(f"palettegen=max_colors={p.palette_size}:stats_mode=full")
    return ",".join(parts)


def _lavfi_pass2(p: Preset) -> str:
    """Pass-2 -lavfi string: optional fps/scale pre-chain, then paletteuse."""
    pre = ",".join(t for t in (_fps_token(p.fps), _scale_token(p.scale_width)) if t)
    if pre:
        return f"{pre}[x];[x][1:v]paletteuse=dither={p.dither}"
    return f"[0:v][1:v]paletteuse=dither={p.dither}"


def build_pass1_cmd(ffmpeg: Path, src: Path, palette_tmp: Path, p: Preset) -> list[str]:
    """Pass 1 — generate the colour palette. Output path is the last element."""
    return [str(ffmpeg), "-y", "-i", str(src), "-vf", _vf_pass1(p), str(palette_tmp)]


def build_pass2_cmd(ffmpeg: Path, src: Path, palette_tmp: Path, dst_tmp: Path, p: Preset) -> list[str]:
    """Pass 2 — render the GIF using the palette. Has two -i inputs."""
    return [str(ffmpeg), "-y", "-i", str(src), "-i", str(palette_tmp),
            "-lavfi", _lavfi_pass2(p), str(dst_tmp)]


# ---------------------------------------------------------------------------
# Core conversion (pure: no original-moving, no logging — easy to unit test)
# ---------------------------------------------------------------------------

def convert_to_gif(src: Path, dst: Path, cfg: config.ConvertConfig, ffmpeg: Path) -> ConvertResult:
    """Two-pass conversion of one file. Writes atomically to ``dst``.

    The palette temp dir and the ``.gif.tmp`` output are always cleaned up in
    the ``finally`` block — on success, failure, or KeyboardInterrupt — so no
    partial artifacts are ever left behind.
    """
    p = effective_settings(cfg)
    tmpdir = Path(tempfile.mkdtemp(prefix="har2gif_"))
    palette_tmp = tmpdir / "palette.png"
    dst_tmp = dst.with_suffix(".gif.tmp")
    try:
        r1 = subprocess.run(build_pass1_cmd(ffmpeg, src, palette_tmp, p),
                            capture_output=True, text=True)
        if r1.returncode != 0:
            return ConvertResult(src=src, dst=dst, error="palettegen (pass 1) failed",
                                 ffmpeg_stderr=r1.stderr)

        r2 = subprocess.run(build_pass2_cmd(ffmpeg, src, palette_tmp, dst_tmp, p),
                            capture_output=True, text=True)
        if r2.returncode != 0:
            return ConvertResult(src=src, dst=dst, error="paletteuse (pass 2) failed",
                                 ffmpeg_stderr=r2.stderr)

        if not dst_tmp.exists():
            return ConvertResult(src=src, dst=dst, error="ffmpeg produced no output",
                                 ffmpeg_stderr=r2.stderr)

        os.replace(dst_tmp, dst)  # atomic; overwrites an existing dst on Windows too
        return ConvertResult(src=src, dst=dst, success=True)

    except FileNotFoundError as exc:
        # ffmpeg binary disappeared / not executable between discovery and exec.
        return ConvertResult(src=src, dst=dst, error=f"ffmpeg not executable: {exc}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        if dst_tmp.exists():
            try:
                dst_tmp.unlink()
            except OSError:
                pass


def move_original(src: Path, originals_dir: Path) -> Path:
    """Move a successfully-converted original into ``originals_dir`` (created if
    missing), disambiguating filename collisions with a 6-char hash suffix."""
    originals_dir.mkdir(parents=True, exist_ok=True)
    target = config.collision_free_move_target(originals_dir, src.name)
    shutil.move(str(src), str(target))
    return target


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def enumerate_sources(source_dir: Path, extensions) -> list[Path]:
    """Return sorted source files in ``source_dir`` matching ``extensions``.

    Non-recursive, so an ``originals/`` subdirectory is naturally excluded.
    """
    exts = {"." + e.lower().lstrip(".") for e in extensions}
    return sorted(
        entry for entry in source_dir.iterdir()
        if entry.is_file() and entry.suffix.lower() in exts
    )


def plan_outputs(files: list[Path], output_dir: Path, skip_existing: bool):
    """Split sources into (to_convert, skipped) lists of (src, dst) tuples."""
    to_convert: list[tuple[Path, Path]] = []
    skipped: list[tuple[Path, Path]] = []
    for src in files:
        dst = output_dir / (src.stem + ".gif")
        if skip_existing and dst.exists():
            skipped.append((src, dst))
        else:
            to_convert.append((src, dst))
    return to_convert, skipped


# ---------------------------------------------------------------------------
# JSONL records
# ---------------------------------------------------------------------------

def _rec_converted(r: ConvertResult, p: Preset) -> dict:
    return {"event": "converted", "src": str(r.src), "dst": str(r.dst),
            "quality_palette": p.palette_size, "fps": p.fps, "ts": time.time()}


def _rec_moved(r: ConvertResult) -> dict:
    return {"event": "moved", "src": str(r.src), "to": str(r.moved_original_to),
            "ts": time.time()}


def _rec_failed(r: ConvertResult) -> dict:
    stderr = (r.ffmpeg_stderr or "")[:STDERR_LOG_LIMIT]
    return {"event": "failed", "src": str(r.src), "dst": str(r.dst) if r.dst else None,
            "error": r.error, "stderr": stderr, "ts": time.time()}


def _rec_skipped(src: Path, dst: Path, reason: str) -> dict:
    return {"event": "skipped", "src": str(src), "dst": str(dst),
            "reason": reason, "ts": time.time()}


# ---------------------------------------------------------------------------
# Batch execution
# ---------------------------------------------------------------------------

def run_batch(plan, cfg: config.ConvertConfig, ffmpeg: Path,
              originals_dir: Path, log_file: Optional[Path]):
    """Convert all planned (src, dst) pairs concurrently.

    Returns ``(results, interrupted)``. On KeyboardInterrupt, pending work is
    cancelled, in-flight conversions are allowed to finish (each cleans its own
    temp files), and we return what completed so far.
    """
    p = effective_settings(cfg)
    results: list[ConvertResult] = []
    total = len(plan)
    completed = 0
    interrupted = False

    with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        futures = {pool.submit(convert_to_gif, src, dst, cfg, ffmpeg): (src, dst)
                   for src, dst in plan}
        try:
            for future in as_completed(futures):
                result = future.result()
                completed += 1
                if result.success:
                    try:
                        result.moved_original_to = move_original(result.src, originals_dir)
                    except OSError as exc:
                        log.warning("Converted but could not move original %s: %s",
                                    result.src.name, exc)
                    log.info("[%4d/%d] OK    %s -> %s", completed, total,
                             result.src.name, result.dst.name)
                    config.emit_jsonl(log_file, _rec_converted(result, p))
                    if result.moved_original_to is not None:
                        config.emit_jsonl(log_file, _rec_moved(result))
                else:
                    log.warning("[%4d/%d] FAIL  %s -- %s", completed, total,
                                result.src.name, result.error)
                    config.emit_jsonl(log_file, _rec_failed(result))
                results.append(result)
        except KeyboardInterrupt:
            interrupted = True
            log.warning("Interrupted -- cancelling pending conversions, "
                        "finishing in-flight work...")
            for fut in futures:
                fut.cancel()
            # Exiting the `with` joins running workers; their `finally` clauses
            # remove any palette temp dir and `.gif.tmp`, so nothing is orphaned.

    return results, interrupted


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_summary(results: list[ConvertResult], skipped_count: int, interrupted: bool) -> None:
    converted = sum(1 for r in results if r.success)
    failed = sum(1 for r in results if not r.success)
    print("\n" + "=" * 60)
    print("  CONVERSION SUMMARY" + ("  (INTERRUPTED)" if interrupted else ""))
    print("=" * 60)
    print(f"  Converted       : {converted}")
    print(f"  Skipped (exist) : {skipped_count}")
    print(f"  Failed          : {failed}")
    if failed:
        print("\n  Failed files:")
        for r in results:
            if not r.success:
                print(f"    [{r.error}]  {r.src.name}")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Convert downloaded MP4/WebM/WebP media to GIF via ffmpeg.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("media_dir", type=Path,
                   help="Directory containing the downloaded media files")
    p.add_argument("--in-place", action="store_true",
                   help="Process files already in <media_dir> (default behaviour)")
    p.add_argument("--source-dir", type=Path, default=None,
                   help="Explicit source dir (overrides in-place behaviour)")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="Where GIFs are written (default: <media_dir>)")
    p.add_argument("--originals-dir", type=str, default=None,
                   help="Where originals move to (default: <media_dir>/originals)")
    p.add_argument("--quality", choices=config.QUALITY_NAMES, default=None,
                   help="GIF quality preset (overrides config)")
    p.add_argument("--fps", type=int, default=None, metavar="N",
                   help="Cap frames per second (overrides preset)")
    p.add_argument("--concurrency", type=int, default=None, metavar="N",
                   help="Parallel ffmpeg workers")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan; convert nothing, move nothing")
    p.add_argument("--log-file", type=Path, default=None, metavar="PATH",
                   help="JSONL conversion log (default: convert.jsonl in output dir)")
    p.add_argument("--extensions", type=str, default=None, metavar="ext,ext",
                   help="Comma-separated source extensions (default: mp4,webm,webp)")
    p.add_argument("--skip-existing", dest="skip_existing", action="store_const",
                   const=True, default=None, help="Skip if output GIF exists (default: on)")
    p.add_argument("--no-skip-existing", dest="skip_existing", action="store_const",
                   const=False, help="Re-convert even if the GIF exists")
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    p.add_argument("--ffmpeg-path", type=Path, default=None, metavar="PATH",
                   help="Explicit path to the ffmpeg binary")
    p.add_argument("--config", type=Path, default=None, metavar="PATH",
                   help=f"Path to {config.CONFIG_FILENAME} (default: ./{config.CONFIG_FILENAME})")
    p.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return p


def main() -> None:
    args = build_parser().parse_args()
    config.setup_logging(None, args.verbose)  # console only; --log-file is JSONL-only

    raw = config.load_toml(config.find_config(args.config))
    cfg = config.build_convert_config(raw, args)

    media_dir: Path = args.media_dir
    if not media_dir.is_dir():
        log.critical("Media directory not found: %s", media_dir)
        sys.exit(config.EXIT_FATAL)

    source_dir = args.source_dir if args.source_dir else media_dir
    output_dir = args.output_dir if args.output_dir else media_dir
    originals_path = Path(cfg.originals_dir)
    if not originals_path.is_absolute():
        originals_path = media_dir / originals_path

    extensions = (
        [e for e in args.extensions.split(",") if e.strip()]
        if args.extensions else list(config.DEFAULT_EXTENSIONS)
    )
    skip_existing = True if args.skip_existing is None else args.skip_existing
    log_file = args.log_file if args.log_file else (output_dir / "convert.jsonl")

    # Locate ffmpeg (warn-only under --dry-run so the plan can still be shown).
    try:
        ffmpeg = config.discover_binary("ffmpeg", args.ffmpeg_path, SCRIPT_DIR)
    except config.BinaryNotFound:
        if args.dry_run:
            ffmpeg = None
            log.warning("ffmpeg not found -- continuing because --dry-run "
                        "(no conversion will run).")
        else:
            print(FFMPEG_INSTALL_MESSAGE, file=sys.stderr)
            sys.exit(config.EXIT_FATAL)

    if not source_dir.is_dir():
        log.critical("Source directory not found: %s", source_dir)
        sys.exit(config.EXIT_FATAL)

    files = enumerate_sources(source_dir, extensions)
    to_convert, skipped = plan_outputs(files, output_dir, skip_existing)

    p = effective_settings(cfg)
    print("\nConfiguration:")
    print(f"  Quality preset  : {cfg.quality} "
          f"(palette={p.palette_size}, fps={p.fps if p.fps is not None else 'source'}, "
          f"width={'source' if p.scale_width == -1 else p.scale_width}, dither={p.dither})")
    print(f"  Source dir      : {source_dir}")
    print(f"  Output dir      : {output_dir}")
    print(f"  Originals dir   : {originals_path}")
    print(f"  Extensions      : {', '.join(extensions)}")
    print(f"  Concurrency     : {cfg.concurrency}")
    print(f"  Skip existing   : {skip_existing}")
    print(f"  Dry run         : {args.dry_run}")
    print(f"  Found           : {len(files)} source file(s); "
          f"{len(to_convert)} to convert, {len(skipped)} to skip\n")

    if not args.dry_run:
        for src, dst in skipped:
            config.emit_jsonl(log_file, _rec_skipped(src, dst, "exists"))

    if not to_convert:
        log.info("Nothing to convert.")
        sys.exit(config.EXIT_OK)

    if args.dry_run:
        print(f"{'SOURCE':<45}  OUTPUT")
        print("-" * 90)
        for src, dst in to_convert:
            print(f"{src.name:<45}  {dst.name}")
        print(f"\nTotal: {len(to_convert)} file(s) would be converted "
              f"(dry run - nothing written, nothing moved).")
        sys.exit(config.EXIT_OK)

    if not args.yes:
        try:
            resp = input(f"Ready to convert {len(to_convert)} files. Proceed? [y/N] ")
        except EOFError:
            resp = ""
        if resp.strip().lower() not in ("y", "yes"):
            log.info("Aborted by user.")
            sys.exit(config.EXIT_OK)

    output_dir.mkdir(parents=True, exist_ok=True)
    log.info("Converting %d file(s) to %s (concurrency=%d)",
             len(to_convert), output_dir, cfg.concurrency)

    results, interrupted = run_batch(to_convert, cfg, ffmpeg, originals_path, log_file)
    print_summary(results, len(skipped), interrupted)

    any_failures = any(not r.success for r in results)
    sys.exit(config.EXIT_PARTIAL if any_failures else config.EXIT_OK)


if __name__ == "__main__":
    main()
