"""Regression tests for editable presenter-host configuration."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from autopollev.config import Config, ConfigError, DEFAULT_CONFIG, app_dir


class UpdateHostTests(unittest.TestCase):
    def test_first_run_generates_default_host(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with self.assertRaises(ConfigError):
                Config(path)
            self.assertEqual(Config(path).host, "your-presenter-id")

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_path = os.path.join(self.temp_dir.name, "config.json")
        with open(self.config_path, "w", encoding="utf-8") as config_file:
            json.dump(DEFAULT_CONFIG, config_file)
        self.config = Config(self.config_path)

    def test_trims_and_persists_valid_host(self):
        self.assertTrue(self.config.update_host("  new-host_2  "))
        self.assertEqual("new-host_2", self.config.host)

        with open(self.config_path, encoding="utf-8") as config_file:
            self.assertEqual("new-host_2", json.load(config_file)["host"])

    def test_unchanged_host_does_not_save(self):
        with patch.object(
            self.config, "save", side_effect=AssertionError("unexpected save")
        ):
            self.assertFalse(
                self.config.update_host(f"  {self.config.host}  ")
            )

    def test_rejects_invalid_host_without_changing_config(self):
        invalid_hosts = (
            "",
            "   ",
            "two hosts",
            "https://pollev.com/presenter",
            "pollev.com/presenter",
            "www.pollev.com",
            "presenter/name",
            "presenter\\name",
            "presenter:name",
            "presenter?poll=1",
            "presenter#poll",
        )

        for invalid_host in invalid_hosts:
            with self.subTest(host=invalid_host):
                with self.assertRaises(ConfigError):
                    self.config.update_host(invalid_host)
                self.assertEqual(DEFAULT_CONFIG["host"], self.config.host)

        with open(self.config_path, encoding="utf-8") as config_file:
            self.assertEqual(
                DEFAULT_CONFIG["host"], json.load(config_file)["host"]
            )

    def test_restores_host_when_save_fails(self):
        with patch.object(
            self.config, "save", side_effect=OSError("disk unavailable")
        ):
            with self.assertRaises(OSError):
                self.config.update_host("new-presenter")

        self.assertEqual(DEFAULT_CONFIG["host"], self.config.host)

        with open(self.config_path, encoding="utf-8") as config_file:
            self.assertEqual(
                DEFAULT_CONFIG["host"], json.load(config_file)["host"]
            )


class AppDirTests(unittest.TestCase):
    """Packaged builds must keep their data next to the .exe, not in _MEIPASS."""

    def test_frozen_build_anchors_on_the_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = os.path.join(directory, "AutoPollEv.exe")
            with patch.object(sys, "frozen", True, create=True), patch.object(
                sys, "executable", executable
            ):
                self.assertEqual(directory, app_dir())

    def test_relative_log_dir_resolves_under_app_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w", encoding="utf-8") as config_file:
                json.dump(DEFAULT_CONFIG, config_file)
            self.assertEqual(os.path.join(app_dir(), "logs"), Config(path).log_dir)

    def test_absolute_log_dir_is_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w", encoding="utf-8") as config_file:
                json.dump({**DEFAULT_CONFIG, "log_dir": directory}, config_file)
            self.assertEqual(directory, Config(path).log_dir)


class MalformedConfigTests(unittest.TestCase):
    """A bad config file must arrive as ConfigError, never as a raw traceback."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = os.path.join(self.temp_dir.name, "config.json")

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_invalid_json_raises_config_error(self):
        self._write('{"host": "x", ')
        with self.assertRaises(ConfigError) as caught:
            Config(self.path)
        self.assertIn("not valid JSON", str(caught.exception))

    def test_non_numeric_interval_is_rejected(self):
        data = dict(DEFAULT_CONFIG, poll_interval="fast",
                    cookies={"polleverywhere_session_id": "c"})
        self._write(json.dumps(data))
        with self.assertRaises(ConfigError) as caught:
            Config(self.path)
        self.assertIn("poll_interval", str(caught.exception))

    def test_quoted_number_reads_back_as_a_number(self):
        """'5' used to become 5555555555 ticks — a 6430-day poll interval."""
        data = dict(DEFAULT_CONFIG, poll_interval="5",
                    cookies={"polleverywhere_session_id": "c"})
        self._write(json.dumps(data))
        self.assertEqual(Config(self.path).poll_interval, 5.0)

    def test_save_leaves_no_partial_file_behind(self):
        self._write(json.dumps(dict(DEFAULT_CONFIG,
                                    cookies={"polleverywhere_session_id": "c"})))
        config = Config(self.path)
        config.update_cookie("polleverywhere_session_id", "new")
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(
                json.load(handle)["cookies"]["polleverywhere_session_id"], "new"
            )


if __name__ == "__main__":
    unittest.main()
