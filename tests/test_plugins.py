import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.join(HERE, "..", "plugins")


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), os.path.join(PLUGINS, name + ".stream.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


orp = load("openrouter-credits")
clp = load("claude-usage")

NOW = 1_700_000_000


def write_snapshot(data_dir, source, data, ok=True, error=None, actions=(), ts=NOW, fetched_at=NOW, interval=60):
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "%s.json" % source), "w") as fh:
        json.dump({"source": source, "ts": ts, "interval": interval, "ok": ok, "error": error,
                   "actions": [list(a) for a in actions], "fetched_at": fetched_at, "data": data}, fh)


OR_DATA = {"total_credits": 100.0, "total_usage": 90.0, "keys_ts": NOW,
           "keys": [{"hash": "h", "name": "k1", "label": "sk-or-v1-abc", "usage_daily": 1.25,
                     "usage_weekly": 2, "usage_monthly": 3, "limit": 10, "limit_remaining": 7}],
           "burn": [["1h", None, 60], ["24h", 2.0, 86400], ["7d", 2.0, 86400]]}

CL_DATA = {"session": {"label": "Current session", "percent": 81, "reset": "Sep 4 at 5:59pm", "tz": "America/Los_Angeles"},
           "weekly": [{"label": "all models", "percent": 8, "reset": "Sep 5 at 9:59am", "tz": "America/Los_Angeles"},
                      {"label": "Fable", "percent": 6, "reset": "Sep 5 at 9:59am", "tz": "America/Los_Angeles"}],
           "extra": ["Last 24h · 1777 requests · 5 sessions", "  93% of your usage came from subagent-heavy sessions"]}


def or_config(data_dir, **env):
    base = {"AI_USAGE_DATA_DIR": data_dir, "SWIFTBAR_PLUGIN_DATA_PATH": os.path.join(data_dir, "p"),
            "SWIFTBAR_PLUGIN_PATH": "/plugins/openrouter-credits.stream.py"}
    base.update(env)
    return orp.Config(base)


def cl_config(data_dir, **env):
    base = {"AI_USAGE_DATA_DIR": data_dir}
    base.update(env)
    return clp.Config(base)


class OpenRouterRenderTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_ok(self):
        write_snapshot(self.dir, "openrouter", OR_DATA)
        with mock.patch.object(orp.subprocess, "Popen") as popen:
            out = orp.render_once(or_config(self.dir), {}, NOW + 30)
        self.assertTrue(out[0].startswith("$10.00 | templateImage="))
        joined = "\n".join(out)
        self.assertIn("$10.00 remaining", joined)
        self.assertIn("Today $1.25", joined)
        self.assertIn("--k1: today $1.25 · 30d $3.00 · limit $7.00 left of $10.00", joined)
        self.assertIn("24h $2.00 ($2.00/day)", joined)
        self.assertIn("~5d 0h of credit left at the 24h rate", joined)
        self.assertNotIn("1h ", joined)
        self.assertIn("Updated %s (just now)" % orp.clock(NOW), joined)
        self.assertIn("Refresh now | bash=/usr/bin/touch param1=", joined)
        self.assertIn("Restart daemon | bash=/bin/launchctl param1=kickstart param2=-k param3=gui/%d/%s" % (os.getuid(), orp.LAUNCHD_LABEL), joined)
        popen.assert_not_called()

    def test_headline_without_today(self):
        write_snapshot(self.dir, "openrouter", OR_DATA)
        out = orp.render_once(or_config(self.dir), {}, NOW)
        row = next(r for r in out if "remaining" in r and "size=15" in r)
        self.assertTrue(row.startswith("$10.00 remaining | size=15"))
        self.assertIn("$100.00 purchased · $90.00 total used", "\n".join(out))

    def test_headline_with_today(self):
        write_snapshot(self.dir, "openrouter", dict(OR_DATA, used_today=43.21))
        out = orp.render_once(or_config(self.dir), {}, NOW)
        row = next(r for r in out if "remaining" in r and "size=15" in r)
        self.assertTrue(row.startswith("$10.00 remaining, $43.21 used today | size=15"))

    def test_headline_shows_a_zero_spend_day(self):
        write_snapshot(self.dir, "openrouter", dict(OR_DATA, used_today=0.0))
        out = orp.render_once(or_config(self.dir), {}, NOW)
        self.assertIn("$10.00 remaining, $0.00 used today", "\n".join(out))

    def test_low_balance_colors_and_notifies_once(self):
        data = dict(OR_DATA, total_usage=97.0)
        write_snapshot(self.dir, "openrouter", data)
        cfg, state = or_config(self.dir), {}
        with mock.patch.object(orp.subprocess, "Popen") as popen:
            out = orp.render_once(cfg, state, NOW)
            self.assertIn("color=orange", out[0])
            self.assertEqual(popen.call_count, 1)
            self.assertIn("swiftbar://notify?", popen.call_args[0][0][1])
            orp.render_once(cfg, state, NOW + 60)
            self.assertEqual(popen.call_count, 1)

    def test_daemon_error_keeps_last_balance(self):
        write_snapshot(self.dir, "openrouter", OR_DATA, ok=False, error="1Password: not signed in",
                       actions=[("Sign in", {"bash": "/x/op", "param1": "signin", "terminal": "true"})],
                       ts=NOW + 600, fetched_at=NOW)
        out = orp.render_once(or_config(self.dir), {}, NOW + 600)
        self.assertTrue(out[0].startswith("$10.00 ⚠︎ | templateImage="))
        self.assertIn("color=gray", out[0])
        joined = "\n".join(out)
        self.assertIn("1Password: not signed in", joined)
        self.assertIn("Sign in | bash=/x/op param1=signin terminal=true", joined)
        self.assertIn("Showing balance from 10 minutes ago", joined)

    def test_daemon_down_detected_from_stale_ts(self):
        write_snapshot(self.dir, "openrouter", OR_DATA, ts=NOW, fetched_at=NOW)
        out = orp.render_once(or_config(self.dir), {}, NOW + 3600)
        self.assertIn("⚠︎", out[0])
        self.assertIn("Daemon last ran 1 hour ago (not running?)", "\n".join(out))

    def test_no_snapshot(self):
        out = orp.render_once(or_config(self.dir), {}, NOW)
        self.assertTrue(out[0].startswith("OpenRouter ⚠︎ | templateImage="))
        joined = "\n".join(out)
        self.assertIn("No data from the ai-usage daemon", joined)
        self.assertIn("Restart daemon", joined)

    def test_error_before_first_success(self):
        write_snapshot(self.dir, "openrouter", None, ok=False, error="No 1Password reference configured",
                       actions=[("Set openrouter.key_ref", {})], fetched_at=None)
        out = orp.render_once(or_config(self.dir), {}, NOW)
        self.assertTrue(out[0].startswith("OpenRouter ⚠︎"))
        self.assertIn("Set openrouter.key_ref", "\n".join(out))

    def test_title_styles(self):
        write_snapshot(self.dir, "openrouter", OR_DATA)
        with mock.patch.object(orp.subprocess, "Popen"):
            self.assertTrue(orp.render_once(or_config(self.dir, OPENROUTER_TITLE_STYLE="remaining+today"), {}, NOW)[0]
                            .startswith("$10.00 ($1.25 today) | "))
            self.assertTrue(orp.render_once(or_config(self.dir, OPENROUTER_TITLE_STYLE="icon-only"), {}, NOW)[0]
                            .startswith(" | templateImage=") or
                            orp.render_once(or_config(self.dir, OPENROUTER_TITLE_STYLE="icon-only"), {}, NOW)[0]
                            .startswith("| templateImage="))

    def test_once_mode_emits_one_block(self):
        write_snapshot(self.dir, "openrouter", OR_DATA)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"AI_USAGE_DATA_DIR": self.dir, "SWIFTBAR_PLUGIN_DATA_PATH": os.path.join(self.dir, "p")}), \
                mock.patch.object(orp.sys, "stdout", buf), mock.patch.object(orp.subprocess, "Popen"):
            self.assertEqual(orp.main(["--once"]), 0)
        text = buf.getvalue()
        self.assertTrue(text.endswith("\n~~~\n"))
        self.assertEqual(text.count("~~~"), 1)

    def test_render_failure_emits_a_block_instead_of_exiting(self):
        write_snapshot(self.dir, "openrouter", OR_DATA)
        buf, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"AI_USAGE_DATA_DIR": self.dir, "SWIFTBAR_PLUGIN_DATA_PATH": os.path.join(self.dir, "p")}), \
                mock.patch.object(orp.sys, "stdout", buf), mock.patch.object(orp.sys, "stderr", err), \
                mock.patch.object(orp, "render_once", side_effect=RuntimeError("boom")):
            orp.main(["--once"])
        text = buf.getvalue()
        self.assertIn("Plugin error: RuntimeError: boom", text)
        self.assertTrue(text.endswith("\n~~~\n"))
        self.assertIn("RuntimeError: boom", err.getvalue())


class ClaudeRenderTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_ok(self):
        write_snapshot(self.dir, "claude", CL_DATA)
        out = clp.render_once(cl_config(self.dir), NOW)
        self.assertTrue(out[0].startswith("81% | templateImage="))
        self.assertNotIn("⚠", out[0])
        self.assertEqual(out[1], "---")
        self.assertTrue(out[2].startswith("Current session: 81%% used · resets Sep 4 at 5:59pm | href=%s" % clp.URL_USAGE))
        self.assertEqual(out[4], "Weekly usage")
        self.assertTrue(out[5].startswith("All models: 8% used"))
        self.assertTrue(out[6].startswith("Fable: 6% used"))
        self.assertIn("--Last 24h · 1777 requests · 5 sessions", out)
        self.assertIn("Updated %s (just now)" % clp.clock(NOW), "\n".join(out))

    def test_weekly_threshold_adds_triangle(self):
        data = json.loads(json.dumps(CL_DATA))
        data["weekly"][1]["percent"] = 80
        write_snapshot(self.dir, "claude", data)
        out = clp.render_once(cl_config(self.dir), NOW)
        self.assertTrue(out[0].startswith("81% ⚠️ | templateImage="))
        self.assertIn("Fable: 80%% used · resets Sep 5 at 9:59am | href=%s" % clp.URL_USAGE, "\n".join(out))
        out = clp.render_once(cl_config(self.dir, CLAUDE_USAGE_WARN_PERCENT="90"), NOW)
        self.assertNotIn("⚠️", out[0])

    def test_daemon_error_keeps_last_usage(self):
        write_snapshot(self.dir, "claude", CL_DATA, ok=False, error="claude exited 1: boom", ts=NOW + 600, fetched_at=NOW)
        out = clp.render_once(cl_config(self.dir), NOW + 600)
        self.assertTrue(out[0].startswith("81% ⚠︎ | templateImage="))
        joined = "\n".join(out)
        self.assertIn("claude exited 1: boom", joined)
        self.assertIn("Showing usage from 10 minutes ago", joined)

    def test_no_snapshot(self):
        out = clp.render_once(cl_config(self.dir), NOW)
        self.assertTrue(out[0].startswith("Claude ⚠︎ | templateImage="))
        self.assertIn("Restart daemon", "\n".join(out))

    def test_once_mode_emits_one_block(self):
        write_snapshot(self.dir, "claude", CL_DATA)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"AI_USAGE_DATA_DIR": self.dir}), mock.patch.object(clp.sys, "stdout", buf):
            self.assertEqual(clp.main(["--once"]), 0)
        self.assertTrue(buf.getvalue().endswith("\n~~~\n"))

    def test_render_failure_emits_a_block_instead_of_exiting(self):
        write_snapshot(self.dir, "claude", CL_DATA)
        buf, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"AI_USAGE_DATA_DIR": self.dir}), \
                mock.patch.object(clp.sys, "stdout", buf), mock.patch.object(clp.sys, "stderr", err), \
                mock.patch.object(clp, "render", side_effect=RuntimeError("boom")):
            clp.main(["--once"])
        text = buf.getvalue()
        self.assertIn("Plugin error: RuntimeError: boom", text)
        self.assertTrue(text.endswith("\n~~~\n"))
        self.assertIn("RuntimeError: boom", err.getvalue())


class ClaudeAnnounceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = cl_config(self.dir)
        os.makedirs(os.path.dirname(self.cfg.announce_file), exist_ok=True)

    def arm(self):
        open(self.cfg.announce_file, "w").close()

    def run_once(self):
        """Run the stream loop for one pass; returns the emitted menu."""
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"AI_USAGE_DATA_DIR": self.dir}), \
                mock.patch.object(clp.sys, "stdout", buf):
            clp.main(["--once"])
        return buf.getvalue()

    def test_toggle_defaults_to_unchecked_and_arms_on_click(self):
        write_snapshot(self.dir, "claude", CL_DATA)
        out = clp.render_once(self.cfg, NOW)
        row = next(r for r in out if r.startswith(clp.ANNOUNCE_LABEL))
        self.assertNotIn("checked=", row)
        self.assertIn("bash=/usr/bin/touch", row)
        self.assertIn(self.cfg.announce_file, row)

        self.arm()
        out = clp.render_once(self.cfg, NOW)
        row = next(r for r in out if r.startswith(clp.ANNOUNCE_LABEL))
        self.assertIn("checked=true", row)
        self.assertIn("bash=/bin/rm", row)

    def test_toggle_is_offered_when_there_is_no_snapshot(self):
        out = clp.render_once(self.cfg, NOW)
        self.assertTrue(any(r.startswith(clp.ANNOUNCE_LABEL) for r in out))

    def snapshot(self, reset_ts=None, percent=81, last_percent=12):
        data = json.loads(json.dumps(CL_DATA))
        data["session"]["percent"] = percent
        if reset_ts is not None:
            data["last_session"] = {"percent": last_percent, "reset_ts": reset_ts}
        write_snapshot(self.dir, "claude", data, ts=reset_ts or NOW, fetched_at=reset_ts or NOW)

    def test_new_reset_while_armed_says_and_disarms(self):
        with mock.patch.object(clp.subprocess, "Popen") as popen:
            self.snapshot(reset_ts=NOW)
            self.run_once()                       # records NOW, nothing to compare
            self.arm()
            self.snapshot(reset_ts=NOW + 18000)
            out = self.run_once()
        popen.assert_called_once()
        self.assertEqual(popen.call_args[0][0], [clp.SAY_PATH, clp.ANNOUNCE_TEXT])
        self.assertTrue(popen.call_args[1]["start_new_session"])
        self.assertFalse(os.path.exists(self.cfg.announce_file))
        self.assertNotIn("checked=true", out)

    def test_new_reset_while_disarmed_stays_quiet(self):
        with mock.patch.object(clp.subprocess, "Popen") as popen:
            self.snapshot(reset_ts=NOW)
            self.run_once()
            self.snapshot(reset_ts=NOW + 18000)
            self.run_once()
        popen.assert_not_called()

    def test_same_reset_while_armed_stays_quiet_and_armed(self):
        with mock.patch.object(clp.subprocess, "Popen") as popen:
            self.snapshot(reset_ts=NOW)
            self.run_once()
            self.arm()
            self.snapshot(reset_ts=NOW, percent=82)
            self.run_once()
        popen.assert_not_called()
        self.assertTrue(os.path.exists(self.cfg.announce_file))

    def test_first_reading_never_announces(self):
        self.arm()
        with mock.patch.object(clp.subprocess, "Popen") as popen:
            self.snapshot(reset_ts=NOW)
            self.run_once()
        popen.assert_not_called()
        self.assertTrue(os.path.exists(self.cfg.announce_file))


class ClaudeSessionTooltipTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def session_row(self, last_session, now):
        data = json.loads(json.dumps(CL_DATA))
        if last_session is not None:
            data["last_session"] = last_session
        write_snapshot(self.dir, "claude", data)
        out = clp.render_once(cl_config(self.dir), now)
        return next(r for r in out if r.startswith("Current session:"))

    def test_shows_the_previous_session_and_how_long_ago(self):
        row = self.session_row({"percent": 12, "reset_ts": NOW - 4500}, NOW)
        self.assertIn('tooltip="Last session: 12% used - reset 1h15m ago"', row)

    def test_says_just_reset_for_the_first_minute(self):
        row = self.session_row({"percent": 12, "reset_ts": NOW - 59}, NOW)
        self.assertIn('tooltip="Last session: 12% used - just reset"', row)
        row = self.session_row({"percent": 12, "reset_ts": NOW - 60}, NOW)
        self.assertIn('tooltip="Last session: 12% used - reset 1m ago"', row)

    def test_minutes_only_under_an_hour(self):
        row = self.session_row({"percent": 5, "reset_ts": NOW - 3540}, NOW)
        self.assertIn('tooltip="Last session: 5% used - reset 59m ago"', row)

    def test_pads_the_minutes_past_an_hour(self):
        row = self.session_row({"percent": 5, "reset_ts": NOW - 3960}, NOW)
        self.assertIn("reset 1h06m ago", row)

    def test_falls_back_to_the_next_reset_when_no_history(self):
        row = self.session_row(None, NOW)
        self.assertIn('tooltip="Resets Sep 4 at 5:59pm (America/Los_Angeles)"', row)

    def test_weekly_lines_fall_back_when_there_is_nothing_to_pace(self):
        data = json.loads(json.dumps(CL_DATA))
        data["last_session"] = {"percent": 12, "reset_ts": NOW - 4500}
        write_snapshot(self.dir, "claude", data)
        out = clp.render_once(cl_config(self.dir), NOW)
        row = next(r for r in out if r.startswith("All models:"))
        self.assertIn('tooltip="Resets Sep 5 at 9:59am (America/Los_Angeles)"', row)


HALF_WEEK = 3.5 * 86400


class ClaudeWeeklyPaceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def weekly_row(self, percent, start=None, reset_ts=None, resets=None, now=NOW):
        data = json.loads(json.dumps(CL_DATA))
        data["weekly"] = [{"label": "all models", "percent": percent,
                           "reset": "Sep 5 at 9:59am", "tz": "America/Los_Angeles",
                           "reset_ts": reset_ts if reset_ts is not None else (
                               (start + clp.WEEK_SECONDS) if start is not None else None)}]
        if resets is not None:
            data["weekly_resets"] = resets
        write_snapshot(self.dir, "claude", data)
        out = clp.render_once(cl_config(self.dir), now)
        return next(r for r in out if r.startswith("All models:"))

    def test_on_track_at_half_the_week(self):
        row = self.weekly_row(50, start=NOW - HALF_WEEK)
        self.assertIn('tooltip="3.5 of 7 days in, 50% expected - on track"', row)

    def test_five_points_either_way_is_still_on_track(self):
        self.assertIn("on track", self.weekly_row(55, start=NOW - HALF_WEEK))
        self.assertIn("on track", self.weekly_row(45, start=NOW - HALF_WEEK))

    def test_behind_the_line(self):
        row = self.weekly_row(30, start=NOW - HALF_WEEK)
        self.assertIn('tooltip="3.5 of 7 days in, 50% expected - 20% behind"', row)
        self.assertNotIn("runs out", row)

    def test_ahead_of_the_line_projects_a_runout(self):
        row = self.weekly_row(70, start=NOW - HALF_WEEK)
        self.assertIn("3.5 of 7 days in, 50% expected - 20% ahead, runs out ", row)
        self.assertIn("runs out %s" % clp.when(NOW + 129600), row)

    def test_recorded_reset_wins_over_the_assumed_one(self):
        # Upcoming reset puts the start a week back; the record says two days.
        row = self.weekly_row(50, reset_ts=NOW + HALF_WEEK,
                              resets={"all models": {"percent": 88, "reset_ts": NOW - 2 * 86400}})
        self.assertIn("2.0 of 7 days in, 29% expected - 21% ahead", row)

    def test_assumed_start_is_a_week_before_the_upcoming_reset(self):
        row = self.weekly_row(50, reset_ts=NOW + HALF_WEEK)
        self.assertIn("3.5 of 7 days in, 50% expected - on track", row)

    def test_elapsed_never_runs_past_the_week(self):
        row = self.weekly_row(99, start=NOW - 9 * 86400)
        self.assertIn("7.0 of 7 days in, 100% expected - on track", row)


if __name__ == "__main__":
    unittest.main()
