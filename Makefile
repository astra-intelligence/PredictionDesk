# Thin convenience wrappers. The verify command is `make eval`.
# Interpreter pick: an activated virtualenv wins; else the ./.venv that `make demo` creates; else
# system python3 — so the demo and every other target run in the same environment.
PY := $(if $(VIRTUAL_ENV),python3,$(if $(wildcard .venv/bin/python3),.venv/bin/python3,python3))

.PHONY: bootstrap demo run eval eval-fast eval-core backtest golden_replay status daemons-stop clean \
        ops-start ops-stop ops-status ops-tail check-python

check-python:   ## fail fast with one clear line when the Python in use is older than 3.11
	@$(PY) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null || \
	 { echo "Python 3.11+ is required ('$(PY)' here is $$($(PY) -V 2>&1)) — install a newer Python (macOS: brew install python@3.12; others: python.org/downloads) and re-run."; exit 1; }

bootstrap: check-python      ## fresh machine: run every tenant's manifest bootstrap modules (desk-only: no-op)
	$(PY) -m desk.signals.registry bootstrap
	@echo "[bootstrap] done — start the desk with 'make run' (dev) or the scripts/systemd units"

run: check-python            ## dev/foreground quick-start: capture + driver + dashboard + exit monitor (Ctrl-C stops)
	PYTHON="$(PY)" bash scripts/run_all.sh

demo: check-python           ## fresh-clone first run, offline, zero keys: venv + deps, backtest the bundled real-book sample, serve the console
	@test -d .venv || python3 -m venv .venv
	@.venv/bin/pip -q install -r requirements.txt
	@echo "[demo] dependencies ready (.venv)"
	.venv/bin/python3 scripts/demo.py
	@echo "[demo] Mission Control: http://127.0.0.1:$${DASH_PORT:-8788} (Ctrl-C stops; 'make run' brings up the live-capture paper desk)"
	.venv/bin/python3 -m desk.dashboard_server

eval: check-python           ## run the full eval suite (golden + paper backtest + recon + supervisor/registry guards)
	$(PY) -m desk.eval.run_eval

eval-fast: check-python      ## PR gate: every HARD guard, store/network-free (seconds) — full eval runs nightly/pre-merge
	$(PY) -m desk.eval.run_eval --fast

eval-core: check-python      ## desk-only acceptance: strategies/ moved aside -> core gate + paper driver + exit monitor must pass
	PYTHON="$(PY)" bash scripts/desk_only_smoke.sh

status:         ## desk jobs: supervisor state + driver heartbeat + job locks + capture freshness
	@echo "── desk units ──"; \
	 command -v systemctl >/dev/null 2>&1 && systemctl --user list-units 'desk-*' --no-legend 2>/dev/null || true; \
	 command -v launchctl >/dev/null 2>&1 && (launchctl list | grep com.desk || true) || true
	@echo "── driver heartbeat ──"; cat desk/driver/_heartbeat 2>/dev/null || echo "  (no heartbeat)"; echo ""
	@echo "── job locks (/tmp/desk_*.lock: pid of a RUNNING tick) ──"; ls -la /tmp/desk_*.lock 2>/dev/null || echo "  (no locks)"
	@echo "── capture freshness ──"; ls -lt desk/data/capture/ 2>/dev/null | head -3

ops-start:      ## start the supervised paper stack (capture+driver+exitmonitor+dashboard+caffeinate)
	supervisord -c ops/supervisord.conf
	@sleep 3; supervisorctl -c ops/supervisord.conf status || true
	@echo "[ops] web UI http://127.0.0.1:9100 · ops page http://127.0.0.1:8788/ops"

ops-stop:       ## stop the whole supervised stack (supervisord itself included)
	supervisorctl -c ops/supervisord.conf shutdown

ops-status:     ## one-glance process table from supervisord
	supervisorctl -c ops/supervisord.conf status

ops-tail:       ## last lines of every program log
	@for f in $$HOME/polyedge-data/logs/*.log; do echo "── $$f ──"; tail -n 12 "$$f"; echo; done

daemons-stop:   ## stop every loaded desk job (ops emergency stop; systemd on Linux, launchctl on macOS)
	@command -v systemctl >/dev/null 2>&1 && systemctl --user stop 'desk-*' 2>/dev/null || true
	@command -v launchctl >/dev/null 2>&1 && for l in $$(launchctl list | grep -o 'com\.desk\.[a-z0-9]*'); do \
	  echo "stop $$l"; launchctl bootout gui/$$(id -u)/$$l 2>/dev/null || true; done || true

backtest: check-python       ## run the toy strategy through the paper-trading engine
	$(PY) -m desk.backtest.runner

golden_replay: check-python  ## reconcile engine fills vs captured real fills
	$(PY) -m desk.eval.recon.golden_replay

clean:
	find desk strategies -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
