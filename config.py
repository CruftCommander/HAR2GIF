#!/usr/bin/env python3
"""
config.py
---------
Shared configuration and infrastructure for the HAR2GIF tools (`convert.py`
and `upload.py`). This is the ONLY module the two scripts share — they must
never import each other.

Responsibilities:
    - Load an optional ``har2gif.toml`` (stdlib ``tomllib``; ``tomli`` fallback
      for Python 3.10) and merge it with environment variables and CLI flags.
    - Provide typed config dataclasses with explicit fallbacks (never silent None).
    - Provide thread-safe structured logging (``emit_jsonl``) and ``setup_logging``.
    - Provide a filename-collision helper and a binary-discovery helper.

Configuration precedence (lowest to highest):
    dataclass defaults  <  har2gif.toml  <  environment variable  <  CLI flag

The config file is optional: absent -> defaults apply silently. Present but
malformed -> fail fast with a clear message and exit code 2.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import os
import shutil
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import tomllib  # stdlib, Python 3.11+
except ImportError:  # pragma: no cover - dead-but-portable for Python 3.10
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:  # tomli not installed either
        tomllib = None  # type: ignore  # checked in load_toml -> clear error

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_FILENAME = "har2gif.toml"

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_FATAL = 2

# Default source extensions for convert.py (no leading dot).
DEFAULT_EXTENSIONS = ("mp4", "webm", "webp")

# Valid GIF quality presets (the per-preset values live in convert.py).
QUALITY_NAMES = ("max", "balanced", "small")
DEFAULT_QUALITY = "balanced"  # sane caps; avoids gigantic GIFs across a whole dir

# Klipy has no public upload API; this is the web "GIF maker" the assisted
# publish flow opens for the user.
KLIPY_CREATE_URL = "https://klipy.com/create/gif-maker"

MANIFEST_VERSION = 1

# Executable suffix for bundled binaries (bin/ffmpeg.exe on Windows).
EXE = ".exe" if sys.platform == "win32" else ""


# ---------------------------------------------------------------------------
# Config dataclasses (frozen; explicit fallbacks, never silent None)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConvertConfig:
    """Resolved configuration for convert.py.

    The three numeric fields are ``None`` when not explicitly configured, which
    means "use the quality preset's value". convert.py resolves them to concrete
    values via ``effective_settings`` — so the *effective* settings are never None.
    """
    quality: str = DEFAULT_QUALITY
    fps_cap: Optional[int] = None       # None -> use preset fps
    scale_width: Optional[int] = None   # None -> use preset width; -1 -> preserve source
    palette_size: Optional[int] = None  # None -> use preset palette size
    originals_dir: str = "originals"
    concurrency: int = 4


@dataclass(frozen=True)
class UploadConfig:
    """Resolved configuration for upload.py."""
    klipy_api_key: str = ""
    auto_upload: bool = False
    skip_uploaded: bool = True
    manifest_path: str = "klipy-manifest.json"


# ---------------------------------------------------------------------------
# Fatal-error helper
# ---------------------------------------------------------------------------

def _fatal(message: str) -> "NoReturn":  # type: ignore[name-defined]
    """Log a critical message and exit with the fatal exit code."""
    log.critical("%s", message)
    sys.exit(EXIT_FATAL)


# ---------------------------------------------------------------------------
# TOML loading
# ---------------------------------------------------------------------------

def find_config(explicit: Optional[Path]) -> Optional[Path]:
    """Locate the config file.

    Explicit ``--config`` wins (and must exist); otherwise use
    ``./har2gif.toml`` if present; otherwise None (defaults apply).
    """
    if explicit is not None:
        if not explicit.exists():
            _fatal(f"Config file not found: {explicit}")
        return explicit
    candidate = Path.cwd() / CONFIG_FILENAME
    return candidate if candidate.exists() else None


def load_toml(path: Optional[Path]) -> dict:
    """Load and return the parsed TOML, or ``{}`` if there is no config.

    A missing/absent config is fine (silent defaults). A present-but-malformed
    config, or a missing TOML parser on Python 3.10, is fatal (exit code 2).
    """
    if path is None or not path.exists():
        return {}
    if tomllib is None:
        _fatal(
            f"Config file {path} found, but no TOML parser is available. "
            "On Python 3.10, install the 'tomli' package (pip install tomli), "
            "or upgrade to Python 3.11+."
        )
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:  # type: ignore[union-attr]
        _fatal(f"Malformed config file {path}: {exc}")
    except OSError as exc:
        _fatal(f"Cannot read config file {path}: {exc}")


# ---------------------------------------------------------------------------
# Merge: defaults < toml table < env < CLI
# ---------------------------------------------------------------------------

def _coerce(value, name: str, section: str, int_fields: set, bool_fields: set):
    """Coerce a raw config value to the field's expected type, or fail fast."""
    if name in int_fields:
        try:
            return int(value)
        except (TypeError, ValueError):
            _fatal(f"{section}: '{name}' must be an integer, got {value!r}")
    if name in bool_fields:
        if isinstance(value, bool):
            return value
        _fatal(f"{section}: '{name}' must be true or false, got {value!r}")
    return str(value)


