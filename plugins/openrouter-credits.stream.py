#!/usr/bin/env python3
# <xbar.title>OpenRouter Credits</xbar.title>
# <xbar.version>v0.1.0</xbar.version>
# <xbar.author>Paul Lambert</xbar.author>
# <xbar.author.github>plambert</xbar.author.github>
# <xbar.desc>Shows remaining openrouter.ai credits from the ai-usage daemon's snapshot, with spend and burn-rate details in the dropdown.</xbar.desc>
# <xbar.dependencies>python3,ai-usage-daemon</xbar.dependencies>
# <xbar.abouturl>https://github.com/plambert/ai-usage-swiftbar</xbar.abouturl>
# <xbar.var>number(OPENROUTER_LOW_CREDITS="5"): Show the balance in orange below this many dollars</xbar.var>
# <xbar.var>number(OPENROUTER_CRITICAL_CREDITS="1"): Show the balance in red below this many dollars</xbar.var>
# <xbar.var>boolean(OPENROUTER_NOTIFY="true"): Send a notification when the balance drops below a threshold</xbar.var>
# <xbar.var>select(OPENROUTER_TITLE_STYLE="remaining"): What to show in the menu bar next to the icon. [remaining, remaining+today, icon-only]</xbar.var>
# <xbar.var>string(AI_USAGE_DATA_DIR=""): Daemon data directory, if not ~/Library/Application Support/ai-usage-swiftbar</xbar.var>
# <swiftbar.type>streamable</swiftbar.type>
# <swiftbar.useTrailingStreamSeparator>true</swiftbar.useTrailingStreamSeparator>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
"""SwiftBar streamable plugin: remaining openrouter.ai credits in the menu bar.

This plugin does no fetching itself. The ai-usage daemon (a launchd user
agent) polls OpenRouter and writes <data-dir>/openrouter.json; this process
watches that file and re-renders the menu whenever it changes, and at least
once a minute so relative times stay current.

Run with --once to print a single menu block and exit.
"""

import json
import os
import subprocess
import sys
import time
import urllib.parse

DEFAULT_DATA_DIR = os.path.expanduser("~/Library/Application Support/ai-usage-swiftbar")
LAUNCHD_LABEL = "net.plambert.ai-usage-swiftbar"
SOURCE = "openrouter"

URL_CREDITS = "https://openrouter.ai/settings/credits"
URL_ACTIVITY = "https://openrouter.ai/activity"
URL_KEYS = "https://openrouter.ai/settings/keys"

POLL_SECONDS = 2        # how often to check the snapshot's mtime
RERENDER_SECONDS = 60   # re-render even if unchanged, to advance relative times

