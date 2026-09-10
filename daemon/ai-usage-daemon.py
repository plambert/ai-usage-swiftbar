#!/usr/bin/env python3
"""ai-usage daemon: polls OpenRouter credits and Claude subscription usage,
and writes one JSON snapshot per source for the SwiftBar plugins to render.

Runs under launchd as a user agent. It resolves the OpenRouter management
key from 1Password once at startup (and again only after an auth failure),
so the 1Password approval prompt appears once per login instead of once per
plugin run.

Only the Python standard library is used, so /usr/bin/python3 (3.9) works.

Usage:
  ai-usage-daemon.py [--config FILE] [--data-dir DIR] [--once] [--source NAME]

Snapshot files: <data-dir>/openrouter.json and <data-dir>/claude.json.
Touch <data-dir>/refresh-<source> to make that source poll immediately.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_CONFIG = os.path.expanduser("~/.config/ai-usage-swiftbar/config.json")
DEFAULT_DATA_DIR = os.path.expanduser("~/Library/Application Support/ai-usage-swiftbar")

API_BASE = os.environ.get("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1").rstrip("/")
USER_AGENT = "ai-usage-swiftbar/0.1.0"
HTTP_TIMEOUT = 15
KEYS_REFRESH_SECONDS = 300
HISTORY_KEEP_SECONDS = 8 * 86400
BURN_WINDOWS = ((3600, "1h"), (86400, "24h"), (7 * 86400, "7d"))

# How long to wait for `op read`. Under launchd nobody is watching a terminal,
# so the 1Password approval prompt needs time to be noticed and answered; a
# short timeout would abandon a prompt the user is about to approve and then
# raise a fresh one on the retry.
OP_TIMEOUT = 120
OP_CANDIDATES = ("/opt/homebrew/bin/op", "/usr/local/bin/op")
CLAUDE_TIMEOUT = 60
CLAUDE_CANDIDATES = (
    os.path.expanduser("~/.local/bin/claude"),
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)
# Retry schedule (seconds) after a failed credential resolution.
RETRY_BACKOFF = (15, 30, 60, 120, 300)


def log(msg):
    sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

DEFAULTS = {
    "op_path": "",
    "openrouter": {"enabled": True, "key_ref": "", "interval": 60},
    "claude": {"enabled": True, "interval": 60, "claude_path": ""},
}


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(path, "r", encoding="utf-8") as fh:
            user = json.load(fh)
    except FileNotFoundError:
        log("config %s not found; using defaults" % path)
        user = {}
    except ValueError as exc:
        log("config %s is not valid JSON: %s" % (path, exc))
        user = {}
    if not isinstance(user, dict):
        user = {}
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    return cfg


# --------------------------------------------------------------------------
# Snapshot files
# --------------------------------------------------------------------------


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class SourceError(Exception):
    """A poll failed. `actions` are (text, params) dropdown lines suggesting a fix."""

    def __init__(self, message, actions=(), auth=False):
        super().__init__(message)
        self.actions = [list(a) for a in actions]
        self.auth = auth


# --------------------------------------------------------------------------
# 1Password
# --------------------------------------------------------------------------


def find_executable(explicit, name, candidates):
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    found = shutil.which(name)
    if found:
        return found
    for candidate in candidates:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


_OP_PREFIX = re.compile(r"^\[ERROR\]\s+\S+\s+\S+\s*")


def op_error_message(stderr):
    for raw in (stderr or "").splitlines():
        text = _OP_PREFIX.sub("", raw.strip())
        if text:
            return text
    return "op exited with an error"


def classify_op_failure(stderr, op, ref):
    text = op_error_message(stderr)
    low = text.lower()
    open_app = ("Open 1Password", {"bash": "/usr/bin/open", "param1": "-a", "param2": "1Password",
                                   "terminal": "false"})
    if "not currently signed in" in low or "no accounts configured" in low or "sign in" in low:
        return "1Password: not signed in", [
            ("Sign in to the 1Password CLI (opens Terminal)",
             {"bash": op, "param1": "signin", "terminal": "true"}),
            ("Or turn on 1Password → Settings → Developer → Integrate with 1Password CLI", {}),
        ]
    if "desktop app" in low:
        return "1Password: cannot reach the 1Password app", [open_app]
    if "dismissed" in low or "denied" in low or "cancel" in low or "rejected" in low:
        return "1Password: access request was denied or dismissed", [
            ("Approve the CLI request in 1Password; the daemon retries on its own", {}),
            open_app,
        ]
    if ("isn't a" in low or "is not a" in low or "not found" in low or "no such" in low
            or "more than one" in low or "could not read secret" in low):
        return "1Password: %s" % text, [
            ("Check openrouter.key_ref in the daemon config: %s" % ref, {}),
            ("Test it in Terminal", {"bash": op, "param1": "read", "param2": ref, "terminal": "true"}),
        ]
    return "1Password: %s" % text, [
        ("Test it in Terminal", {"bash": op, "param1": "read", "param2": ref, "terminal": "true"}),
    ]


def resolve_secret(ref, op_path=""):
    if not ref:
        raise SourceError("No 1Password reference configured", [
            ("Set openrouter.key_ref in %s" % DEFAULT_CONFIG, {}),
            ("Format: op://Vault/Item/Field", {}),
        ])
    if not ref.startswith("op://") or len(ref[len("op://"):].split("/")) < 3:
        raise SourceError("openrouter.key_ref is not an op://Vault/Item/Field reference", [
            ("Current value: %s" % ref, {}),
        ])
    op = find_executable(op_path, "op", OP_CANDIDATES)
    if not op:
        raise SourceError("1Password CLI (op) not found", [
            ("Install it: brew install 1password-cli", {}),
        ])
    try:
        proc = subprocess.run(
            [op, "read", "--no-newline", ref],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=OP_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise SourceError("1Password did not answer within %ds (approval prompt pending?)" % OP_TIMEOUT, [
            ("Approve the CLI request in 1Password; the daemon retries on its own", {}),
            ("Open 1Password", {"bash": "/usr/bin/open", "param1": "-a", "param2": "1Password",
                                "terminal": "false"}),
        ]) from None
    except OSError as exc:
        raise SourceError("Could not run %s: %s" % (op, exc)) from None
    if proc.returncode != 0:
        message, actions = classify_op_failure(proc.stderr.decode("utf-8", "replace"), op, ref)
        raise SourceError(message, actions)
    secret = proc.stdout.decode("utf-8", "replace").strip()
    if not secret:
        raise SourceError("1Password returned an empty value for the reference", [
            ("Check openrouter.key_ref: %s" % ref, {}),
        ])
    return secret


# --------------------------------------------------------------------------
# OpenRouter
# --------------------------------------------------------------------------


def api_get(key, path, params=None):
    url = "%s/%s" % (API_BASE, path.lstrip("/"))
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer %s" % key,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            detail = payload.get("error", {}).get("message") or payload.get("message") or ""
        except (ValueError, AttributeError):
            pass
        raise SourceError("HTTP %d%s" % (exc.code, (": " + detail) if detail else ""),
                          auth=exc.code in (401, 403)) from None
    except urllib.error.URLError as exc:
        raise SourceError("network error: %s" % exc.reason) from None
    except OSError as exc:
        raise SourceError("network error: %s" % exc) from None
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        raise SourceError("invalid JSON from %s" % path) from None


def fetch_credits(key):
    data = api_get(key, "credits").get("data") or {}
    try:
        return float(data["total_credits"]), float(data["total_usage"])
    except (KeyError, TypeError, ValueError):
        raise SourceError("unexpected credits response") from None


def fetch_keys(key):
    keys, seen, offset = [], set(), 0
    for _ in range(10):
        page = api_get(key, "keys", {"offset": offset}).get("data") or []
        new = [k for k in page if k.get("hash") not in seen]
        if not new:
            break
        for k in new:
            seen.add(k.get("hash"))
            keys.append(k)
        offset += len(page)
    return keys


def burn_rates(history, now):
    """[(label, used, seconds_covered)] per window; used is None if coverage < 5 min."""
    if len(history) < 2:
        return []
    latest = history[-1]
    out = []
    for window, label in BURN_WINDOWS:
        older = [h for h in history if h[0] <= now - window]
        base = older[-1] if older else history[0]
        covered = latest[0] - base[0]
        if covered < 300:
            out.append([label, None, covered])
            continue
        out.append([label, max(0.0, latest[2] - base[2]), covered])
    return out


class OpenRouterSource:
    name = "openrouter"

    def __init__(self, cfg, data_dir, config_path=None):
        self.config_path = config_path
        section = cfg.get("openrouter", {})
        self.interval = max(15, int(section.get("interval", 60)))
        self.key_ref = (section.get("key_ref") or "").strip()
        self.key_override = os.environ.get("OPENROUTER_MANAGEMENT_KEY", "").strip()
        self.op_path = (cfg.get("op_path") or "").strip()
        self.history_file = os.path.join(data_dir, "openrouter-history.json")
        self.key = None
        self.keys_cache = {"ts": 0, "data": []}
        self.history = [h for h in read_json(self.history_file).get("history", [])
                        if isinstance(h, list) and len(h) == 3]

    def refresh_settings(self):
        """Pick up an edited key_ref without a daemon restart."""
        if not self.config_path or not os.path.exists(self.config_path):
            return
        cfg = load_config(self.config_path)
        ref = (cfg.get("openrouter", {}).get("key_ref") or "").strip()
        self.op_path = (cfg.get("op_path") or "").strip()
        if ref != self.key_ref:
            log("openrouter: key_ref changed in config")
            self.key_ref = ref
            self.key = None

    def ensure_key(self):
        self.refresh_settings()
        if self.key:
            return
        if self.key_override:
            self.key = self.key_override
            return
        log("resolving OpenRouter key from 1Password")
        self.key = resolve_secret(self.key_ref, self.op_path)
        log("OpenRouter key resolved")

    def poll(self, now):
        self.ensure_key()
        try:
            total, usage = fetch_credits(self.key)
        except SourceError as exc:
            if exc.auth:
                log("OpenRouter auth failure (%s); key will be re-resolved" % exc)
                self.key = None
            raise
        self.history.append([int(now), total, usage])
        self.history = [h for h in self.history if h[0] >= now - HISTORY_KEEP_SECONDS]
        write_json(self.history_file, {"history": self.history})
        if now - self.keys_cache["ts"] >= KEYS_REFRESH_SECONDS:
            try:
                self.keys_cache = {"ts": int(now), "data": fetch_keys(self.key)}
            except SourceError as exc:
                log("OpenRouter keys fetch failed: %s" % exc)
        return {
            "total_credits": total,
            "total_usage": usage,
            "keys": self.keys_cache["data"],
            "keys_ts": self.keys_cache["ts"],
            "burn": burn_rates(self.history, now),
        }


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_LIMIT = re.compile(
    r"^Current (?P<scope>session|week \((?P<model>[^)]*)\))\s*:\s*(?P<pct>\d+)% used"
    r"(?:\s*·\s*resets\s+(?P<reset>.+?))?\s*$"
)
_TZ_SUFFIX = re.compile(r"\s*\(([A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?)\)\s*$")


def strip_ansi(text):
    return _ANSI.sub("", text)


def extract_result(raw):
    raw = raw.strip()
    try:
        payload, _ = json.JSONDecoder().raw_decode(raw)
    except ValueError:
        return strip_ansi(raw)
    if isinstance(payload, dict):
        if payload.get("is_error"):
            raise SourceError(str(payload.get("result") or "claude reported an error"))
        if isinstance(payload.get("result"), str):
            return strip_ansi(payload["result"])
    return strip_ansi(raw)


def parse_usage(text):
    session, weekly, extra, in_extra = None, [], [], False
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _LIMIT.match(line.strip())
        if m:
            reset, tz = m.group("reset") or "", ""
            tzm = _TZ_SUFFIX.search(reset)
            if tzm:
                tz, reset = tzm.group(1), reset[: tzm.start()].strip()
            limit = {
                "label": "Current session" if m.group("scope") == "session" else (m.group("model") or "week"),
                "percent": int(m.group("pct")),
                "reset": reset,
                "tz": tz,
            }
            if m.group("scope") == "session":
                session = limit
            else:
                weekly.append(limit)
            continue
        if line.startswith("What's contributing"):
            in_extra = True
            continue
        if in_extra and line.strip() and not line.startswith("Approximate"):
            extra.append(line)
    if session is None and not weekly:
        first = next((l.strip() for l in text.splitlines() if l.strip()), "no output")
        raise SourceError("could not parse usage: %s" % first[:120])
    return {"session": session, "weekly": weekly, "extra": extra}


class ClaudeSource:
    name = "claude"

    def __init__(self, cfg, data_dir):
        section = cfg.get("claude", {})
        self.interval = max(15, int(section.get("interval", 60)))
        self.claude_path = (section.get("claude_path") or "").strip()
        self.fixture = os.environ.get("CLAUDE_USAGE_FIXTURE", "").strip()
        # claude treats its cwd as the project and scans it at startup. Under
        # launchd the cwd would be "/", and the scan walks into ~/Desktop and
        # friends, triggering macOS folder-access prompts. Give it an empty
        # directory instead.
        self.work_dir = os.path.join(data_dir, "claude-cwd")

    def run_claude(self):
        if self.fixture:
            with open(self.fixture, "r", encoding="utf-8") as fh:
                return extract_result(fh.read())
        claude = find_executable(self.claude_path, "claude", CLAUDE_CANDIDATES)
        if not claude:
            raise SourceError("claude not found; set claude.claude_path in the daemon config")
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        os.makedirs(self.work_dir, exist_ok=True)
        try:
            proc = subprocess.run(
                [claude, "-p", "/usage", "--output-format", "json"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=CLAUDE_TIMEOUT, env=env, cwd=self.work_dir,
            )
        except subprocess.TimeoutExpired:
            raise SourceError("claude did not answer within %ds" % CLAUDE_TIMEOUT) from None
        except OSError as exc:
            raise SourceError("could not run claude: %s" % exc) from None
        out = proc.stdout.decode("utf-8", "replace")
        if proc.returncode != 0:
            lines = strip_ansi(proc.stderr.decode("utf-8", "replace") + "\n" + out).strip().splitlines()
            detail = lines[-1].strip() if lines else "no output"
            raise SourceError("claude exited %d: %s" % (proc.returncode, detail[:120]))
        return extract_result(out)

    def poll(self, now):
        return parse_usage(self.run_claude())


# --------------------------------------------------------------------------
# Worker loop
# --------------------------------------------------------------------------


class Worker(threading.Thread):
    def __init__(self, source, data_dir, stop, once=False):
        super().__init__(name=source.name, daemon=True)
        self.source = source
        self.stop = stop
        self.once = once
        self.snapshot_file = os.path.join(data_dir, "%s.json" % source.name)
        self.trigger_file = os.path.join(data_dir, "refresh-%s" % source.name)
        previous = read_json(self.snapshot_file)
        self.data = previous.get("data") if previous.get("fetched_at") else None
        self.fetched_at = previous.get("fetched_at")
        self.failures = 0

    def write(self, now, error=None, actions=()):
        write_json(self.snapshot_file, {
            "source": self.source.name,
            "ts": int(now),
            "interval": self.source.interval,
            "ok": error is None,
            "error": error,
            "actions": [list(a) for a in actions],
            "fetched_at": self.fetched_at,
            "data": self.data,
        })

    def poll_once(self):
        now = time.time()
        try:
            self.data = self.source.poll(now)
            self.fetched_at = int(now)
            self.failures = 0
            self.write(now)
            return True
        except SourceError as exc:
            self.failures += 1
            log("%s: %s" % (self.source.name, exc))
            self.write(now, str(exc), exc.actions)
        except Exception as exc:  # keep the worker alive on anything unexpected
            self.failures += 1
            log("%s: unexpected %s: %s" % (self.source.name, type(exc).__name__, exc))
            self.write(now, "%s: %s" % (type(exc).__name__, exc))
        return False

    def next_delay(self, ok):
        if ok:
            return self.source.interval
        return min(RETRY_BACKOFF[min(self.failures, len(RETRY_BACKOFF)) - 1], self.source.interval)

    def run(self):
        while not self.stop.is_set():
            ok = self.poll_once()
            if self.once:
                return
            deadline = time.time() + self.next_delay(ok)
            while not self.stop.is_set() and time.time() < deadline:
                if os.path.exists(self.trigger_file):
                    try:
                        os.remove(self.trigger_file)
                    except OSError:
                        pass
                    log("%s: refresh requested" % self.source.name)
                    break
                self.stop.wait(1)


def build_sources(cfg, data_dir, only=None, config_path=None):
    sources = []
    if cfg["openrouter"].get("enabled", True) and only in (None, "openrouter"):
        sources.append(OpenRouterSource(cfg, data_dir, config_path))
    if cfg["claude"].get("enabled", True) and only in (None, "claude"):
        sources.append(ClaudeSource(cfg, data_dir))
    return sources


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=os.environ.get("AI_USAGE_CONFIG", DEFAULT_CONFIG))
    parser.add_argument("--data-dir", default=os.environ.get("AI_USAGE_DATA_DIR", DEFAULT_DATA_DIR))
    parser.add_argument("--once", action="store_true", help="poll each source once and exit")
    parser.add_argument("--source", choices=("openrouter", "claude"), help="run only this source")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    os.makedirs(args.data_dir, exist_ok=True)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    workers = [Worker(s, args.data_dir, stop, once=args.once) for s in build_sources(cfg, args.data_dir, args.source, args.config)]
    if not workers:
        log("no sources enabled")
        return 1
    log("starting: %s (data in %s)" % (", ".join(w.source.name for w in workers), args.data_dir))
    for w in workers:
        w.start()
    while any(w.is_alive() for w in workers):
        for w in workers:
            w.join(0.5)
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
