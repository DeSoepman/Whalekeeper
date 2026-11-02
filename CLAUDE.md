# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Whalekeeper — a self-hosted Docker container auto-updater with a web UI, health-check
monitoring, and automatic rollback (a safer watchtower alternative). It runs as a single
container with the host's Docker socket mounted, and is distributed as a Docker image, so
**keep the image lightweight** — avoid adding dependencies or system packages without a strong reason.

## Commands

```bash
# Tests
./test.sh                                    # full suite (installs dev deps if missing)
# pytest isn't installed on the dev host, so run the suite in a throwaway container there:
docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD":/src:ro -w /src python:3.11-slim sh -c \
  'pip install -q -r requirements-dev.txt && python -m pytest tests/ -q -p no:cacheprovider'
pytest tests/ -v                             # all tests
pytest tests/test_database.py::test_name -v  # single test
# markers: -m unit | -m integration | -m slow

# Local dev (Docker)
docker compose up -d --build   # rebuild + restart (REQUIRED after editing any app/*.py, requirements.txt, Dockerfile, docker-compose.yml)
docker compose logs -f
docker compose restart         # restart without rebuild

# Run without Docker
python -m app.main             # serves on :5454

# Deploy (bumps VERSION patch, commits, pushes to git + Docker Hub + GHCR)
./deploy.sh "commit message"

# Log server (separate service, see below)
cd logserver && docker compose up -d --build
```

`deploy.sh` pushes to git before it runs `test.sh`, and always publishes `latest`. On the dev host the
tests fail for lack of pytest, so it stops halfway. Publishing `latest` reaches every installation that
auto-updates, so to try a version first, push only its version tag.

`docker-compose.yml` live-mounts `./app`, `./data`, `tests`, and templates/static.
Editing `app/web/templates/*` or `app/web/static/app.js` needs only a browser refresh (no rebuild);
editing any `.py` needs a rebuild. See `DEVELOPMENT.md`.

## Architecture

FastAPI + asyncio backend, SQLite storage, vanilla-JS frontend (no framework). No ORM —
all persistence goes through the hand-written `Database` class.

- `app/main.py` — entry point. A `lifespan` context builds the singletons (`Config`, `Database`,
  `DockerMonitor`, `NotificationService`) and starts `monitor.start_monitoring()` as a background
  task. These singletons are injected into `app/web/routes.py` via module-level globals
  (`routes.monitor`, `routes.db`, `routes.config`) rather than DI containers.
- `app/docker_monitor.py` — the core (~1700 lines). Owns the Docker SDK client and all update logic.
- `app/database.py` — SQLite: configuration, update history, check logs, image-version snapshots
  (for rollback), users, and secure settings.
- `app/config.py` — pydantic models stored in the database (`load_config(db)`). A `config/config.yaml` from an older version is imported once.
- `app/notifications.py` — email (SMTP) / Discord / generic webhook fan-out.
- `app/web/routes.py` — all HTTP + JSON API endpoints, session auth (`require_auth` dependency).
- `app/diagnostics.py` — sends reports to the log server, see below.
- `logserver/` — separate service that stores and shows those reports. Not part of the Whalekeeper image.

### Update flow (the central logic in `docker_monitor.py`)

1. `check_for_updates` compares the running container's image digest against the latest pulled image.
2. `update_container` (standalone) or `update_compose_container` (compose-managed) performs the swap:
   save current config → pull → stop/remove old → recreate with same config → monitor health.
3. `monitor_container_health` watches Docker's native health check after the swap; if none exists it
   falls back to crash detection. On failure, `rollback_after_failed_update` restores the prior image.
4. Old image versions are snapshotted in the DB; `cleanup_old_versions` keeps the last N (`rollback.keep_versions`).

Key distinctions to respect when editing:
- **Compose vs standalone**: containers with the `com.docker.compose.project` label are compose-managed
  and updated differently. The image intentionally ships **without** the Docker CLI, so don't assume
  `docker compose` is callable — handle its absence gracefully.
- **Dependent containers**: `detect_dependent_containers` / `stop_dependent_containers` /
  `restart_dependent_containers` handle containers networked to the one being updated
  (`monitoring.auto_restart_dependents`). Network reattachment is non-trivial — see `reconnect_networks`.
- **Self-update**: `self_update` handles updating Whalekeeper's own container; the new container takes
  over since the old one is replaced mid-operation. `self_check_loop` runs separately from the main schedule.

### Diagnostics and log server

Whalekeeper reports to `https://whalekeeper.rubenpauwels.be`. This is **on by default for everyone using
the public image**; `WHALEKEEPER_DIAGNOSTICS=off` disables it and `WHALEKEEPER_DIAGNOSTICS_URL` overrides
the address.

- **Automatic:** `ErrorLogForwarder` sends every `logger.error` with its traceback, once per day per
  distinct message. `pull access denied` is skipped, since every locally built image causes it.
- **Explicit:** `_report()` in `docker_monitor.py` adds container state and logs, for example
  `update_failed`, `update_rolled_back` and `update_success`. Pass `extra=ALREADY_REPORTED` on the matching
  `logger.error` to avoid a second report of the same failure.
- **Before a rollback:** call `_capture_state()` first, since the rollback removes the failed container.
- **Never send environment variables.** `describe_container` is a whitelist. Masking with `redact` is best
  effort, so container logs can still contain secrets.
- **Keep the Diagnostics section in `README.md` accurate** whenever what gets sent changes.
- **`INGEST_TOKEN` is public on purpose:** it ships in the image, so the server treats all input as
  untrusted. Its limits and escaping are the real protection. Never put a real secret there.

The log server is FastAPI + SQLite with server-rendered HTML. It runs from `logserver/docker-compose.yml`
with a Cloudflare Tunnel and its own `logserver/.env`, which holds the UI password and tunnel token and must
never be committed. Its CSP only allows the one filter script, by hash, so changing `FILTER_SCRIPT` updates
the hash automatically but any other script is blocked.

### Conventions

- All Docker operations are async; never block the event loop with sync calls.
- Record every container operation in the DB via `add_update_history` (status `success`/`failed`) and
  notify on rollbacks/critical errors. Use `logger`, never `print`. Catch specific exceptions, not bare `except`.
- The SMTP password goes through `set_secure_setting` / `get_secure_setting`. Despite the name, these
  are not encrypted. Never log credentials.
- Don't modify the user's docker-compose files.

A much more detailed style/pattern guide lives in `.github/copilot-instructions.md` — consult it for
endpoint/operation boilerplate and the full "Don't Do" list.