# 46x32 px TIFF at 144 DPI, so AppKit sizes it at 23x16 pt on a Retina display.
# A PNG would not work here: NSImage(data:) ignores PNG DPI and draws pixels as
# points. Regenerate with tools/make-icon.sh openrouter-glyph.
ICON_B64 = (
    "SUkqAFICAAB42q2WTWxMURiGn4i/0upGZJhakFQi0Rj/04SFLRUSG1YSf00aoUzb1EKCSND4TUuiWAkSCSFBWEmk"
    "VbWhDRs7E9ImNmKhjMzwTqdm7t/cczruu5ic895z3nvmO+/3fRcCUEMTG1nOImqZjg3GGGGYR9zjR8iqJXSwk1lW"
    "in584Si3Ap9UcZJDTKtQ9x+ucpCsh6vnPg3/qVvAOdpd8wTPmReJch6beeo4c1+EyvBeTsiNj6p4wzLP0xwDYj9r"
    "VMc6kkyZpPp6+ifik3LxGd3HWUYdzHzdf7OlHws4ox15131gqoP9xFaGAlavkH8XWms/YLt+b7LbpdyoPAjGAl5b"
    "q/exgTlSKmXKL8V2KGTHSqnb+f8xW5SDdxzMJQ4b9nRzwEq7Wxl0nb3FeVaeGDXsiZO28kwTT+SzNa4YmTEgR5ow"
    "wmJ+8pW5ReY8bRbaF2k1rmmml/ztlVx7RPvMaKfLsOKZIpL1aKe4EIH2Sznk+/go2pj8podO5XUBg6wtPulXFTCj"
    "3F2mlbc9fHQwvewrjnNyWGUevC3Xf/OxO7jrmF02euAKLYH8cU74uBqdtJTzGeX8uxDl1bwqm/NB6jfY44pasmyt"
    "iqua1IW82a9erxrrPEuabbwN2LmKh6HKwepdnt6Z4RqnXaePq9Lvt6qAXvWZvPD5Kid3Dk70tKR8at/TvOox1Sz7"
    "njJZ9QZ1fVM0K1ePqcc1RqZ+jFOu+QzdWIrqSLT/sNTHxfTGXcyOQD0RyFazSd/ICfWOWv2XSjCmL5y2v5Uuh1cA"
    "FgAAAQMAAQAAAC4AAAABAQMAAQAAACAAAAACAQMAAgAAAAgACAADAQMAAQAAAAgAAAAGAQMAAQAAAAEAAAAKAQMA"
    "AQAAAAEAAAARAQQAAQAAAAgAAAASAQMAAQAAAAEAAAAVAQMAAQAAAAIAAAAWAQMAAQAAACAAAAAXAQQAAQAAAEkC"
    "AAAaAQUAAQAAAGADAAAbAQUAAQAAAGgDAAAcAQMAAQAAAAEAAAAeAQUAAQAAAHADAAAfAQUAAQAAAHgDAAAoAQMA"
    "AQAAAAIAAAApAQMAAgAAAAAAAQA9AQMAAQAAAAIAAAA+AQUAAgAAALADAAA/AQUABgAAAIADAABSAQMAAQAAAAIA"
    "AAAAAAAAkAAAAAEAAACQAAAAAQAAAAAAAAABAAAAAAAAAAEAAACF61EAAACAAMP1qAAAAAACzcxMAAAAAAHNzEwA"
    "AACAAM3MTAAAAAACj8L1AAAAABA3GqAAAAAAAiuHCgAAACAA"
)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def env_float(e, name, default):
    try:
        return float(e.get(name, "") or default)
    except ValueError:
        return float(default)


class Config:
    def __init__(self, environ=None):
        e = os.environ if environ is None else environ
        self.low = env_float(e, "OPENROUTER_LOW_CREDITS", 5)
        self.critical = env_float(e, "OPENROUTER_CRITICAL_CREDITS", 1)
        self.notify = (e.get("OPENROUTER_NOTIFY", "true").strip().lower() or "true") in ("1", "true", "yes", "on")
        self.title_style = e.get("OPENROUTER_TITLE_STYLE", "remaining").strip() or "remaining"
        self.data_dir = e.get("AI_USAGE_DATA_DIR", "").strip() or DEFAULT_DATA_DIR
        self.snapshot_file = os.path.join(self.data_dir, "%s.json" % SOURCE)
        self.trigger_file = os.path.join(self.data_dir, "refresh-%s" % SOURCE)
        self.plugin_name = os.path.basename(e.get("SWIFTBAR_PLUGIN_PATH", os.path.abspath(__file__)))
        plugin_data = e.get("SWIFTBAR_PLUGIN_DATA_PATH") or os.path.join(self.data_dir, "plugin-%s" % SOURCE)
        self.state_file = os.path.join(plugin_data, "state.json")


def read_snapshot(path):
    """Return (snapshot dict or None, mtime or None)."""
    try:
        mtime = os.stat(path).st_mtime
        with open(path, "r", encoding="utf-8") as fh:
            snap = json.load(fh)
        return (snap if isinstance(snap, dict) else None), mtime
    except (OSError, ValueError):
        return None, None


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as exc:
        print("state save failed: %s" % exc, file=sys.stderr)


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------


def money(value, decimals=2):
    sign = "-" if value < 0 else ""
    return "%s$%s" % (sign, format(abs(value), ",.%df" % decimals))


def duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh %dm" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dd %dh" % (seconds // 86400, (seconds % 86400) // 3600)


def relative(seconds):
    seconds = int(seconds)
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        count, unit = max(1, seconds // 60), "minute"
    elif seconds < 86400:
        count, unit = seconds // 3600, "hour"
    else:
        count, unit = seconds // 86400, "day"
    return "%d %s%s ago" % (count, unit, "" if count == 1 else "s")


def clock(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


def clean(text):
    return str(text).replace("|", "¦").replace("\n", " ")


def line(text, **params):
    parts = [clean(text)]
    if params:
        rendered = []
        for k, v in params.items():
            v = str(v)
            if " " in v or '"' in v:
                v = '"%s"' % v.replace('"', '\\"')
            rendered.append("%s=%s" % (k, v))
        parts.append(" ".join(rendered))
    return " | ".join(parts)


def action_lines(actions):
    return [line(text, **params) for text, params in actions]


def daemon_lines(cfg):
    uid = os.getuid()
    return [
        line("Restart daemon", bash="/bin/launchctl", param1="kickstart", param2="-k",
             param3="gui/%d/%s" % (uid, LAUNCHD_LABEL), terminal="false"),
        line("Refresh now", bash="/usr/bin/touch", param1=cfg.trigger_file, terminal="false"),
    ]


LEVEL_COLOR = {"low": "orange", "critical": "red"}
LEVEL_RANK = {"ok": 0, "low": 1, "critical": 2}


def alert_level(remaining, cfg):
    if remaining < cfg.critical:
        return "critical"
    if remaining < cfg.low:
        return "low"
    return "ok"


def title_line(cfg, remaining, today, level, stale):
    style = cfg.title_style
    if style == "icon-only":
        text = ""
    elif style == "remaining+today" and today is not None:
        text = "%s (%s today)" % (money(remaining), money(today))
    else:
        text = money(remaining)
    if stale:
        text = (text + " ⚠︎").strip()
    params = {"templateImage": ICON_B64}
    if stale:
        params["color"] = "gray"
    elif level in LEVEL_COLOR:
        params["color"] = LEVEL_COLOR[level]
    return line(text, **params)


def maybe_notify(cfg, state, level, remaining):
    previous = state.get("alert_level", "ok")
    state["alert_level"] = level
    if not cfg.notify or LEVEL_RANK[level] <= LEVEL_RANK.get(previous, 0):
        return
    title = "OpenRouter credits %s" % ("critically low" if level == "critical" else "low")
    query = urllib.parse.urlencode({"plugin": cfg.plugin_name, "title": title,
                                    "body": "%s remaining" % money(remaining), "href": URL_CREDITS})
    try:
        subprocess.Popen(["/usr/bin/open", "swiftbar://notify?%s" % query],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render(cfg, state, now, snap):
    out = []
    data = (snap or {}).get("data") or None
    fetched_at = (snap or {}).get("fetched_at")
    error = None
    actions = []
    if snap is None:
        error = "No data from the ai-usage daemon (not running?)"
    else:
        interval = int(snap.get("interval") or 60)
        if now - int(snap.get("ts") or 0) > 3 * interval + 30:
            error = "Daemon last ran %s (not running?)" % relative(now - int(snap.get("ts") or 0))
        elif not snap.get("ok"):
            error = snap.get("error") or "unknown error"
            actions = [(a[0], a[1]) for a in snap.get("actions") or [] if len(a) == 2]
    stale = error is not None

    if data is None or fetched_at is None:
        out.append(line("OpenRouter ⚠︎", templateImage=ICON_B64, color="gray"))
        out.append("---")
        out.append(line(error or "No successful fetch yet", color="red"))
        out.extend(action_lines(actions))
        out.append("---")
        out.append(line("Credits & billing", href=URL_CREDITS))
        out.extend(daemon_lines(cfg))
        return out

    total, usage = float(data["total_credits"]), float(data["total_usage"])
    remaining = total - usage
    level = alert_level(remaining, cfg)
    keys = data.get("keys") or []
    today = sum(float(k.get("usage_daily") or 0) for k in keys) if keys else None
    week = sum(float(k.get("usage_weekly") or 0) for k in keys) if keys else None
    month = sum(float(k.get("usage_monthly") or 0) for k in keys) if keys else None

    out.append(title_line(cfg, remaining, today, level, stale))
    out.append("---")
    if stale:
        out.append(line("%s: %s" % (clock(now), error), color="red"))
        out.extend(action_lines(actions))
        out.append(line("Showing balance from %s" % relative(now - fetched_at), color="gray"))
        out.append("---")

    out.append(line("%s remaining" % money(remaining), size=15,
                    **({"color": LEVEL_COLOR[level]} if level in LEVEL_COLOR else {})))
    out.append(line("%s purchased · %s used" % (money(total), money(usage)), color="gray"))
    if level != "ok":
        threshold = cfg.critical if level == "critical" else cfg.low
        out.append(line("Below %s threshold (%s)" % (level, money(threshold)), color=LEVEL_COLOR[level]))
    out.append("---")

    if keys:
        out.append(line("Spend via API keys"))
        out.append(line("Today %s · 7 days %s · 30 days %s" % (money(today), money(week), money(month))))
        out.append(line("Keys (%d)" % len(keys)))
        for k in sorted(keys, key=lambda k: -float(k.get("usage_monthly") or 0)):
            name = k.get("name") or k.get("label") or k.get("hash", "")[:8]
            parts = ["today %s" % money(float(k.get("usage_daily") or 0)),
                     "30d %s" % money(float(k.get("usage_monthly") or 0))]
            if k.get("limit") is not None:
                parts.append("limit %s left of %s" % (money(float(k.get("limit_remaining") or 0)),
                                                      money(float(k["limit"]))))
            out.append(line("--%s: %s" % (name, " · ".join(parts))))
            out.append(line("--%s" % (k.get("label") or ""), alternate="true", color="gray"))
        out.append("---")

    rate_lines, runway = [], None
    for label, used, covered in data.get("burn") or []:
        if used is None:
            continue
        per_day = used / covered * 86400 if covered else 0
        rate_lines.append("%s %s (%s/day)" % (label, money(used), money(per_day)))
        if label == "24h" and per_day > 0:
            runway = remaining / per_day
    if rate_lines:
        out.append(line("Burn rate"))
        out.extend(line(r) for r in rate_lines)
        if runway is not None:
            out.append(line("~%s of credit left at the 24h rate" % duration(runway * 86400)))
        out.append("---")

    out.append(line("Credits & billing", href=URL_CREDITS))
    out.append(line("Activity", href=URL_ACTIVITY))
    out.append(line("API keys", href=URL_KEYS))
    out.append("---")
    out.append(line("Updated %s (%s)" % (clock(fetched_at), relative(now - fetched_at))))
    out.extend(daemon_lines(cfg))
    return out


def render_once(cfg, state, now=None):
    now = time.time() if now is None else now
    snap, _ = read_snapshot(cfg.snapshot_file)
    out = render(cfg, state, now, snap)
    data = (snap or {}).get("data")
    if snap and snap.get("ok") and data:
        remaining = float(data["total_credits"]) - float(data["total_usage"])
        maybe_notify(cfg, state, alert_level(remaining, cfg), remaining)
    return out


# --------------------------------------------------------------------------
# Streaming loop
# --------------------------------------------------------------------------


def emit(rows):
    sys.stdout.write("\n".join(rows) + "\n~~~\n")
    sys.stdout.flush()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cfg = Config()
    state = load_state(cfg.state_file)
    once = "--once" in argv
    last_mtime, last_render = object(), 0.0
    while True:
        _, mtime = read_snapshot(cfg.snapshot_file)
        now = time.time()
        if mtime != last_mtime or now - last_render >= RERENDER_SECONDS:
            try:
                emit(render_once(cfg, state, now))
            except BrokenPipeError:
                return 0
            save_state(cfg.state_file, state)
            last_mtime, last_render = mtime, now
            if once:
                return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
