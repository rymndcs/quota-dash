import copy
import io
import json
import runpy
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, "argv", ["quota-dash"]):
    DASH = runpy.run_path(str(ROOT / "quota-dash"))
NOW = datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc)


def provider(name="claude", remaining=72):
    return {
        "provider": name, "source": "oauth", "plan": "max",
        "windows": [{"id": "session", "label": "session",
                     "percentRemaining": remaining,
                     "resetsAt": "2026-10-25T10:00:00Z",
                     "pace": {"burnMultiple": 0.5}}],
        "state": {"status": "fresh", "stale": False,
                  "refreshedAt": "2026-10-25T07:00:00Z"},
    }


def failed(name="claude"):
    return {"provider": name, "source": "unavailable", "windows": [],
            "state": {"status": "rate_limited", "stale": False,
                      "error": "Claude quota endpoint rate limited"}}


def render(value):
    stream = io.StringIO()
    Console(file=stream, width=160, color_system=None).print(value)
    return stream.getvalue()


class QuotaCacheTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".cache").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".cache")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "quota.json"
        self.cache = DASH["QuotaCache"](self.path)

    def test_failed_provider_retains_windows_without_staling_healthy_peer(self):
        self.cache.update({"providers": [provider(), provider("codex")]}, now=NOW)
        data = self.cache.update({"providers": [failed(), provider("codex", 85)]}, now=NOW)
        claude, codex = data["providers"]
        self.assertEqual(claude["windows"][0]["percentRemaining"], 72)
        self.assertTrue(claude["state"]["stale"])
        self.assertEqual(claude["state"]["refreshedAt"], "2026-10-25T07:00:00Z")
        self.assertEqual(codex["windows"][0]["percentRemaining"], 85)
        self.assertFalse(codex["state"]["stale"])
        text = render(DASH["render_dashboard"](data, "unconfigured", "", [], now=NOW))
        for expected in ("72.0%", "not updating", "1h 0m old", "rate limited", "1 live"):
            self.assertIn(expected, text)
        _, rows = DASH["windows_table"]([claude], NOW)
        pace = render(DASH["pace_table"](rows, NOW))
        self.assertNotIn("under budget", pace)
        self.assertIn("no projection", pace)

    def test_restart_and_command_failure_preserve_last_success_not_last_failure(self):
        original = provider()
        self.cache.update({"providers": [original]}, now=NOW)
        self.cache.update({"providers": [failed()]}, now=NOW)
        restarted = DASH["QuotaCache"](self.path)
        data = restarted.update(None, error="quota-axi timed out", now=NOW)
        self.assertEqual(data["providers"][0]["windows"], original["windows"])
        self.assertIn("timed out", data["providers"][0]["state"]["error"])
        recovered = restarted.update({"providers": [provider(remaining=60)]}, now=NOW)
        self.assertFalse(recovered["providers"][0]["state"]["stale"])
        failed_again = restarted.update({"providers": [failed()]}, now=NOW)
        self.assertEqual(failed_again["providers"][0]["windows"][0]["percentRemaining"], 60)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_stale_upstream_data_does_not_overwrite_last_success(self):
        self.cache.update({"providers": [provider()]}, now=NOW)
        stale = provider(remaining=10)
        stale["source"] = "cache"
        stale["state"].update(status="stale", stale=True, error="sign-in required")
        self.cache.update({"providers": [stale]}, now=NOW)
        data = self.cache.update(None, error="offline", now=NOW)
        self.assertEqual(data["providers"][0]["windows"][0]["percentRemaining"], 72)

    def test_error_status_cannot_replace_last_successful_snapshot(self):
        self.cache.update({"providers": [provider()]}, now=NOW)
        error = provider(remaining=10)
        error["state"].update(status="error", stale=False, error="refresh failed")
        self.cache.update({"providers": [error]}, now=NOW)
        result = DASH["QuotaCache"](self.path).update(None, error="offline", now=NOW)
        self.assertEqual(72, result["providers"][0]["windows"][0]["percentRemaining"])

    def test_malformed_fresh_windows_cannot_overwrite_last_good_reading(self):
        self.cache.update({"providers": [provider()]}, now=NOW)
        saved = self.path.read_bytes()
        for remaining in (None, "unknown", float("nan"), float("inf"), -1, 101):
            with self.subTest(remaining=remaining):
                data = self.cache.update({"providers": [provider(remaining=remaining)]}, now=NOW)
                self.assertEqual(72, data["providers"][0]["windows"][0]["percentRemaining"])
                self.assertEqual(saved, self.path.read_bytes())

    def test_no_saved_reading_does_not_invent_quota(self):
        self.assertIsNone(self.cache.update(None, error="offline", now=NOW))
        self.assertEqual(self.cache.update({"providers": [failed()]}, now=NOW)["providers"], [failed()])

    def test_fresh_empty_windows_clear_old_capacity(self):
        self.cache.update({"providers": [provider()]}, now=NOW)
        empty = provider()
        empty["windows"] = []
        self.cache.update({"providers": [empty]}, now=NOW)
        data = self.cache.update(None, error="offline", now=NOW)
        self.assertEqual(data["providers"][0]["windows"], [])

    def test_omitted_windows_preserve_saved_reading_through_failure_and_restart(self):
        original = {
            "provider": "codex", "source": "oauth", "account": {"accountId": "synthetic"},
            "windows": [{"id": "five_hour", "percentRemaining": 72}],
            "state": {"status": "fresh", "stale": False,
                      "refreshedAt": "2026-11-17T01:00:00Z"},
        }
        incomplete = {key: value for key, value in original.items() if key != "windows"}
        responses = [subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"providers": [report]}))
            for report in (original, incomplete)] + [OSError("offline"), OSError("offline")]
        globals_ = DASH["read_quota"].__globals__
        with patch.dict(globals_, QUOTA_CACHE=self.cache, read_omp_claude=lambda: []), \
                patch("subprocess.run", side_effect=responses):
            DASH["read_quota"]()
            saved = self.path.read_bytes()
            omitted = DASH["read_quota"]()["providers"][0]
            failed_refresh = DASH["read_quota"]()["providers"][0]
            with patch.dict(globals_, QUOTA_CACHE=DASH["QuotaCache"](self.path)):
                restarted = DASH["read_quota"]()["providers"][0]
        for stage, reading in (("omitted", omitted), ("failure", failed_refresh),
                               ("restart offline", restarted)):
            with self.subTest(stage=stage):
                self.assertEqual(original["windows"], reading.get("windows"))
                self.assertTrue(reading["state"]["stale"])
                self.assertEqual(original["state"]["refreshedAt"], reading["state"]["refreshedAt"])
        self.assertEqual(saved, self.path.read_bytes())

    def test_default_json_without_source_is_saved_across_restart(self):
        fresh = provider()
        del fresh["source"]
        self.cache.update({"providers": [fresh]}, now=NOW)
        restarted = DASH["QuotaCache"](self.path)
        data = restarted.update(None, error="offline", now=NOW)
        self.assertEqual(data["providers"][0]["windows"], fresh["windows"])
        self.assertTrue(data["providers"][0]["state"]["stale"])

    def test_accounts_of_one_provider_keep_independent_last_valid_readings(self):
        first, second = provider("codex", 72), provider("codex", 36)
        first.update(accountKey="first", credits={"remaining": 42, "unit": "credits"})
        second.update(accountKey="second", credits={"remaining": 7, "unit": "credits"})
        self.cache.update({"providers": [first, second]}, now=NOW)
        first["windows"] = [{**first["windows"][0], "percentRemaining": 65}]
        failed_second = {**failed("codex"), "accountKey": "second"}
        data = self.cache.update({"providers": [first, failed_second]}, now=NOW)
        accounts = {p["accountKey"]: p for p in data["providers"]}
        self.assertEqual(set(accounts), {"first", "second"})
        self.assertEqual(accounts["first"]["windows"][0]["percentRemaining"], 65)
        self.assertFalse(accounts["first"]["state"]["stale"])
        self.assertEqual(accounts["second"]["windows"][0]["percentRemaining"], 36)
        self.assertEqual(accounts["second"]["credits"]["remaining"], 7)
        self.assertTrue(accounts["second"]["state"]["stale"])
        restarted = DASH["QuotaCache"](self.path).update(None, error="offline", now=NOW)
        self.assertEqual({p["accountKey"]: p["credits"]["remaining"]
                          for p in restarted["providers"]}, {"first": 42, "second": 7})

    def test_command_failures_keep_saved_data_until_recovery(self):
        self.cache.update({"providers": [provider()]}, now=NOW)
        saved = self.path.read_bytes()
        failures = [
            {"side_effect": subprocess.TimeoutExpired("quota-axi", 45)},
            {"side_effect": FileNotFoundError(2, "No such file or directory")},
            {"return_value": subprocess.CompletedProcess([], 1, stdout="{broken")},
            {"return_value": subprocess.CompletedProcess([], 0, stdout='{"providers":null}')},
        ]
        with patch.dict(DASH["read_quota"].__globals__, QUOTA_CACHE=self.cache):
            for failure in failures:
                with self.subTest(failure=failure), patch("subprocess.run", **failure):
                    data = DASH["read_quota"]()
                    cached = data["providers"][0]
                    self.assertEqual(cached["windows"][0]["percentRemaining"], 72)
                    self.assertTrue(cached["state"]["stale"])
                    self.assertEqual(cached["state"]["refreshedAt"], "2026-10-25T07:00:00Z")
                    self.assertEqual(self.path.read_bytes(), saved)
            response = subprocess.CompletedProcess(
                [], 0, stdout=json.dumps({"providers": [provider(remaining=61)]}))
            with patch("subprocess.run", return_value=response):
                recovered = DASH["read_quota"]()["providers"][0]
                self.assertEqual(recovered["windows"][0]["percentRemaining"], 61)
                self.assertFalse(recovered["state"]["stale"])

    def test_failed_atomic_save_keeps_disk_snapshot_and_in_memory_fallback(self):
        self.cache.update({"providers": [provider()]}, now=NOW)
        saved = self.path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("disk full")):
            self.cache.update({"providers": [provider(remaining=60)]}, now=NOW)
        self.assertEqual(self.path.read_bytes(), saved)
        self.assertEqual(set(self.path.parent.iterdir()), {self.path})
        data = self.cache.update(None, error="offline", now=NOW)
        self.assertEqual(data["providers"][0]["windows"][0]["percentRemaining"], 60)

    def test_corrupt_cache_is_ignored(self):
        self.path.write_text("{broken")
        cache = DASH["QuotaCache"](self.path)
        self.assertIsNone(cache.update(None, error="offline", now=NOW))

    def test_missing_provider_retains_saved_reading(self):
        self.cache.update({"providers": [provider()]}, now=NOW)
        data = self.cache.update({"providers": [provider("codex")]}, now=NOW)
        claude = next(p for p in data["providers"] if p["provider"] == "claude")
        self.assertEqual(claude["windows"][0]["percentRemaining"], 72)
        self.assertTrue(claude["state"]["stale"])


