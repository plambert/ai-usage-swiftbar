#!/usr/bin/env python3
# <xbar.title>Claude Usage</xbar.title>
# <xbar.version>v0.1.0</xbar.version>
# <xbar.author>Paul Lambert</xbar.author>
# <xbar.author.github>plambert</xbar.author.github>
# <xbar.desc>Shows Claude subscription usage from the ai-usage daemon's snapshot: current session in the menu bar, weekly limits in the dropdown.</xbar.desc>
# <xbar.dependencies>python3,ai-usage-daemon</xbar.dependencies>
# <xbar.abouturl>https://github.com/plambert/ai-usage-swiftbar</xbar.abouturl>
# <xbar.var>number(CLAUDE_USAGE_WARN_PERCENT="80"): Show a warning triangle when a weekly limit reaches this percentage</xbar.var>
# <xbar.var>string(AI_USAGE_DATA_DIR=""): Daemon data directory, if not ~/Library/Application Support/ai-usage-swiftbar</xbar.var>
# <swiftbar.type>streamable</swiftbar.type>
# <swiftbar.useTrailingStreamSeparator>true</swiftbar.useTrailingStreamSeparator>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
"""SwiftBar streamable plugin: Claude subscription usage in the menu bar.

This plugin does no fetching itself. The ai-usage daemon (a launchd user
agent) runs `claude -p /usage` and writes <data-dir>/claude.json; this
process watches that file and re-renders the menu whenever it changes, and
at least once a minute so relative times stay current.

Run with --once to print a single menu block and exit.
"""

import json
import os
import subprocess
import sys
import time
import traceback

DEFAULT_DATA_DIR = os.path.expanduser("~/Library/Application Support/ai-usage-swiftbar")
LAUNCHD_LABEL = "net.plambert.ai-usage-swiftbar"
SOURCE = "claude"

URL_USAGE = "https://claude.ai/new#settings/usage"

SAY_PATH = "/usr/bin/say"
ANNOUNCE_LABEL = "Announce when session limit resets"
ANNOUNCE_TEXT = "The claude session limit has reset."

POLL_SECONDS = 2
RERENDER_SECONDS = 60

# 32x32 px TIFF at 144 DPI, so AppKit sizes it at 16x16 pt on a Retina display.
# A PNG would not work here: NSImage(data:) ignores PNG DPI and draws pixels as
# points. Regenerate with tools/make-icon.sh claude-glyph.
ICON_B64 = (
    "SUkqAH4DAAB42pWVC2iVZRjHf26zXM2VGFKGTMugUqO8FKElmqbVkqDCWUkLKxPLkq6WlFLqjAyC6OIoLzQDC4Ki"
    "G1habVJbpaXOqFlK5kqXqUdaptN+33s+zvbtnDPW/+O8z3s57/M9/+f2QQK92MC5rOH+Dns9mMrXNNEdPMnCIC9h"
    "c2ZvDi/wK+fzdzfuP8yzQa7kjszel1xGG6dypBv3z2MrJymPMIjmeG8PZ7GDwfGqQDaFvJFXwzM8EeTiWBaoq4gP"
    "uC6sopsVykpW5bl/Cj8wQLmfgaSUfWlxrGJeOH2Ze4Jcw60d7lzMeg7rp3fCapqnEeazyPECGh2nstbxMZbEN27m"
    "7SyvneA5HueY8fqcMa7/4hwOcBXrgl+auJb3ZBOhhtsSNg+j3shH+Ex2vzOKr9QCTxvPSlZwiNP1X71jhO+5PCuW"
    "w/TLRWHWzHQ+8Q23OE8ZhVlq+YLJahwazg+q/accfuslh3vDW4/LciVbgkVLKOE+XnSszMk8iQlU6/UItbK/XrmL"
    "Oi35jbPjf7zE7C7zp4SlWtyjw06K3pl5kzz/ied95DOID3PoGMfrsRWdcQMbudRnpHoGhJ1vOYNibT2c+F9vnmdG"
    "woo0dnbSu1ff7qTM2VHzbb96Dvrsle8e415JbvxrBL/xqWO7OTONueboaZTSk65xyBt1xrKB1pznBWopNmqR7Mmd"
    "Mkhiu9FoCZnTxp/h2WS15kKZca/I4r+WEfamjjiaI5qlVtoDWnGct7jJem1HrRVQypXWxhUMiSvhj8TdIq1eSD9n"
    "jWZdlZna4K8d9ZSzL8z6MFo9xVZcO0ZaKUODVVVWbrV10GROL7emxrp7jF+swh+ZZMSyUWS3me+InXaG8bmbV43T"
    "aLvOAm5nCjd6Ms+YTrC+JnveGemu1cpTZk6bfqrVAw+xLNThJN/aqK0phrs30ywpNw+TuMv7m+wjUZfva26U8b71"
    "c4JtXGh3+o5H5YR1PZFH7Iyt2vNxniwptFdezW6/AC3m8QH9fKZeLrKLj/B0Fq/IocZOMiWPhkX2sDbG28PQbxuc"
    "n+wPu0uDnT1l5eyyn9fwqZyzUc67gc1rYfWg8Wmmf3yW5rBaf+bHFiMY+S2NN83CzTJJo8BOOs4KGNPF/Qo5V2dW"
    "P9shPuKazLq/lq0PzLqDQr1XkvgS/l/MMYtn6qku8B/2xO25FgAAAQMAAQAAACAAAAABAQMAAQAAACAAAAACAQMA"
    "AgAAAAgACAADAQMAAQAAAAgAAAAGAQMAAQAAAAEAAAAKAQMAAQAAAAEAAAARAQQAAQAAAAgAAAASAQMAAQAAAAEA"
    "AAAVAQMAAQAAAAIAAAAWAQMAAQAAACAAAAAXAQQAAQAAAHYDAAAaAQUAAQAAAIwEAAAbAQUAAQAAAJQEAAAcAQMA"
    "AQAAAAEAAAAeAQUAAQAAAJwEAAAfAQUAAQAAAKQEAAAoAQMAAQAAAAIAAAApAQMAAgAAAAAAAQA9AQMAAQAAAAIA"
    "AAA+AQUAAgAAANwEAAA/AQUABgAAAKwEAABSAQMAAQAAAAIAAAAAAAAAkAAAAAEAAACQAAAAAQAAAAAAAAABAAAA"
    "AAAAAAEAAACF61EAAACAAMP1qAAAAAACzcxMAAAAAAHNzEwAAACAAM3MTAAAAAACj8L1AAAAABA3GqAAAAAAAiuH"
    "CgAAACAA"
)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