def _merge_config(cls, table: dict, env: dict, cli: dict,
                  section: str, int_fields: set, bool_fields: set):
    """Build a config dataclass by applying precedence default < toml < env < cli.

    - ``table``: the relevant ``[section]`` sub-table from the parsed TOML.
    - ``env``: field-name -> environment value (or None); applied if non-empty.
    - ``cli``: field-name -> CLI value (already typed by argparse); applied if
      not None (None is the "flag was not supplied" sentinel).
    """
    if not isinstance(table, dict):
        _fatal(f"[{section}] in {CONFIG_FILENAME} must be a table")

    defaults = cls()
    resolved = {}
    for f in dataclasses.fields(cls):
        name = f.name
        value = getattr(defaults, name)
        if name in table:
            value = _coerce(table[name], name, f"{CONFIG_FILENAME} [{section}]",
                            int_fields, bool_fields)
        env_val = env.get(name)
        if env_val is not None and env_val != "":
            value = _coerce(env_val, name, f"environment ({name})",
                            int_fields, bool_fields)
        cli_val = cli.get(name)
        if cli_val is not None:
            value = cli_val  # argparse already produced the right type
        resolved[name] = value
    return cls(**resolved)


def build_convert_config(raw: dict, args: argparse.Namespace) -> ConvertConfig:
    """Resolve a ConvertConfig from the parsed TOML and CLI args."""
    cfg = _merge_config(
        ConvertConfig,
        table=raw.get("convert", {}),
        env={},
        cli={
            "quality": getattr(args, "quality", None),
            "fps_cap": getattr(args, "fps", None),
            "originals_dir": getattr(args, "originals_dir", None),
            "concurrency": getattr(args, "concurrency", None),
            # scale_width / palette_size have no CLI flag; TOML-only overrides.
        },
        section="convert",
        int_fields={"fps_cap", "scale_width", "palette_size", "concurrency"},
        bool_fields=set(),
    )
    if cfg.quality not in QUALITY_NAMES:
        _fatal(f"convert: 'quality' must be one of {QUALITY_NAMES}, got {cfg.quality!r}")
    return cfg


def build_upload_config(raw: dict, args: argparse.Namespace) -> UploadConfig:
    """Resolve an UploadConfig from the parsed TOML, env, and CLI args.

    The Klipy API key may come from the KLIPY_API_KEY environment variable.
    ``--no-skip-uploaded`` forces ``skip_uploaded=False``; ``--auto-upload``
    forces ``auto_upload=True``. Both default to None ("not supplied") on the CLI.
    """
    return _merge_config(
        UploadConfig,
        table=raw.get("upload", {}),
        env={"klipy_api_key": os.environ.get("KLIPY_API_KEY")},
        cli={
            "klipy_api_key": getattr(args, "api_key", None),
            "auto_upload": getattr(args, "auto_upload", None),
            "skip_uploaded": getattr(args, "skip_uploaded", None),
            "manifest_path": (str(args.manifest) if getattr(args, "manifest", None) else None),
        },
        section="upload",
        int_fields=set(),
        bool_fields={"auto_upload", "skip_uploaded"},
    )


# ---------------------------------------------------------------------------
# Structured logging (thread-safe) and console logging
# ---------------------------------------------------------------------------

_jsonl_lock = threading.Lock()


def emit_jsonl(log_file: Optional[Path], record: dict) -> None:
    """Append one JSON object (one line) to ``log_file``. Thread-safe.

    The JSON is serialised outside the lock; only the append is guarded, so
    concurrent worker threads never interleave partial lines.
    """
    if not log_file:
        return
    line = json.dumps(record) + "\n"
    with _jsonl_lock:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line)


def setup_logging(log_file: Optional[Path], verbose: bool) -> None:
    """Configure console (+ optional file) logging. Mirrors main.py's setup."""
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


# ---------------------------------------------------------------------------
# Filename collision handling (mirrors main.py's 6-char-hash idiom)
# ---------------------------------------------------------------------------

def collision_free_move_target(dest_dir: Path, filename: str) -> Path:
    """Return a non-colliding path inside ``dest_dir`` for ``filename``.

    If ``dest_dir/filename`` already exists, a 6-char hash suffix is appended
    before the extension (same shape as main.py's ``resolve_output_path``).
    """
    target = dest_dir / filename
    if not target.exists():
        return target
    stem = Path(filename).stem
    ext = Path(filename).suffix
    counter = 0
    while True:
        short_hash = hashlib.md5(f"{filename}{counter}".encode()).hexdigest()[:6]
        candidate = dest_dir / f"{stem}_{short_hash}{ext}"
        if not candidate.exists():
            return candidate
        counter += 1


# ---------------------------------------------------------------------------
# Binary discovery (ffmpeg / chafa)
# ---------------------------------------------------------------------------

class BinaryNotFound(Exception):
    """Raised when a required external binary cannot be located."""


def discover_binary(name: str, explicit: Optional[Path], script_dir: Path) -> Path:
    """Locate an external binary.

    Order: explicit path (must exist) -> system PATH -> bundled
    ``<script_dir>/bin/<name><EXE>``. Raises ``BinaryNotFound`` if none found.

    ``script_dir`` should be ``Path(__file__).resolve().parent`` of the calling
    script so the bundled path is relative to the script, never the CWD.
    """
    if explicit is not None:
        p = Path(explicit)
        if p.exists():
            return p
        raise BinaryNotFound(f"Specified {name} path does not exist: {explicit}")
    found = shutil.which(name)
    if found:
        return Path(found)
    bundled = script_dir / "bin" / (name + EXE)
    if bundled.exists():
        return bundled
    raise BinaryNotFound(name)
