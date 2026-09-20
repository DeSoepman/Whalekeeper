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
```

`docker-compose.yml` live-mounts `./app`, `./config`, `./data`, `tests`, and templates/static.
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
- `app/database.py` — SQLite: update history, check logs, image-version snapshots (for rollback),
  users, and encrypted secure settings.
- `app/config.py` — pydantic models loaded from `config/config.yaml` (`load_config()`).
- `app/notifications.py` — email (SMTP) / Discord / generic webhook fan-out.
- `app/web/routes.py` — all HTTP + JSON API endpoints, session auth (`require_auth` dependency).

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

### Conventions

- All Docker operations are async; never block the event loop with sync calls.
- Record every container operation in the DB via `add_update_history` (status `success`/`failed`) and
  notify on rollbacks/critical errors. Use `logger`, never `print`. Catch specific exceptions, not bare `except`.
- Sensitive values (SMTP passwords, registry creds) are encrypted via `set_secure_setting` /
  `get_secure_setting`, not stored in plaintext config. Never log credentials.
- Don't modify the user's docker-compose files.

A much more detailed style/pattern guide lives in `.github/copilot-instructions.md` — consult it for
endpoint/operation boilerplate and the full "Don't Do" list.