class Config:
    def __init__(self, environ=None):
        e = os.environ if environ is None else environ
        try:
            self.warn_percent = float(e.get("CLAUDE_USAGE_WARN_PERCENT", "") or 80)
        except ValueError:
            self.warn_percent = 80.0
        self.data_dir = e.get("AI_USAGE_DATA_DIR", "").strip() or DEFAULT_DATA_DIR
        self.snapshot_file = os.path.join(self.data_dir, "%s.json" % SOURCE)
        self.trigger_file = os.path.join(self.data_dir, "refresh-%s" % SOURCE)
        plugin_data = e.get("SWIFTBAR_PLUGIN_DATA_PATH") or os.path.join(self.data_dir, "plugin-%s" % SOURCE)
        self.state_file = os.path.join(plugin_data, "state.json")
        # Presence of this file is the armed state of the announce toggle; the
        # menu item itself creates and removes it, so the choice survives a
        # plugin restart without the stream having to own it.
        self.announce_file = os.path.join(plugin_data, "announce-session-reset")


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


def announce_armed(cfg):
    return os.path.exists(cfg.announce_file)


def announce_line(cfg):
    """The announce-on-reset toggle: clicking it flips the checkmark.

    SwiftBar runs the command and the stream notices the file within a poll,
    so the menu redraws itself without a plugin restart.
    """
    if announce_armed(cfg):
        return line(ANNOUNCE_LABEL, checked="true", bash="/bin/rm", param1="-f",
                    param2=cfg.announce_file, terminal="false")
    return line(ANNOUNCE_LABEL, bash="/usr/bin/touch", param1=cfg.announce_file,
                terminal="false")


