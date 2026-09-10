import importlib.util
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")

spec = importlib.util.spec_from_file_location("daemon", os.path.join(HERE, "..", "daemon", "ai-usage-daemon.py"))
daemon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daemon)


class FakeProc:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ConfigTests(unittest.TestCase):
    def test_defaults_when_missing(self):
        cfg = daemon.load_config(os.path.join(tempfile.mkdtemp(), "nope.json"))
        self.assertEqual(cfg["openrouter"]["interval"], 60)
        self.assertTrue(cfg["claude"]["enabled"])

    def test_merge(self):
        path = os.path.join(tempfile.mkdtemp(), "config.json")
        with open(path, "w") as fh:
            json.dump({"openrouter": {"key_ref": "op://V/I/F"}, "claude": {"enabled": False}}, fh)
        cfg = daemon.load_config(path)
        self.assertEqual(cfg["openrouter"]["key_ref"], "op://V/I/F")
        self.assertEqual(cfg["openrouter"]["interval"], 60)
        self.assertFalse(cfg["claude"]["enabled"])

    def test_example_config_is_valid(self):
        cfg = daemon.load_config(os.path.join(HERE, "..", "config.example.json"))
        self.assertTrue(cfg["openrouter"]["key_ref"].startswith("op://"))


class SecretTests(unittest.TestCase):
    def test_unconfigured_and_bad_shape(self):
        with self.assertRaises(daemon.SourceError) as ctx:
            daemon.resolve_secret("")
        self.assertIn("No 1Password reference", str(ctx.exception))
        for ref in ("sk-or-v1-abc", "op://Vault/Item"):
            with self.assertRaises(daemon.SourceError):
                daemon.resolve_secret(ref)

    def test_success(self):
        with mock.patch.object(daemon, "find_executable", return_value="/fake/op"), \
                mock.patch.object(daemon.subprocess, "run", return_value=FakeProc(0, b"sk-or-v1-abc\n")) as run:
            self.assertEqual(daemon.resolve_secret("op://V/I/F"), "sk-or-v1-abc")
        self.assertEqual(run.call_args[0][0], ["/fake/op", "read", "--no-newline", "op://V/I/F"])

    def test_classification(self):
        cases = [
            (b"[ERROR] 2026/09/10 10:00:00 You are not currently signed in.\n", "not signed in", "Sign in"),
            (b"[ERROR] 2026/09/10 10:00:00 \"X\" isn't an item in the \"V\" vault.\n", "isn't an item", "Check openrouter.key_ref"),
            (b"[ERROR] 2026/09/10 10:00:00 authorization prompt dismissed\n", "denied or dismissed", "Approve"),
            (b"[ERROR] 2026/09/10 10:00:00 connecting to desktop app: refused\n", "cannot reach", "Open 1Password"),
        ]
        for stderr, msg, action in cases:
            with mock.patch.object(daemon, "find_executable", return_value="/fake/op"), \
                    mock.patch.object(daemon.subprocess, "run", return_value=FakeProc(1, b"", stderr)):
                with self.assertRaises(daemon.SourceError) as ctx:
                    daemon.resolve_secret("op://V/I/F")
            self.assertIn(msg, str(ctx.exception), stderr)
            self.assertTrue(any(action in text for text, _ in ctx.exception.actions), stderr)

    def test_timeout(self):
        with mock.patch.object(daemon, "find_executable", return_value="/fake/op"), \
                mock.patch.object(daemon.subprocess, "run", side_effect=daemon.subprocess.TimeoutExpired("op", 20)):
            with self.assertRaises(daemon.SourceError) as ctx:
                daemon.resolve_secret("op://V/I/F")
        self.assertIn("did not answer", str(ctx.exception))


class BurnRateTests(unittest.TestCase):
    def test_windows(self):
        now = 200000
        history = [[now - 90000, 100, 10], [now - 86400, 100, 12], [now - 3600, 100, 20], [now, 100, 21]]
        rates = {label: (used, covered) for label, used, covered in daemon.burn_rates(history, now)}
        self.assertEqual(rates["1h"], (1, 3600))
        self.assertEqual(rates["24h"], (9, 86400))
        self.assertEqual(rates["7d"], (11, 90000))

    def test_short_coverage(self):
        rates = daemon.burn_rates([[940, 1, 1], [1000, 1, 2]], 1000)
        self.assertTrue(all(used is None for _, used, _ in rates))


class OpenRouterSourceTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp()
        self.cfg = daemon.load_config("/nonexistent")
        self.cfg["openrouter"]["key_ref"] = "op://V/I/F"

    def test_resolves_key_once_and_polls(self):
        src = daemon.OpenRouterSource(self.cfg, self.data_dir)
        with mock.patch.object(daemon, "resolve_secret", return_value="k") as resolve, \
                mock.patch.object(daemon, "fetch_credits", return_value=(100.0, 90.0)), \
                mock.patch.object(daemon, "fetch_keys", return_value=[{"hash": "h", "usage_daily": 1}]) as keys:
            data = src.poll(1_700_000_000)
            src.poll(1_700_000_060)
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(keys.call_count, 1)  # cached for KEYS_REFRESH_SECONDS
        self.assertEqual(data["total_credits"], 100.0)
        self.assertEqual(data["keys"][0]["hash"], "h")
        self.assertTrue(os.path.exists(src.history_file))
        self.assertEqual(len(src.history), 2)

    def test_auth_failure_drops_key(self):
        src = daemon.OpenRouterSource(self.cfg, self.data_dir)
        with mock.patch.object(daemon, "resolve_secret", return_value="k") as resolve, \
                mock.patch.object(daemon, "fetch_credits", side_effect=daemon.SourceError("HTTP 401", auth=True)):
            with self.assertRaises(daemon.SourceError):
                src.poll(1_700_000_000)
            self.assertIsNone(src.key)
            with self.assertRaises(daemon.SourceError):
                src.poll(1_700_000_060)
        self.assertEqual(resolve.call_count, 2)

    def test_history_survives_restart(self):
        src = daemon.OpenRouterSource(self.cfg, self.data_dir)
        with mock.patch.object(daemon, "resolve_secret", return_value="k"), \
                mock.patch.object(daemon, "fetch_credits", return_value=(100.0, 90.0)), \
                mock.patch.object(daemon, "fetch_keys", return_value=[]):
            src.poll(1_700_000_000)
        again = daemon.OpenRouterSource(self.cfg, self.data_dir)
        self.assertEqual(again.history, [[1_700_000_000, 100.0, 90.0]])