class BalanceFallbackTests(unittest.TestCase):
    def test_failed_balance_displays_history_without_claiming_availability(self):
        history = [{"at": "2026-10-25T07:00:00Z", "deepseek": {
            "total_balance": "12.50", "granted_balance": "2.50", "topped_up_balance": "10.00"}}]
        original = copy.deepcopy(history)
        text = render(DASH["render_dashboard"](
            {"providers": []}, "unreachable", "", history, now=NOW))
        self.assertIn("$12.50", text)
        self.assertIn("not updating", text)
        self.assertIn("balance unreachable", text)
        self.assertEqual(history, original)
        unconfigured = render(DASH["render_dashboard"](
            {"providers": []}, "unconfigured", "", history, now=NOW))
        self.assertNotIn("$12.50", unconfigured)

    def test_balance_survives_subscription_failure_without_recording_cached_samples(self):
        (ROOT / ".cache").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".cache") as directory:
            path = Path(directory) / "history.jsonl"
            balances = {"total_balance": "12.50", "granted_balance": "2.50",
                        "topped_up_balance": "10.00"}
            DASH["append_history"](balances, path=path, at=NOW)
            saved = path.read_bytes()
            globals_ = DASH["frame"].__globals__
            with patch.dict(globals_, HISTORY_FILE=path), \
                    patch.dict(globals_, read_quota=lambda: None,
                               read_deepseek=lambda: ("unreachable", "", False)):
                text = render(DASH["frame"]())
            self.assertIn("$12.50", text)
            self.assertIn("not updating", text)
            self.assertIn("quota-axi", text)
            balance_row = next(line for line in text.splitlines() if "DeepSeek balance" in line)
            self.assertIn("⚠", balance_row)
            self.assertNotIn("🟢", balance_row)
            self.assertNotIn("🔴", balance_row)
            self.assertEqual(path.read_bytes(), saved)
            recovery = {**balances, "total_balance": "11.00", "topped_up_balance": "8.50"}
            with patch.dict(globals_, HISTORY_FILE=path), \
                    patch.dict(globals_, read_quota=lambda: {"providers": []},
                               read_deepseek=lambda: (recovery, "yes", False)):
                text = render(DASH["frame"]())
            self.assertIn("$11.00", text)
            self.assertNotIn("not updating", text)
            self.assertEqual([r["deepseek"]["total_balance"] for r in DASH["load_history"](path)],
                             ["12.50", "11.00"])


if __name__ == "__main__":
    unittest.main()
