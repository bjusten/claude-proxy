"""Unit tests for journal.config.load_config."""

import json
import os
import tempfile
import unittest

from journal.config import load_config, DEFAULT_CONFIG


class TestJournalConfigAutoCreate(unittest.TestCase):
    def test_missing_file_is_created_with_enabled_false(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            cfg = load_config(path)
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                written = json.load(f)
            self.assertEqual(written.get("enabled"), False)
            self.assertEqual(cfg["enabled"], False)

    def test_missing_file_is_created_with_retention_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            cfg = load_config(path)
            with open(path) as f:
                written = json.load(f)
            self.assertEqual(written.get("max_bytes"), 1073741824)
            self.assertEqual(written.get("max_age_seconds"), 7200)
            self.assertEqual(cfg["max_bytes"], 1073741824)
            self.assertEqual(cfg["max_age_seconds"], 7200)


class TestJournalConfigMerge(unittest.TestCase):
    def test_existing_file_overrides_default(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            with open(path, "w") as f:
                json.dump({"enabled": True}, f)
            cfg = load_config(path)
            self.assertEqual(cfg["enabled"], True)

    def test_partial_user_config_fills_missing_keys_from_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            with open(path, "w") as f:
                json.dump({}, f)  # empty user config
            cfg = load_config(path)
            for k in DEFAULT_CONFIG:
                self.assertIn(k, cfg)

    def test_user_supplied_retention_values_take_precedence(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            with open(path, "w") as f:
                json.dump({"enabled": True, "max_bytes": 4096, "max_age_seconds": 30}, f)
            cfg = load_config(path)
            self.assertEqual(cfg["max_bytes"], 4096)
            self.assertEqual(cfg["max_age_seconds"], 30)

    def test_partial_user_config_fills_retention_fields_from_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            with open(path, "w") as f:
                json.dump({"enabled": True}, f)
            cfg = load_config(path)
            self.assertEqual(cfg["enabled"], True)
            self.assertEqual(cfg["max_bytes"], 1073741824)
            self.assertEqual(cfg["max_age_seconds"], 7200)


class TestJournalConfigGpu(unittest.TestCase):
    def test_default_includes_gpu_poll_interval_seconds(self):
        self.assertEqual(DEFAULT_CONFIG.get("gpu_poll_interval_seconds"), 5)

    def test_missing_file_writes_gpu_poll_interval_seconds(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            cfg = load_config(path)
            with open(path) as f:
                written = json.load(f)
            self.assertEqual(written.get("gpu_poll_interval_seconds"), 5)
            self.assertEqual(cfg["gpu_poll_interval_seconds"], 5)

    def test_user_supplied_gpu_interval_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            with open(path, "w") as f:
                json.dump({"gpu_poll_interval_seconds": 1}, f)
            cfg = load_config(path)
            self.assertEqual(cfg["gpu_poll_interval_seconds"], 1)


class TestJournalConfigSystem(unittest.TestCase):
    def test_default_includes_system_poll_interval_seconds(self):
        self.assertEqual(DEFAULT_CONFIG.get("system_poll_interval_seconds"), 10)

    def test_default_includes_disk_mounts_root(self):
        self.assertEqual(DEFAULT_CONFIG.get("disk_mounts"), ["/"])

    def test_missing_file_writes_system_keys(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            cfg = load_config(path)
            with open(path) as f:
                written = json.load(f)
            self.assertEqual(written.get("system_poll_interval_seconds"), 10)
            self.assertEqual(written.get("disk_mounts"), ["/"])
            self.assertEqual(cfg["system_poll_interval_seconds"], 10)
            self.assertEqual(cfg["disk_mounts"], ["/"])

    def test_user_supplied_system_keys_override(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            with open(path, "w") as f:
                json.dump({
                    "system_poll_interval_seconds": 3,
                    "disk_mounts": ["/", "/data"],
                }, f)
            cfg = load_config(path)
            self.assertEqual(cfg["system_poll_interval_seconds"], 3)
            self.assertEqual(cfg["disk_mounts"], ["/", "/data"])


class TestJournalConfigEnableFlags(unittest.TestCase):
    def test_default_includes_enable_gpu_true(self):
        self.assertEqual(DEFAULT_CONFIG.get("enable_gpu"), True)

    def test_default_includes_enable_system_true(self):
        self.assertEqual(DEFAULT_CONFIG.get("enable_system"), True)

    def test_missing_file_writes_enable_flags(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            cfg = load_config(path)
            with open(path) as f:
                written = json.load(f)
            self.assertEqual(written.get("enable_gpu"), True)
            self.assertEqual(written.get("enable_system"), True)
            self.assertEqual(cfg["enable_gpu"], True)
            self.assertEqual(cfg["enable_system"], True)

    def test_user_supplied_enable_flags_override(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            with open(path, "w") as f:
                json.dump({"enable_gpu": False, "enable_system": False}, f)
            cfg = load_config(path)
            self.assertEqual(cfg["enable_gpu"], False)
            self.assertEqual(cfg["enable_system"], False)

    def test_partial_user_config_fills_enable_flags_from_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            with open(path, "w") as f:
                json.dump({"enable_gpu": False}, f)
            cfg = load_config(path)
            self.assertEqual(cfg["enable_gpu"], False)
            self.assertEqual(cfg["enable_system"], True)


class TestJournalConfigUi(unittest.TestCase):
    def test_default_includes_ui_section_disabled(self):
        ui = DEFAULT_CONFIG.get("ui")
        self.assertIsInstance(ui, dict)
        self.assertEqual(ui.get("enabled"), False)
        self.assertEqual(ui.get("admin_password"), "")
        self.assertEqual(ui.get("session_ttl_seconds"), 43200)

    def test_missing_file_writes_ui_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            cfg = load_config(path)
            with open(path) as f:
                written = json.load(f)
            self.assertEqual(written.get("ui"), {
                "enabled": False,
                "admin_password": "",
                "session_ttl_seconds": 43200,
                "theme": "system",
                "show_gpu": True,
                "show_system": True,
                "state_colors": {
                    "INIT": "#9e9e9e",
                    "CLASSIFYING": "#9c27b0",
                    "QUEUED": "#ff9800",
                    "ROUTING_REQUEST": "#2196f3",
                    "ROUTING_RESPONSE": "#3f51b5",
                    "SUCCESS": "#4caf50",
                    "FAILURE": "#f44336",
                },
            })
            self.assertEqual(cfg["ui"]["enabled"], False)
            self.assertEqual(cfg["ui"]["admin_password"], "")
            self.assertEqual(cfg["ui"]["session_ttl_seconds"], 43200)

    def test_user_supplied_ui_values_override(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            with open(path, "w") as f:
                json.dump({
                    "ui": {
                        "enabled": True,
                        "admin_password": "hunter2",
                        "session_ttl_seconds": 60,
                    },
                }, f)
            cfg = load_config(path)
            self.assertEqual(cfg["ui"]["enabled"], True)
            self.assertEqual(cfg["ui"]["admin_password"], "hunter2")
            self.assertEqual(cfg["ui"]["session_ttl_seconds"], 60)

    def test_partial_ui_user_config_fills_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            with open(path, "w") as f:
                json.dump({"ui": {"enabled": False}}, f)
            cfg = load_config(path)
            self.assertEqual(cfg["ui"]["enabled"], False)
            self.assertEqual(cfg["ui"]["admin_password"], "")
            self.assertEqual(cfg["ui"]["session_ttl_seconds"], 43200)

    def test_missing_ui_section_uses_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            with open(path, "w") as f:
                json.dump({"enabled": True}, f)
            cfg = load_config(path)
            self.assertEqual(cfg["ui"]["enabled"], False)
            self.assertEqual(cfg["ui"]["admin_password"], "")

    def test_ui_enabled_without_password_raises_value_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            with open(path, "w") as f:
                json.dump({"ui": {"enabled": True, "admin_password": ""}}, f)
            with self.assertRaises(ValueError) as cm:
                load_config(path)
            self.assertIn("admin_password", str(cm.exception))

    def test_ui_enabled_with_missing_password_raises_value_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            with open(path, "w") as f:
                json.dump({"ui": {"enabled": True}}, f)
            with self.assertRaises(ValueError) as cm:
                load_config(path)
            self.assertIn("admin_password", str(cm.exception))

    def test_ui_enabled_with_password_loads(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            with open(path, "w") as f:
                json.dump({"ui": {"enabled": True, "admin_password": "ok"}}, f)
            cfg = load_config(path)
            self.assertEqual(cfg["ui"]["enabled"], True)


class TestJournalMaxBodyBytes(unittest.TestCase):
    def test_default_includes_max_body_bytes(self):
        self.assertEqual(DEFAULT_CONFIG.get("max_body_bytes"), 10240)

    def test_missing_file_writes_max_body_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            cfg = load_config(path)
            with open(path) as f:
                written = json.load(f)
            self.assertEqual(written.get("max_body_bytes"), 10240)
            self.assertEqual(cfg["max_body_bytes"], 10240)

    def test_user_supplied_max_body_bytes_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            with open(path, "w") as f:
                json.dump({"max_body_bytes": 256}, f)
            cfg = load_config(path)
            self.assertEqual(cfg["max_body_bytes"], 256)


class TestJournalRedactFields(unittest.TestCase):
    DEFAULT = ["system", "api_key", "Authorization", "api_secret", "private_key"]

    def test_default_includes_redact_fields(self):
        self.assertEqual(DEFAULT_CONFIG.get("redact_fields"), self.DEFAULT)

    def test_missing_file_writes_redact_fields(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            cfg = load_config(path)
            with open(path) as f:
                written = json.load(f)
            self.assertEqual(written.get("redact_fields"), self.DEFAULT)
            self.assertEqual(cfg["redact_fields"], self.DEFAULT)

    def test_user_supplied_redact_fields_overrides_verbatim(self):
        # Config value replaces the default outright — defaults are only a
        # fallback for an absent key, not an enforced floor.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            with open(path, "w") as f:
                json.dump({"redact_fields": ["password"]}, f)
            cfg = load_config(path)
            self.assertEqual(cfg["redact_fields"], ["password"])


class TestJournalConfigStateColors(unittest.TestCase):
    def test_default_includes_state_colors(self):
        import tempfile, os
        from journal.config import load_config
        d = tempfile.mkdtemp()
        cfg = load_config(os.path.join(d, "journal.json"))
        self.assertEqual(cfg["ui"]["state_colors"], {
            "INIT": "#9e9e9e", "CLASSIFYING": "#9c27b0", "QUEUED": "#ff9800",
            "ROUTING_REQUEST": "#2196f3", "ROUTING_RESPONSE": "#3f51b5",
            "SUCCESS": "#4caf50", "FAILURE": "#f44336",
        })

    def test_partial_state_colors_override_keeps_other_defaults(self):
        import tempfile, os, json
        from journal.config import load_config
        d = tempfile.mkdtemp()
        p = os.path.join(d, "journal.json")
        with open(p, "w") as f:
            json.dump({"ui": {"state_colors": {"SUCCESS": "#00ff00"}}}, f)
        cfg = load_config(p)
        self.assertEqual(cfg["ui"]["state_colors"]["SUCCESS"], "#00ff00")
        self.assertEqual(cfg["ui"]["state_colors"]["FAILURE"], "#f44336")
        self.assertEqual(cfg["ui"]["state_colors"]["INIT"], "#9e9e9e")


if __name__ == "__main__":
    unittest.main()