class ClaudeSourceTests(unittest.TestCase):
    def test_fixture_json(self):
        cfg = daemon.load_config("/nonexistent")
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_FIXTURE": os.path.join(FIXTURES, "claude-usage.json")}):
            src = daemon.ClaudeSource(cfg, tempfile.mkdtemp())
        data = src.poll(0)
        self.assertEqual(data["session"]["label"], "Current session")
        self.assertEqual([w["label"] for w in data["weekly"]], ["all models", "Fable"])
        self.assertNotIn("\x1b", json.dumps(data))
        self.assertFalse(any(x.startswith("Approximate") for x in data["extra"]))

    def test_parse_errors(self):
        with self.assertRaises(daemon.SourceError):
            daemon.parse_usage("Please run /login\n")
        with self.assertRaises(daemon.SourceError):
            daemon.extract_result('{"is_error": true, "result": "Not logged in"}')

    def test_run_claude_command(self):
        cfg = daemon.load_config("/nonexistent")
        cfg["claude"]["claude_path"] = "/fake/claude"
        src = daemon.ClaudeSource(cfg, tempfile.mkdtemp())
        proc = FakeProc(0, b'{"is_error":false,"result":"Current session: 1% used"}\n\x1b[?2004l')
        with mock.patch.object(daemon.os, "access", return_value=True), \
                mock.patch.object(daemon.subprocess, "run", return_value=proc) as run, \
                mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            self.assertEqual(src.poll(0)["session"]["percent"], 1)
        self.assertEqual(run.call_args[0][0], ["/fake/claude", "-p", "/usage", "--output-format", "json"])
        self.assertNotIn("CLAUDECODE", run.call_args[1]["env"])


class FakeSource:
    name = "fake"
    interval = 60

    def __init__(self, results):
        self.results = list(results)

    def poll(self, now):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp()
        self.stop = threading.Event()
        quiet = mock.patch.object(daemon, "log")
        quiet.start()
        self.addCleanup(quiet.stop)

    def snapshot(self):
        with open(os.path.join(self.data_dir, "fake.json")) as fh:
            return json.load(fh)

    def test_success_then_failure_keeps_data(self):
        src = FakeSource([{"v": 1}, daemon.SourceError("boom", [("Fix it", {"href": "x"})])])
        w = daemon.Worker(src, self.data_dir, self.stop)
        self.assertTrue(w.poll_once())
        snap = self.snapshot()
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["data"], {"v": 1})
        self.assertFalse(w.poll_once())
        snap = self.snapshot()
        self.assertFalse(snap["ok"])
        self.assertEqual(snap["error"], "boom")
        self.assertEqual(snap["actions"], [["Fix it", {"href": "x"}]])
        self.assertEqual(snap["data"], {"v": 1})  # last good data retained
        self.assertEqual(snap["fetched_at"], snap["fetched_at"])

    def test_previous_snapshot_reloaded(self):
        src = FakeSource([{"v": 1}])
        daemon.Worker(src, self.data_dir, self.stop).poll_once()
        w2 = daemon.Worker(FakeSource([daemon.SourceError("down")]), self.data_dir, self.stop)
        w2.poll_once()
        self.assertEqual(self.snapshot()["data"], {"v": 1})

    def test_unexpected_exception_does_not_kill_worker(self):
        w = daemon.Worker(FakeSource([RuntimeError("bad")]), self.data_dir, self.stop)
        self.assertFalse(w.poll_once())
        self.assertIn("RuntimeError: bad", self.snapshot()["error"])

    def test_backoff(self):
        w = daemon.Worker(FakeSource([]), self.data_dir, self.stop)
        self.assertEqual(w.next_delay(True), 60)
        w.failures = 1
        self.assertEqual(w.next_delay(False), 15)
        w.failures = 3
        self.assertEqual(w.next_delay(False), 60)
        w.failures = 99
        self.assertEqual(w.next_delay(False), 60)  # capped at the interval

    def test_trigger_file_breaks_wait(self):
        src = FakeSource([{"v": 1}, {"v": 2}])
        w = daemon.Worker(src, self.data_dir, self.stop)
        open(w.trigger_file, "w").close()
        t = threading.Thread(target=w.run, daemon=True)
        t.start()
        t.join(3)
        # First poll, trigger consumed immediately, second poll, then waiting on the interval.
        self.assertEqual(self.snapshot()["data"], {"v": 2})
        self.assertFalse(os.path.exists(w.trigger_file))
        self.stop.set()
        t.join(3)
        self.assertFalse(t.is_alive())


if __name__ == "__main__":
    unittest.main()
