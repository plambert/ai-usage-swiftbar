# ai-usage-swiftbar

Menu bar usage meters for [SwiftBar](https://github.com/swiftbar/swiftbar): remaining
[openrouter.ai](https://openrouter.ai) credits and Claude subscription usage. A single launchd
user agent does all the fetching and holds the credentials; two streamable SwiftBar plugins
render what it writes.

This replaces the one-shot plugins in `openrouter-swiftbar` and `claude-swiftbar`. The reason
for the split is 1Password: a one-shot plugin is a fresh process every minute, and the
1Password CLI integration asks for approval per process tree. The daemon resolves the OpenRouter
management key once at login and keeps it in memory, so the approval prompt appears once.

## How it fits together

```text
launchd ──▶ ai-usage-daemon.py ──▶ ~/Library/Application Support/ai-usage-swiftbar/
               │                        openrouter.json   claude.json
               ├─ op read (once)                ▲              ▲
               ├─ GET /api/v1/credits            │              │
               └─ claude -p /usage      openrouter-credits   claude-usage
                                        .stream.py           .stream.py   (SwiftBar)
```

* The daemon polls each source on its own interval (60 seconds by default), and writes one JSON
  snapshot per source: timestamp, ok flag, error text and suggested fixes, the last good data,
  and when it was fetched. A failed poll keeps the last good data in the snapshot.
* Each plugin is a SwiftBar streamable plugin. It watches its snapshot's mtime, re-renders the
  menu when the file changes, and re-renders once a minute regardless so "5 minutes ago" text
  stays current. It never contacts OpenRouter, Claude, or 1Password.
* "Refresh now" in a dropdown touches `refresh-<source>` in the data directory; the daemon
  notices within a second and polls. "Restart daemon" runs `launchctl kickstart -k`.
* Failure classification is the same as before: the last good value stays in the menu bar in
  gray with a warning mark, and the dropdown shows the error plus an action line (sign in to
  1Password, approve the pending request, check the reference, and so on). If the snapshot's
  timestamp is older than three intervals, the plugin reports the daemon as not running.

## Requirements

* SwiftBar 2.1 or later
* `python3`: Homebrew or the Xcode Command Line Tools `/usr/bin/python3` (3.9). Standard
  library only.
* An OpenRouter management key stored in 1Password, and the
  [1Password CLI](https://developer.1password.com/docs/cli/get-started/) with the desktop app
  integration turned on
* Claude Code (`claude`) logged in to a subscription

## Install

```bash
make install
```

`install-daemon` copies the daemon to `~/.local/libexec/ai-usage-swiftbar/`, writes
`~/.config/ai-usage-swiftbar/config.json` from `config.example.json` if it does not exist,
renders `~/Library/LaunchAgents/net.plambert.ai-usage-swiftbar.plist` from the template, and
loads it with `launchctl bootstrap`. Loading starts the daemon, which resolves the OpenRouter key:
expect one 1Password approval prompt. `install-plugins` copies both plugins into the SwiftBar
plugin directory.

Set `openrouter.key_ref` in the config file before or right after installing; the daemon
retries with backoff and picks up a corrected reference only on restart (`make restart`).

```json
{
  "op_path": "",
  "openrouter": {"enabled": true, "key_ref": "op://Private/OpenRouter/credential", "interval": 60},
  "claude": {"enabled": true, "interval": 60, "claude_path": ""}
}
```

Other targets: `make restart`, `make stop`, `make status`, `make logs` (tails
`~/Library/Logs/ai-usage-swiftbar.log`), `make uninstall`.

## Plugin settings

These are SwiftBar plugin variables, editable in SwiftBar → Preferences → Plugins. Streamable
plugins read them at start, so SwiftBar restarts the plugin when they change.

| Plugin | Variable | Default | Meaning |
|--------|----------|---------|---------|
| OpenRouter | `OPENROUTER_LOW_CREDITS` | `5` | Balance turns orange below this many dollars |
| OpenRouter | `OPENROUTER_CRITICAL_CREDITS` | `1` | Balance turns red below this many dollars |
| OpenRouter | `OPENROUTER_NOTIFY` | `true` | Notify once per threshold crossing |
| OpenRouter | `OPENROUTER_TITLE_STYLE` | `remaining` | `remaining`, `remaining+today`, or `icon-only` |
| Claude | `CLAUDE_USAGE_WARN_PERCENT` | `80` | Weekly percentage that adds the warning triangle |
| both | `AI_USAGE_DATA_DIR` | empty | Daemon data directory, if not the default |

The Claude menu also carries an "Announce when session limit resets" item, unticked by
default. Ticking it arms a one-shot spoken announcement: the next time the session
percentage drops, which means the window has rolled over, the plugin runs `say` in the
background and clears the tick. Ticking it again disarms it. The arm is a file in the
plugin's data directory, so it survives a plugin restart.

## Development

```bash
make test        # unit tests for the daemon and both plugins
make run-local   # daemon --once against fixtures, then each plugin --once
make icons       # regenerate the TIFF icons and print their base64
```

Useful overrides while developing: the daemon accepts `--config`, `--data-dir`, `--once`, and
`--source`; `OPENROUTER_API_BASE` and `OPENROUTER_MANAGEMENT_KEY` bypass the real API and
1Password; `CLAUDE_USAGE_FIXTURE` bypasses `claude`. Each plugin accepts `--once`.

The icons are the OpenRouter glyph and the Claude glyph from
[Simple Icons](https://simpleicons.org/?q=claude), rasterized at 144 DPI and embedded as TIFFs so
that `NSImage(data:)` sizes them at 16 pt on a Retina display. PNG DPI is ignored on that path.
