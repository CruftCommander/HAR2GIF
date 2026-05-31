#!/usr/bin/env python3
"""Unit tests for convert.py — stdlib unittest, no ffmpeg required.

All ffmpeg invocations are mocked via ``convert.subprocess.run``. The keystone
mock (``make_ffmpeg_side_effect``) actually writes the expected output file so
the real atomic ``os.replace`` path is exercised.
"""

import argparse
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Make the repo root importable regardless of how the tests are launched.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config       # noqa: E402
import convert      # noqa: E402


def make_ffmpeg_side_effect(fail_pass=None):
    """Return (side_effect, calls). The side_effect writes the output file
    (cmd[-1]) for every passing call, so os.replace succeeds. ``fail_pass`` (1
    or 2) makes that pass return a non-zero exit with stderr set."""
    calls: list[list[str]] = []

    def _run(cmd, *a, **kw):
        calls.append(list(cmd))
        is_pass2 = cmd.count("-i") == 2
        if fail_pass == 1 and not is_pass2:
            return mock.Mock(returncode=1, stderr="palettegen boom", stdout="")
        if fail_pass == 2 and is_pass2:
            return mock.Mock(returncode=1, stderr="paletteuse boom", stdout="")
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"GIF89a-fake")
        return mock.Mock(returncode=0, stderr="", stdout="")

    return _run, calls


def make_interrupt_side_effect(interrupt_on_call):
    """Side effect that raises KeyboardInterrupt on the Nth call, writing output
    for all other calls."""
    state = {"n": 0}
    calls: list[list[str]] = []

    def _run(cmd, *a, **kw):
        state["n"] += 1
        calls.append(list(cmd))
        if state["n"] == interrupt_on_call:
            raise KeyboardInterrupt()
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"GIF89a-fake")
        return mock.Mock(returncode=0, stderr="", stdout="")

    return _run, calls


def _ns(**kw):
    return argparse.Namespace(**kw)


class ConvertTestBase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name)
        self.media = self.tmp / "media"
        self.media.mkdir()
        self.out = self.media
        self.cfg = config.ConvertConfig(quality="balanced", concurrency=1)
        # Silence logging setup so basicConfig side effects don't leak.
        sp = mock.patch("config.setup_logging")
        sp.start()
        self.addCleanup(sp.stop)

    def _make_src(self, name, content=b"src-bytes"):
        p = self.media / name
        p.write_bytes(content)
        return p

    def _fake_ffmpeg(self):
        f = self.tmp / ("ffmpeg" + config.EXE)
        if not f.exists():
            f.write_bytes(b"")
        return f

    def _events(self, path):
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _main(self, *extra):
        argv = ["convert.py", str(self.media), "--yes",
                "--ffmpeg-path", str(self._fake_ffmpeg()),
                "--concurrency", "1", *extra]
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                convert.main()
        self.last_stdout = buf.getvalue()
        return cm.exception.code


class TestConvertCore(ConvertTestBase):
    def test_ffmpeg_not_found(self):
        # subprocess raises FileNotFoundError -> ConvertResult.error is set.
        src = self._make_src("clip.mp4")
        dst = self.out / "clip.gif"
        with mock.patch("convert.subprocess.run", side_effect=FileNotFoundError("no ffmpeg")):
            result = convert.convert_to_gif(src, dst, self.cfg, Path("ffmpeg"))
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)
        self.assertIn("ffmpeg", result.error)

    def test_atomic_write_on_success(self):
        src = self._make_src("clip.mp4")
        dst = self.out / "clip.gif"
        run, _ = make_ffmpeg_side_effect()
        with mock.patch("convert.subprocess.run", side_effect=run):
            result = convert.convert_to_gif(src, dst, self.cfg, Path("ffmpeg"))
        self.assertTrue(result.success)
        self.assertTrue(dst.exists())
        self.assertEqual(dst.read_bytes(), b"GIF89a-fake")
        self.assertFalse(dst.with_suffix(".gif.tmp").exists())

    def test_palette_tmp_cleaned_on_failure(self):
        known_tmp = self.tmp / "palettedir"
        known_tmp.mkdir()
        src = self._make_src("clip.mp4")
        dst = self.out / "clip.gif"
        run, _ = make_ffmpeg_side_effect(fail_pass=2)
        with mock.patch("convert.tempfile.mkdtemp", return_value=str(known_tmp)), \
             mock.patch("convert.subprocess.run", side_effect=run):
            result = convert.convert_to_gif(src, dst, self.cfg, Path("ffmpeg"))
        self.assertFalse(result.success)
        self.assertEqual(result.ffmpeg_stderr, "paletteuse boom")
        self.assertFalse(known_tmp.exists())                      # palette tmp dir removed
        self.assertFalse(dst.with_suffix(".gif.tmp").exists())    # no orphan .tmp
        self.assertFalse(dst.exists())

    def test_filename_collision_in_originals(self):
        originals = self.tmp / "orig"
        originals.mkdir()
        (originals / "clip.mp4").write_bytes(b"existing")
        src = self._make_src("clip.mp4", content=b"new")
        moved = convert.move_original(src, originals)
        self.assertRegex(moved.name, r"^clip_[0-9a-f]{6}\.mp4$")
        self.assertEqual((originals / "clip.mp4").read_bytes(), b"existing")  # untouched
        self.assertEqual(moved.read_bytes(), b"new")
        self.assertFalse(src.exists())

    def test_quality_preset_max(self):
        p = convert.effective_settings(config.ConvertConfig(quality="max"))
        cmd1 = convert.build_pass1_cmd(Path("ffmpeg"), Path("a.mp4"), Path("pal.png"), p)
        vf = cmd1[cmd1.index("-vf") + 1]
        self.assertIn("palettegen=max_colors=256", vf)
        self.assertNotIn("fps=", vf)        # max => source fps, no cap
        self.assertNotIn("scale=", vf)      # max => preserve width
        cmd2 = convert.build_pass2_cmd(Path("ffmpeg"), Path("a.mp4"), Path("pal.png"),
                                       Path("o.gif.tmp"), p)
        lavfi = cmd2[cmd2.index("-lavfi") + 1]
        self.assertEqual(lavfi, "[0:v][1:v]paletteuse=dither=sierra2_4a")

    def test_quality_preset_small(self):
        p = convert.effective_settings(config.ConvertConfig(quality="small"))
        cmd1 = convert.build_pass1_cmd(Path("ffmpeg"), Path("a.mp4"), Path("pal.png"), p)
        vf = cmd1[cmd1.index("-vf") + 1]
        self.assertIn("max_colors=128", vf)
        self.assertIn("fps=15", vf)
        self.assertIn("scale=480", vf)
        cmd2 = convert.build_pass2_cmd(Path("ffmpeg"), Path("a.mp4"), Path("pal.png"),
                                       Path("o.gif.tmp"), p)
        lavfi = cmd2[cmd2.index("-lavfi") + 1]
        self.assertIn("fps=15", lavfi)
        self.assertIn("dither=bayer:bayer_scale=3", lavfi)


