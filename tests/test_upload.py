#!/usr/bin/env python3
"""Unit tests for upload.py — stdlib unittest. No network, browser, viewer, or
clipboard is ever actually invoked (all are patched)."""

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Make the repo root importable regardless of how the tests are launched.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config       # noqa: E402
import upload       # noqa: E402


def _ns(**kw):
    kw.setdefault("api_key", None)
    kw.setdefault("auto_upload", None)
    kw.setdefault("skip_uploaded", None)
    kw.setdefault("manifest", None)
    return argparse.Namespace(**kw)


class UploadTestBase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name)
        self.gifs_dir = self.tmp / "gifs"
        self.gifs_dir.mkdir()
        self.manifest_path = self.gifs_dir / "klipy-manifest.json"
        sp = mock.patch("config.setup_logging")
        sp.start()
        self.addCleanup(sp.stop)

    def _make_gif(self, name, content=b"GIF89a-data"):
        p = self.gifs_dir / name
        p.write_bytes(content)
        return p

    def _events(self, path):
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _review(self, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return upload.review_queue(*args, **kwargs)


class TestTagSuggestion(unittest.TestCase):
    def test_tag_suggestion_from_filename(self):
        self.assertEqual(upload.suggest_tags("dancing-al-bundy.gif"), ["dancing", "al bundy"])
        self.assertEqual(upload.suggest_tags("wave.gif"), ["wave"])

    def test_tag_suggestion_strips_hash_suffix(self):
        self.assertEqual(upload.suggest_tags("dancing-al-bundy_b758c2.gif"),
                         ["dancing", "al bundy"])
        # A non-hex tail must be preserved (proves we only strip true hex suffixes).
        self.assertEqual(upload.suggest_tags("foo_zzzzzz.gif"), ["foo_zzzzzz"])


class TestManifest(UploadTestBase):
    def test_manifest_load_missing(self):
        data = upload.manifest_load(self.tmp / "nope.json")
        self.assertEqual(data, {"version": config.MANIFEST_VERSION, "entries": {}})

    def test_manifest_skip_already_uploaded(self):
        gif = self._make_gif("cat.gif")
        sha = upload.file_sha256(gif)
        manifest = {"version": 1, "entries": {sha: {"filename": "cat.gif", "tags": [], "published_ts": 0}}}
        cfg = config.UploadConfig(auto_upload=True)
        with mock.patch("upload.publish_assisted") as pub:
            results = self._review([gif], manifest, self.manifest_path, cfg,
                                   dry_run=False, skip_already=True, log_file=None)
        pub.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].action, "already")

    def test_manifest_atomic_write(self):
        original = {"version": 1, "entries": {"abc": {"filename": "x.gif", "tags": [], "published_ts": 0}}}
        self.manifest_path.write_text(json.dumps(original), encoding="utf-8")
        with mock.patch("upload.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                upload.manifest_save(self.manifest_path, {"version": 1, "entries": {"new": {}}})
        # Live file is uncorrupted (the failed write went to a .tmp).
        self.assertEqual(json.loads(self.manifest_path.read_text(encoding="utf-8")), original)


class TestApiKeyPrecedence(UploadTestBase):
    def test_api_key_env_var(self):
        with mock.patch.dict(os.environ, {"KLIPY_API_KEY": "envkey"}, clear=False):
            self.assertEqual(config.build_upload_config({}, _ns()).klipy_api_key, "envkey")
            # CLI overrides env.
            self.assertEqual(config.build_upload_config({}, _ns(api_key="clikey")).klipy_api_key, "clikey")
            # env overrides toml.
            self.assertEqual(
                config.build_upload_config({"upload": {"klipy_api_key": "tomlkey"}}, _ns()).klipy_api_key,
                "envkey")
        with mock.patch.dict(os.environ, {}, clear=True):
            # toml only (no env, no CLI).
            self.assertEqual(
                config.build_upload_config({"upload": {"klipy_api_key": "tomlkey"}}, _ns()).klipy_api_key,
                "tomlkey")


class TestReviewQueue(UploadTestBase):
    def test_dry_run_no_http_calls(self):
        gif = self._make_gif("cat.gif")
        cfg = config.UploadConfig(auto_upload=True)
        log_file = self.tmp / "up.jsonl"
        with mock.patch("upload.open_in_viewer") as viewer, \
             mock.patch("upload.copy_to_clipboard") as clip, \
             mock.patch("upload.open_klipy") as browser, \
             mock.patch("upload.manifest_save") as saver:
            results = self._review([gif], {"version": 1, "entries": {}}, self.manifest_path, cfg,
                                   dry_run=True, skip_already=False, log_file=log_file)
        viewer.assert_not_called()
        clip.assert_not_called()
        browser.assert_not_called()
        saver.assert_not_called()
        published = [e for e in self._events(log_file) if e["event"] == "published"]
        self.assertEqual(len(published), 1)
        self.assertTrue(published[0]["dry_run"])
        self.assertEqual(results[0].action, "published")

    def test_auto_upload_flag_skips_confirmation(self):
        gif = self._make_gif("cat.gif")
        cfg = config.UploadConfig(auto_upload=True)
        with mock.patch("builtins.input", side_effect=AssertionError("input must not be called")), \
             mock.patch("upload.publish_assisted"):
            results = self._review([gif], {"version": 1, "entries": {}}, self.manifest_path, cfg,
                                   dry_run=False, skip_already=False, log_file=None)
        self.assertEqual(results[0].action, "published")

    def test_upload_logs_on_success(self):
        gif = self._make_gif("dancing-al-bundy.gif")
        sha = upload.file_sha256(gif)
        cfg = config.UploadConfig(auto_upload=True)
        log_file = self.tmp / "up.jsonl"
        manifest = {"version": 1, "entries": {}}
        with mock.patch("upload.open_in_viewer"), \
             mock.patch("upload.copy_to_clipboard", return_value=True), \
             mock.patch("upload.open_klipy"):
            self._review([gif], manifest, self.manifest_path, cfg,
                         dry_run=False, skip_already=False, log_file=log_file)
        published = [e for e in self._events(log_file) if e["event"] == "published"]
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["sha256"], sha)
        self.assertEqual(published[0]["tags"], ["dancing", "al bundy"])
        self.assertIn(sha, manifest["entries"])
        saved = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertIn(sha, saved["entries"])

    def test_upload_logs_on_failure(self):
        gif = self._make_gif("cat.gif")
        cfg = config.UploadConfig(auto_upload=True)
        log_file = self.tmp / "up.jsonl"
        self.manifest_path.write_text(json.dumps({"version": 1, "entries": {}}), encoding="utf-8")
        manifest = upload.manifest_load(self.manifest_path)
        with mock.patch("upload.open_in_viewer", side_effect=OSError("no viewer")), \
             mock.patch("upload.copy_to_clipboard", return_value=True), \
             mock.patch("upload.open_klipy"):
            results = self._review([gif], manifest, self.manifest_path, cfg,
                                   dry_run=False, skip_already=False, log_file=log_file)
        self.assertEqual(results[0].action, "failed")
        failed = [e for e in self._events(log_file) if e["event"] == "failed"]
        self.assertEqual(len(failed), 1)
        # Manifest never recorded the failed file.
        saved = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["entries"], {})


if __name__ == "__main__":
    unittest.main()
