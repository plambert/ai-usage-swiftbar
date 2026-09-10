LABEL      := net.plambert.ai-usage-swiftbar
PYTHON     ?= $(shell command -v python3)
PREFIX     ?= $(HOME)/.local
DAEMON_DIR := $(PREFIX)/libexec/ai-usage-swiftbar
DAEMON     := $(DAEMON_DIR)/ai-usage-daemon.py
CONFIG_DIR := $(HOME)/.config/ai-usage-swiftbar
CONFIG     := $(CONFIG_DIR)/config.json
DATA_DIR   := $(HOME)/Library/Application Support/ai-usage-swiftbar
LOG        := $(HOME)/Library/Logs/ai-usage-swiftbar.log
PLIST      := $(HOME)/Library/LaunchAgents/$(LABEL).plist
PLUGIN_DIR := $(shell defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || echo "$$HOME/Library/Application Support/SwiftBar/Plugins")
PLUGINS    := openrouter-credits.stream.py claude-usage.stream.py
UID        := $(shell id -u)

.PHONY: all test run-local install install-daemon install-plugins uninstall \
        uninstall-daemon uninstall-plugins restart stop status logs icons

all: test

test:
	$(PYTHON) -m unittest discover -s tests -v

run-local:
	tests/run-local.sh

install: install-daemon install-plugins

# Copies the daemon, writes the config from the example if absent, renders
# the LaunchAgent plist, and (re)loads it. Loading starts the daemon, which
# resolves the OpenRouter key from 1Password: expect one approval prompt.
install-daemon:
	install -d "$(DAEMON_DIR)" "$(CONFIG_DIR)" "$(DATA_DIR)" "$(dir $(PLIST))" "$(dir $(LOG))"
	install -m 755 daemon/ai-usage-daemon.py "$(DAEMON)"
	@if [ ! -f "$(CONFIG)" ]; then \
	    install -m 600 config.example.json "$(CONFIG)"; \
	    echo "Wrote $(CONFIG) from config.example.json; set openrouter.key_ref."; \
	fi
	sed -e 's|@PYTHON@|$(PYTHON)|g' -e 's|@DAEMON@|$(DAEMON)|g' \
	    -e 's|@CONFIG@|$(CONFIG)|g' -e 's|@DATA_DIR@|$(DATA_DIR)|g' \
	    -e 's|@LOG@|$(LOG)|g' -e 's|@HOME@|$(HOME)|g' \
	    launchd/$(LABEL).plist.in > "$(PLIST)"
	plutil -lint "$(PLIST)"
	-launchctl bootout gui/$(UID)/$(LABEL) 2>/dev/null
	launchctl bootstrap gui/$(UID) "$(PLIST)"
	@echo "Daemon loaded as $(LABEL); log: $(LOG)"

install-plugins:
	@for p in $(PLUGINS); do install -m 755 "plugins/$$p" "$(PLUGIN_DIR)/$$p"; done
	@echo "Installed $(PLUGINS) to $(PLUGIN_DIR)"

uninstall: uninstall-plugins uninstall-daemon

uninstall-daemon:
	-launchctl bootout gui/$(UID)/$(LABEL) 2>/dev/null
	rm -f "$(PLIST)" "$(DAEMON)"
	@echo "Left $(CONFIG) and $(DATA_DIR) in place."

uninstall-plugins:
	@for p in $(PLUGINS); do rm -f "$(PLUGIN_DIR)/$$p"; done

restart:
	launchctl kickstart -k gui/$(UID)/$(LABEL)

stop:
	launchctl bootout gui/$(UID)/$(LABEL)

status:
	launchctl print gui/$(UID)/$(LABEL) | sed -n '1,25p'

logs:
	tail -n 50 -f "$(LOG)"

icons:
	tools/make-icon.sh openrouter-glyph
	tools/make-icon.sh claude-glyph
