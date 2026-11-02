# Whalekeeper log server

Stores failure reports sent by Whalekeeper instances and lets you inspect them in a
password-protected web UI. It is a separate service and is not part of the Whalekeeper image.

## Run

```bash
cd logserver
cp .env.example .env    # set LOGSERVER_PASSWORD
docker compose up -d --build
```

The server listens on `127.0.0.1:5455`. Expose it over HTTPS with a reverse proxy or tunnel,
because login cookies are only sent over HTTPS.

## API

`POST /api/events` with `Authorization: Bearer <token>`:

```json
{"instance_id": "<32 hex characters>", "instance_name": "my-host", "kind": "update_failed",
 "level": "error", "container": "nginx", "message": "...", "version": "1.1.55",
 "context": {}, "log_lines": []}
```

`POST /api/heartbeat` takes `instance_id`, `instance_name` and `version`, and only updates "last
seen". An instance is shown as silent after 13 hours without contact.

Events can contain container logs, so treat this server and its backups as sensitive.

The ingest token is public and instance names are self-reported, so treat events as untrusted.
To limit abuse, the server allows 128 KB per request, 120 events per hour per instance and
600 per hour in total. It keeps at most 10,000 events and 1,000 instances, and deletes those older than
`LOGSERVER_RETENTION_DAYS`.
