import copy
import importlib.machinery
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("quota_dash", str(ROOT / "quota-dash"))
spec = importlib.util.spec_from_loader(loader.name, loader)
dash = importlib.util.module_from_spec(spec)
with patch.object(sys, "argv", ["quota-dash"]):
    loader.exec_module(dash)

NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
STAMP = int(NOW.timestamp() * 1000)
FAILED = {"provider": "claude", "source": "unavailable", "windows": [],
          "state": {"status": "rate_limited", "error": "rate limited"}}
CODEX = {"provider": "codex", "source": "live", "windows": [
    {"id": "weekly", "label": "Weekly", "percentRemaining": 60}]}
SCHEMA = """CREATE TABLE usage_history (
    id INTEGER PRIMARY KEY, recorded_at INTEGER NOT NULL, provider TEXT NOT NULL,
    account_key TEXT NOT NULL, email TEXT, account_id TEXT, limit_id TEXT NOT NULL,
    label TEXT NOT NULL, window_label TEXT, used_fraction REAL, status TEXT,
    resets_at INTEGER)"""


class OmpFallbackTest(unittest.TestCase):
    def setUp(self):
        (ROOT / ".cache").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".cache")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "agent.db"
        self.home = patch.object(dash.Path, "home", return_value=Path(self.temp.name))
        self.home.start()
        self.addCleanup(self.home.stop)
        (Path(self.temp.name) / ".omp/agent").mkdir(parents=True)
        self.path = Path(self.temp.name) / ".omp/agent/agent.db"
        self.data = {"providers": [copy.deepcopy(FAILED), copy.deepcopy(CODEX)]}

    def database(self, wal=False):
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(SCHEMA)
        conn.commit()
        return conn

    def save(self, conn, fraction=0.25, age=3600000, account="account-a",
             limit="anthropic:7d", label="Claude 7 Day", provider="anthropic",
             reset=STAMP + 86400000):
        conn.execute("""INSERT INTO usage_history
            (recorded_at, provider, account_key, email, account_id, limit_id,
             label, window_label, used_fraction, status, resets_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (STAMP - age, provider, account, account + "@example.test", account,
             limit, label, None, fraction, "ok", reset))
        conn.commit()

    def read(self, data=None):
        payload = self.data if data is None else data
        with patch.object(dash.subprocess, "run", return_value=SimpleNamespace(
                stdout=json.dumps(payload))):
            return dash.read_quota()

    def render(self, data):
        out = io.StringIO()
        console = Console(file=out, width=160, color_system=None)
        console.print(dash.render_dashboard(data, "unconfigured", "", [], now=NOW))
        return out.getvalue()

    def test_latest_limit_readings_are_kept_separate_per_account(self):
        conn = self.database()
        self.save(conn, fraction=0.9, age=7200000)
        self.save(conn, fraction=0.3, age=1800000)
        self.save(conn, fraction=0.4, age=1800000)
        self.save(conn, fraction=1, age=7200000, limit="anthropic:7d:fable",
                  label="Claude 7 Day (Fable)")
        self.save(conn, fraction=0, account="account-b")
        self.save(conn, fraction=0.99, provider="openai")
        data = self.read()
        claude = [p for p in data["providers"] if p["provider"] == "claude"]
        self.assertEqual(2, len(claude))
        readings = {p["account"]["accountId"]: p for p in claude}
        self.assertEqual([60, 0], [w["percentRemaining"] for w in
                                   readings["account-a"]["windows"]])
        self.assertEqual(100, readings["account-b"]["windows"][0]["percentRemaining"])
        self.assertEqual([CODEX], [p for p in data["providers"]
                                   if p["provider"] != "claude"])
        text = self.render(data)
        for expected in ("omp", "30m old", "2h 0m old", "account-a@example.test",
                         "account-b@example.test", "1 live", "claude stale",
                         "no projection", "Fable"):
            self.assertIn(expected, text)
        self.assertNotIn("lasts through reset", text)

    def test_unusable_claude_readings_fall_back(self):
        conn = self.database()
        self.save(conn)
        for source, status, windows in (
                ("unavailable", "fresh", [{"percentRemaining": 90}]),
                ("live", "rate_limited", [{"percentRemaining": 90}]),
                ("live", "fresh", []),
                ("live", "fresh", [{"percentRemaining": None}]),
                ("live", "fresh", [{"percentRemaining": float("nan")}])):
            with self.subTest(source=source, status=status, windows=windows):
                data = {"providers": [{"provider": "claude", "source": source,
                        "state": {"status": status}, "windows": windows}]}
                result = self.read(data)
                self.assertEqual("omp", result["providers"][0]["source"])
                self.assertEqual(75, result["providers"][0]["windows"][0]["percentRemaining"])

    def test_null_samples_do_not_hide_latest_non_null_limit_readings(self):
        conn = self.database()
        self.save(conn, fraction=0.25)
        self.save(conn, fraction=0.9, age=7200000)
        self.save(conn, fraction=0.6, limit="anthropic:5h", label="Claude 5 Hour")
        self.save(conn, fraction=0.1, account="account-b")
        self.save(conn, fraction=None, account="only-null")
        for age in (3600000, 3599000):
            with self.subTest(null_age=age):
                self.save(conn, fraction=None, age=age)
                data = self.read()
                readings = {p["account"]["accountId"]: p["windows"]
                            for p in data["providers"] if p["provider"] == "claude"}
                self.assertEqual({"account-a", "account-b"}, set(readings))
                self.assertEqual({"anthropic:5h": 40, "anthropic:7d": 75},
                                 {w["id"]: w["percentRemaining"]
                                  for w in readings["account-a"]})
                self.assertEqual(90, readings["account-b"][0]["percentRemaining"])
                text = self.render(data)
                self.assertIn("75.0%", text)
                self.assertIn("40.0%", text)

    def test_history_query_stays_within_linear_work_budget(self):
        conn = self.database()
        conn.execute("CREATE INDEX idx_usage_history_series ON usage_history "
                     "(provider, account_key, limit_id, recorded_at)")
        conn.execute("CREATE INDEX idx_usage_history_recorded ON usage_history (recorded_at)")
        conn.executemany("""INSERT INTO usage_history
            (recorded_at, provider, account_key, limit_id, label, used_fraction)
            VALUES (?, 'anthropic', 'account-a', 'anthropic:7d', 'Claude 7 Day', .25)
            """, ((STAMP + i,) for i in range(4000)))
        conn.commit()
        real_connect = sqlite3.connect
        steps = 0

        def progress():
            nonlocal steps
            steps += 1000
            return int(steps >= 1000000)

        def connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connection.set_progress_handler(progress, 1000)
            return connection

        with patch.object(sqlite3, "connect", side_effect=connect):
            data = self.read()
        self.assertLess(steps, 1000000, "latest-limit query exhausted its VM work budget")
        claude = [p for p in data["providers"] if p["provider"] == "claude"]
        self.assertEqual("omp", claude[0]["source"])
        self.assertEqual(75, claude[0]["windows"][0]["percentRemaining"])

    def test_json_null_keeps_frame_error_panel_even_with_saved_usage(self):
        conn = self.database()
        self.save(conn)
        with patch.object(dash.subprocess, "run", return_value=SimpleNamespace(stdout="null")), \
                patch.object(dash, "read_deepseek", return_value=("unconfigured", "", False)), \
                patch.object(dash, "load_history", return_value=[]):
            out = io.StringIO()
            Console(file=out, width=160, color_system=None).print(dash.frame())
        self.assertIn("quota-axi returned nothing readable", out.getvalue())
        self.assertNotIn("omp", out.getvalue())

    def test_string_state_preserves_usable_claude_and_frame(self):
        conn = self.database()
        self.save(conn)
        data = {"providers": [{"provider": "claude", "source": "oauth",
                "state": "fresh", "windows": [{"id": "weekly", "percentRemaining": 90}]}]}
        with patch.object(dash.subprocess, "run", return_value=SimpleNamespace(
                stdout=json.dumps(data))), \
                patch.object(dash, "read_deepseek", return_value=("unconfigured", "", False)), \
                patch.object(dash, "load_history", return_value=[]):
            out = io.StringIO()
            Console(file=out, width=160, color_system=None).print(dash.frame())
        self.assertIn("90.0%", out.getvalue())
        self.assertIn("1 live", out.getvalue())
        self.assertNotIn("omp", out.getvalue())
        self.assertEqual(data, self.read(data))

    def test_missing_claude_provider_falls_back(self):
        conn = self.database()
        self.save(conn)
        data = self.read({"providers": [CODEX]})
        self.assertEqual({"codex", "claude"},
                         {p["provider"] for p in data["providers"]})

    def test_usable_live_and_cached_claude_are_preserved(self):
        conn = self.database()
        self.save(conn)
        for source, status in (("oauth", "fresh"), ("cache", "stale")):
            with self.subTest(source=source):
                data = {"providers": [{"provider": "claude", "source": source,
                        "state": {"status": status},
                        "windows": [{"percentRemaining": 90}]}]}
                self.assertEqual(data, self.read(data))

    def test_wal_only_reading_is_visible_and_does_not_block_a_writer(self):
        conn = self.database(wal=True)
        self.save(conn, fraction=0.8)
        self.assertTrue(Path(str(self.path) + "-wal").exists())
        self.assertAlmostEqual(20, self.read()["providers"][-1]["windows"][0]["percentRemaining"])
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE usage_history SET used_fraction = 0.5")
        self.assertAlmostEqual(20, self.read()["providers"][-1]["windows"][0]["percentRemaining"])
        conn.commit()
        self.assertEqual(50, self.read()["providers"][-1]["windows"][0]["percentRemaining"])

    def test_reader_is_read_only_and_closed_after_refresh(self):
        conn = self.database()
        self.save(conn)
        before = self.path.read_bytes()
        real_connect = sqlite3.connect
        opened = []

        def connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                connection.execute("UPDATE usage_history SET used_fraction = 0")
            return connection

        with patch.object(sqlite3, "connect", side_effect=connect):
            self.read()
        self.assertEqual(1, len(opened))
        with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
            opened[0].execute("SELECT 1")
        self.assertEqual(before, self.path.read_bytes())
        conn.execute("BEGIN EXCLUSIVE")
        conn.rollback()

    def test_missing_database_is_silent_and_is_not_created(self):
        self.assertEqual(self.data, self.read())
        self.assertFalse(self.path.exists())

    def test_locked_database_is_silent(self):
        conn = self.database()
        self.save(conn)
        conn.execute("BEGIN EXCLUSIVE")
        self.assertEqual(self.data, self.read())
        conn.rollback()
        self.assertEqual("omp", self.read()["providers"][-1]["source"])

    def test_unreadable_database_is_silent(self):
        self.path.mkdir()
        self.assertEqual(self.data, self.read())

    def test_incompatible_schema_and_corrupt_file_are_silent(self):
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE usage_history (id INTEGER)")
        conn.commit()
        conn.close()
        self.assertEqual(self.data, self.read())
        self.path.write_bytes(b"not a sqlite database")
        self.assertEqual(self.data, self.read())

    def test_invalid_saved_values_do_not_replace_original(self):
        conn = self.database()
        for value in (None, "unknown", -0.1, 1.1, float("inf")):
            with self.subTest(value=value):
                self.save(conn, fraction=value)
                self.assertEqual(self.data, self.read())
        self.save(conn, fraction=0.5, age=-10**18)
        self.assertEqual(self.data, self.read())

    def test_invalid_account_keys_and_schema_field_types_are_ignored(self):
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        conn.execute(SCHEMA.replace("label TEXT NOT NULL", "label BLOB NOT NULL"))
        self.save(conn)
        for column, value in (("account_key", ""), ("account_key", b"bytes"),
                              ("label", 123), ("recorded_at", "unknown"),
                              ("resets_at", "unknown")):
            with self.subTest(column=column, value=value):
                conn.execute(f"UPDATE usage_history SET {column} = ?", (value,))
                conn.commit()
                self.assertEqual(self.data, self.read())
                conn.execute("DELETE FROM usage_history WHERE provider = 'anthropic'")
                self.save(conn)

    def test_quota_command_failure_keeps_existing_error_behavior(self):
        conn = self.database()
        self.save(conn)
        with patch.object(dash.subprocess, "run", side_effect=OSError("no quota-axi")):
            self.assertIsNone(dash.read_quota())

    def test_resetless_and_elapsed_saved_windows_stay_stale(self):
        conn = self.database()
        self.save(conn, reset=None)
        self.save(conn, limit="anthropic:7d:fable", label="Claude Fable",
                  reset=STAMP - 1000)
        text = self.render(self.read())
        self.assertIn("reset passed", text)
        self.assertIn("no projection", text)
        self.assertIn("omp", text)
        self.assertNotIn("lasts through reset", text)

    def test_frame_renders_fallback_without_new_error_banner(self):
        conn = self.database()
        self.save(conn)
        with patch.object(dash.subprocess, "run", return_value=SimpleNamespace(
                stdout=json.dumps(self.data))), \
                patch.object(dash, "read_deepseek", return_value=("unconfigured", "", False)), \
                patch.object(dash, "load_history", return_value=[]):
            out = io.StringIO()
            Console(file=out, width=160).print(dash.frame())
        self.assertIn("75.0%", out.getvalue())
        self.assertIn("omp", out.getvalue())
        self.assertNotIn("returned nothing readable", out.getvalue())


if __name__ == "__main__":
    unittest.main()