def maybe_announce(cfg, state, usage):
    """Speak a line when the session percentage drops, if the user armed it.

    A drop means the session window rolled over. The arm is one-shot: firing
    clears it. `say` is spawned detached so a long sentence neither blocks the
    render loop nor dies when SwiftBar restarts the plugin.
    """
    session = (usage or {}).get("session")
    percent = session.get("percent") if isinstance(session, dict) else None
    if percent is None:
        return
    previous = state.get("session_percent")
    state["session_percent"] = percent
    if previous is None or percent >= previous or not announce_armed(cfg):
        return
    try:
        subprocess.Popen([SAY_PATH, ANNOUNCE_TEXT], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as exc:
        print("say failed: %s" % exc, file=sys.stderr)
        return
    try:
        os.remove(cfg.announce_file)
    except OSError:
        pass


def capitalize(label):
    return label[:1].upper() + label[1:]


def limit_text(limit):
    text = "%s: %d%% used" % (capitalize(limit["label"]), limit["percent"])
    if limit.get("reset"):
        text += " · resets %s" % limit["reset"]
    return text


def limit_line(limit, cfg):
    params = {"href": URL_USAGE}
    if limit.get("tz"):
        params["tooltip"] = "Resets %s (%s)" % (limit["reset"], limit["tz"])
    if limit["percent"] >= cfg.warn_percent:
        params["color"] = "orange"
    return line(limit_text(limit), **params)


def title_line(usage, cfg, stale):
    session = usage.get("session")
    text = "%d%%" % session["percent"] if session else "?"
    if any(w["percent"] >= cfg.warn_percent for w in usage.get("weekly") or []):
        text += " ⚠️"
    if stale:
        text += " ⚠︎"
    params = {"templateImage": ICON_B64}
    if stale:
        params["color"] = "gray"
    return line(text, **params)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render(cfg, now, snap):
    out = []
    usage = (snap or {}).get("data") or None
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

    if usage is None or fetched_at is None:
        out.append(line("Claude ⚠︎", templateImage=ICON_B64, color="gray"))
        out.append("---")
        out.append(line(error or "No successful fetch yet", color="red"))
        out.extend(action_lines(actions))
        out.append("---")
        out.append(line("Usage settings", href=URL_USAGE))
        out.append(announce_line(cfg))
        out.extend(daemon_lines(cfg))
        return out

    out.append(title_line(usage, cfg, stale))
    out.append("---")
    if stale:
        out.append(line("%s: %s" % (clock(now), error), color="red"))
        out.extend(action_lines(actions))
        out.append(line("Showing usage from %s" % relative(now - fetched_at), color="gray"))
        out.append("---")

    if usage.get("session"):
        out.append(limit_line(usage["session"], cfg))
        out.append("---")
    if usage.get("weekly"):
        out.append(line("Weekly usage"))
        for limit in usage["weekly"]:
            out.append(limit_line(limit, cfg))
        out.append("---")
    if usage.get("extra"):
        out.append(line("What's contributing"))
        for text in usage["extra"]:
            out.append(line("--" + text.strip()))
        out.append("---")

    out.append(line("Usage settings", href=URL_USAGE))
    out.append(line("Updated %s (%s)" % (clock(fetched_at), relative(now - fetched_at))))
    out.append(announce_line(cfg))
    out.extend(daemon_lines(cfg))
    return out


def render_once(cfg, now=None):
    now = time.time() if now is None else now
    snap, _ = read_snapshot(cfg.snapshot_file)
    return render(cfg, now, snap)


# --------------------------------------------------------------------------
# Streaming loop
# --------------------------------------------------------------------------


def emit(rows):
    sys.stdout.write("\n".join(rows) + "\n~~~\n")
    sys.stdout.flush()


def error_rows(exc):
    """Menu shown when rendering raised something we did not anticipate."""
    return [
        line("Claude \u26a0\ufe0e", templateImage=ICON_B64, color="gray"),
        "---",
        line("Plugin error: %s: %s" % (type(exc).__name__, exc), color="red"),
        line("Usage settings", href=URL_USAGE),
    ]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cfg = Config()
    try:
        os.makedirs(os.path.dirname(cfg.announce_file), exist_ok=True)
    except OSError as exc:
        print("plugin data dir unavailable: %s" % exc, file=sys.stderr)
    state = load_state(cfg.state_file)
    once = "--once" in argv
    last_key, last_render = object(), 0.0
    while True:
        snap, mtime = read_snapshot(cfg.snapshot_file)
        now = time.time()
        # The toggle is part of what the menu shows, so a click has to redraw
        # it as promptly as a new snapshot does.
        key = (mtime, announce_armed(cfg))
        if key != last_key or now - last_render >= RERENDER_SECONDS:
            # SwiftBar never restarts a streamable plugin that exits, so an
            # unhandled exception here would leave a stale menu on screen
            # until the user notices. Report the failure in the menu, log a
            # traceback for diagnosis, and try again on the next tick.
            try:
                maybe_announce(cfg, state, (snap or {}).get("data"))
                rows = render(cfg, now, snap)
            except Exception as exc:
                traceback.print_exc()
                rows = error_rows(exc)
            try:
                emit(rows)
            except BrokenPipeError:
                return 0
            try:
                save_state(cfg.state_file, state)
            except Exception:
                traceback.print_exc()
            last_key, last_render = (mtime, announce_armed(cfg)), now
            if once:
                return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