class TestConvertMain(ConvertTestBase):
    def test_dry_run_no_files_written(self):
        self._make_src("clip.mp4")

        def boom(*a, **k):
            self.fail("subprocess.run must not run during --dry-run")

        with mock.patch("convert.subprocess.run", side_effect=boom):
            code = self._main("--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse((self.media / "clip.gif").exists())
        self.assertFalse((self.media / "originals").exists())
        self.assertFalse((self.media / "convert.jsonl").exists())
        self.assertTrue((self.media / "clip.mp4").exists())

    def test_skip_existing_gif(self):
        self._make_src("clip.mp4")
        (self.media / "clip.gif").write_bytes(b"existing-gif")

        def boom(*a, **k):
            self.fail("subprocess.run must not run when output is skipped")

        with mock.patch("convert.subprocess.run", side_effect=boom):
            code = self._main()
        self.assertEqual(code, 0)
        self.assertTrue((self.media / "clip.mp4").exists())  # original not moved
        events = self._events(self.media / "convert.jsonl")
        self.assertTrue(any(e["event"] == "skipped" for e in events))

    def test_original_moved_on_success(self):
        self._make_src("clip.mp4")
        run, _ = make_ffmpeg_side_effect()
        with mock.patch("convert.subprocess.run", side_effect=run):
            code = self._main()
        self.assertEqual(code, 0)
        self.assertTrue((self.media / "clip.gif").exists())
        self.assertFalse((self.media / "clip.mp4").exists())
        self.assertTrue((self.media / "originals" / "clip.mp4").exists())
        events = self._events(self.media / "convert.jsonl")
        self.assertTrue(any(e["event"] == "converted" for e in events))
        self.assertTrue(any(e["event"] == "moved" for e in events))

    def test_original_not_moved_on_failure(self):
        self._make_src("clip.mp4")
        run, _ = make_ffmpeg_side_effect(fail_pass=2)
        with mock.patch("convert.subprocess.run", side_effect=run):
            code = self._main()
        self.assertEqual(code, 1)  # partial failure
        self.assertTrue((self.media / "clip.mp4").exists())
        self.assertFalse((self.media / "originals" / "clip.mp4").exists())
        failed = [e for e in self._events(self.media / "convert.jsonl") if e["event"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["stderr"], "paletteuse boom")

    def test_concurrent_interrupt(self):
        for name in ("a.mp4", "b.mp4", "c.mp4"):
            self._make_src(name)
        # concurrency=1 => deterministic order; interrupt on b's pass-1 (call #3).
        run, _ = make_interrupt_side_effect(interrupt_on_call=3)
        with mock.patch("convert.subprocess.run", side_effect=run):
            code = self._main()
        self.assertIn(code, (0, 1))                       # handled cleanly, no crash
        self.assertEqual(list(self.tmp.rglob("*.gif.tmp")), [])  # no orphan temp output
        self.assertIn("CONVERSION SUMMARY", self.last_stdout)


class TestConvertConfigLoading(ConvertTestBase):
    def test_config_missing_toml(self):
        self.assertEqual(config.load_toml(None), {})
        self.assertEqual(config.load_toml(self.tmp / "nope.toml"), {})
        cfg = config.build_convert_config({}, _ns())
        self.assertEqual(cfg, config.ConvertConfig())

    def test_config_malformed_toml(self):
        bad = self.tmp / "har2gif.toml"
        bad.write_text('quality = "max\n', encoding="utf-8")  # unterminated string
        with self.assertRaises(SystemExit) as cm:
            config.load_toml(bad)
        self.assertEqual(cm.exception.code, config.EXIT_FATAL)

        # Wrong type for an int field -> coercion failure is fatal.
        with self.assertRaises(SystemExit) as cm2:
            config.build_convert_config({"convert": {"fps_cap": "fast"}}, _ns())
        self.assertEqual(cm2.exception.code, config.EXIT_FATAL)

        # No TOML parser available, but a config file exists -> fatal (3.10 branch).
        good = self.tmp / "c2.toml"
        good.write_text("x = 1\n", encoding="utf-8")
        with mock.patch("config.tomllib", None):
            with self.assertRaises(SystemExit) as cm3:
                config.load_toml(good)
        self.assertEqual(cm3.exception.code, config.EXIT_FATAL)


if __name__ == "__main__":
    unittest.main()
