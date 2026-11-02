"""Whalekeeper log server: stores failure reports and shows them in a web UI.

Everything in an event comes from a public ingest token, so it is untrusted:
all output is escaped and the CSP forbids scripts.
"""
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterator, List, Literal, Optional
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger("logserver")

MAX_BODY_BYTES = 128 * 1024
MAX_EVENTS_PER_INSTANCE_PER_HOUR = 120
MAX_EVENTS_PER_HOUR = 600
MAX_STORED_EVENTS = 10_000
MAX_INSTANCES = 1000
# Instances send a heartbeat every 6 hours; two missed ones means something is wrong.
SILENT_AFTER_SECONDS = 13 * 3600
PRUNE_INTERVAL_SECONDS = 3600
PAGE_SIZE = 50
SESSION_COOKIE = "logserver_session"
SESSION_SECONDS = 7 * 24 * 3600
LOGIN_FAILURE_WINDOW_SECONDS = 300
MAX_LOGIN_FAILURES = 10
HOUR = 3600
DAY = 24 * HOUR

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS instances (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    instance_name TEXT NOT NULL,
    version TEXT NOT NULL,
    kind TEXT NOT NULL,
    level TEXT NOT NULL,
    container TEXT,
    message TEXT NOT NULL,
    context TEXT NOT NULL,
    log_lines TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_received ON events (received_at);
CREATE INDEX IF NOT EXISTS idx_events_instance ON events (instance_id, received_at);
"""

CSS = """
:root { --bg:#f6f7f9; --panel:#fff; --text:#1c2128; --muted:#636c76; --line:#d8dee4;
  --error:#b42318; --warning:#a15c07; --info:#175cd3; }
@media (prefers-color-scheme: dark) { :root { --bg:#14181d; --panel:#1c2128; --text:#e6edf3;
  --muted:#8d96a0; --line:#30363d; --error:#ff7b72; --warning:#e3b341; --info:#79c0ff; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:1100px; margin:0 auto; padding:16px; }
header { display:flex; justify-content:space-between; align-items:center; gap:12px; }
h1 { font-size:20px; margin:8px 0; } h2 { font-size:16px; margin:24px 0 8px; }
a { color:var(--info); }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:8px;
  overflow-x:auto; }
table { width:100%; border-collapse:collapse; }
th,td { padding:8px 12px; text-align:left; border-bottom:1px solid var(--line);
  vertical-align:top; }
tr:last-child td { border-bottom:0; }
th { color:var(--muted); font-weight:500; font-size:13px; }
.muted { color:var(--muted); } .nowrap { white-space:nowrap; }
.level { font-weight:600; }
.level-error { color:var(--error); } .level-warning { color:var(--warning); }
.level-info { color:var(--info); }
form.filters { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin:8px 0 12px; }
input,select,button { font:inherit; color:inherit; background:var(--panel);
  border:1px solid var(--line); border-radius:6px; padding:6px 10px; }
button { cursor:pointer; }
pre { margin:0; padding:12px; overflow-x:auto; font:13px/1.5 ui-monospace,Menlo,monospace; }
.message { white-space:pre-wrap; overflow-wrap:anywhere; }
.login { max-width:340px; margin:15vh auto; display:grid; gap:10px; }
.error { color:var(--error); }
"""

# Applies a dropdown filter as soon as it changes. Allowed by hash, so scripts
# injected through event data stay blocked by the CSP.
FILTER_SCRIPT = (
    "document.querySelectorAll('form.filters select').forEach("
    "s=>s.addEventListener('change',()=>s.form.submit()))"
)
FILTER_SCRIPT_HASH = base64.b64encode(hashlib.sha256(FILTER_SCRIPT.encode()).digest()).decode()


@dataclass
class Settings:
    password: str
    token: str
    db_path: str = "/data/logs.db"
    retention_days: int = 90
    cookie_secure: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        password = os.environ.get("LOGSERVER_PASSWORD", "")
        token = os.environ.get("LOGSERVER_TOKEN", "")
        if len(password) < 12:
            raise SystemExit("LOGSERVER_PASSWORD must be at least 12 characters")
        if len(token) < 32:
            raise SystemExit("LOGSERVER_TOKEN must be at least 32 characters")
        return cls(
            password=password,
            token=token,
            db_path=os.environ.get("LOGSERVER_DB", "/data/logs.db"),
            retention_days=int(os.environ.get("LOGSERVER_RETENTION_DAYS", "90")),
            cookie_secure=os.environ.get("LOGSERVER_COOKIE_SECURE", "true").lower() != "false",
        )


class EventIn(BaseModel):
    instance_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    instance_name: str = Field(min_length=1, max_length=255)
    version: str = Field(default="unknown", max_length=64)
    kind: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    level: Literal["info", "warning", "error"]
    container: Optional[str] = Field(default=None, max_length=255)
    message: str = Field(max_length=8000)
    context: Dict = {}
    log_lines: List[str] = Field(default=[], max_length=500)


class HeartbeatIn(BaseModel):
    instance_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    instance_name: str = Field(min_length=1, max_length=255)
    version: str = Field(default="unknown", max_length=64)


class Store:
    def __init__(self, path: str, retention_days: int):
        self.path = path
        self.retention_days = retention_days
        self.last_prune = 0.0
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('session_secret', ?)",
                (secrets.token_hex(32),),
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def session_secret(self) -> bytes:
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'session_secret'").fetchone()
        return row["value"].encode()

    def add_event(self, event: EventIn) -> bool:
        """Store an event. Returns False when a rate limit was hit."""
        now = int(time.time())
        since = now - HOUR
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            total = db.execute(
                "SELECT COUNT(*) FROM events WHERE received_at > ?", (since,)
            ).fetchone()[0]
            for_instance = db.execute(
                "SELECT COUNT(*) FROM events WHERE instance_id = ? AND received_at > ?",
                (event.instance_id, since),
            ).fetchone()[0]
            if total >= MAX_EVENTS_PER_HOUR or for_instance >= MAX_EVENTS_PER_INSTANCE_PER_HOUR:
                return False
            if not self.touch_instance(db, event, now):
                return False
            db.execute(
                """INSERT INTO events (received_at, instance_id, instance_name, version, kind,
                   level, container, message, context, log_lines)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now, event.instance_id, event.instance_name, event.version, event.kind,
                    event.level, event.container, event.message,
                    json.dumps(event.context), json.dumps(event.log_lines),
                ),
            )
            if now - self.last_prune > PRUNE_INTERVAL_SECONDS:
                self.prune(db, now)
                self.last_prune = now
        return True

    def touch_instance(self, db: sqlite3.Connection, source, now: int) -> bool:
        """Record that an instance was seen. Returns False when no new one may be added."""
        known = db.execute("SELECT 1 FROM instances WHERE id = ?", (source.instance_id,)).fetchone()
        if not known:
            if db.execute("SELECT COUNT(*) FROM instances").fetchone()[0] >= MAX_INSTANCES:
                return False
        db.execute(
            """INSERT INTO instances (id, name, version, last_seen) VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name = excluded.name,
               version = excluded.version, last_seen = excluded.last_seen""",
            (source.instance_id, source.instance_name, source.version, now),
        )
        return True

    def heartbeat(self, heartbeat: HeartbeatIn) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self.touch_instance(db, heartbeat, int(time.time()))

    def prune(self, db: sqlite3.Connection, now: int) -> None:
        cutoff = now - self.retention_days * DAY
        db.execute("DELETE FROM events WHERE received_at < ?", (cutoff,))
        db.execute(
            """DELETE FROM events WHERE id NOT IN
               (SELECT id FROM events ORDER BY id DESC LIMIT ?)""",
            (MAX_STORED_EVENTS,),
        )
        db.execute("DELETE FROM instances WHERE last_seen < ?", (cutoff,))

    def instances(self) -> List[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                """SELECT i.*, (SELECT COUNT(*) FROM events e WHERE e.instance_id = i.id
                   AND e.level != 'info') AS problems
                   FROM instances i ORDER BY i.last_seen DESC"""
            ).fetchall()

    def events(self, instance: str, level: str, q: str, before: Optional[int]) -> List[sqlite3.Row]:
        clauses, params = [], []
        if instance:
            clauses.append("instance_id = ?")
            params.append(instance)
        if level:
            clauses.append("level = ?")
            params.append(level)
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            clauses.append(
                "(message LIKE ? ESCAPE '\\' OR container LIKE ? ESCAPE '\\' "
                "OR kind LIKE ? ESCAPE '\\' OR log_lines LIKE ? ESCAPE '\\')"
            )
            params.extend([like] * 4)
        if before:
            clauses.append("id < ?")
            params.append(before)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as db:
            return db.execute(
                f"""SELECT id, received_at, instance_name, kind, level, container, message
                    FROM events {where} ORDER BY id DESC LIMIT ?""",
                (*params, PAGE_SIZE + 1),
            ).fetchall()

    def event(self, event_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as db:
            return db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def format_time(timestamp: int) -> str:
    """Format in the server's timezone (the TZ environment variable)."""
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>""",
        status_code=status_code,
    )


def page_header(title: str) -> str:
    return f"""<header><h1>{esc(title)}</h1>
<form method="post" action="/logout"><button>Log out</button></form></header>"""


def level_badge(level: str) -> str:
    return f'<span class="level level-{esc(level)}">{esc(level)}</span>'


def instance_status(last_seen: int) -> str:
    if time.time() - last_seen > SILENT_AFTER_SECONDS:
        return '<span class="level level-error">silent</span>'
    return '<span class="muted">online</span>'


def log_block(title: str, lines: List[str]) -> str:
    return f'<h2>{esc(title)}</h2><div class="panel"><pre>{esc(chr(10).join(lines))}</pre></div>'


def context_sections(context: Dict) -> str:
    """Move tracebacks and container logs out of the context, into readable blocks."""
    sections = []
    if isinstance(context.get("traceback"), str):
        sections.append(log_block("Traceback", [context.pop("traceback")]))
    related = [context.get("container")]
    if isinstance(context.get("dependents"), list):
        related.extend(context["dependents"])
    for item in related:
        if isinstance(item, dict) and isinstance(item.get("logs"), list):
            lines = [str(line) for line in item.pop("logs")]
            sections.append(log_block(f"Container log: {item.get('name', 'unknown')}", lines))
    return "".join(sections)


def login_page(error: str = "", status_code: int = 200) -> HTMLResponse:
    message = f'<p class="error">{esc(error)}</p>' if error else ""
    return page(
        "Log in",
        f"""<form class="login" method="post" action="/login"><h1>Whalekeeper logs</h1>{message}
<input type="password" name="password" placeholder="Password" aria-label="Password"
 autocomplete="current-password" required autofocus><button>Log in</button></form>""",
        status_code,
    )


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.db_path, settings.retention_days)
    secret = store.session_secret()
    login_failures: deque = deque()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'sha256-{FILTER_SCRIPT_HASH}'; "
            "style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def sign(expires: str) -> str:
        return hmac.new(secret, expires.encode(), hashlib.sha256).hexdigest()

    def is_logged_in(request: Request) -> bool:
        expires, _, signature = request.cookies.get(SESSION_COOKIE, "").partition(".")
        if not expires.isdigit() or not hmac.compare_digest(sign(expires), signature):
            return False
        return int(expires) > time.time()

    def require_login(request: Request) -> None:
        if not is_logged_in(request):
            raise HTTPException(status_code=303, headers={"Location": "/login"})

    async def read_authorized_body(request: Request) -> bytes:
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied.encode(), settings.token.encode()):
            raise HTTPException(status_code=401, detail="Invalid token")

        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Body too large")
        return bytes(body)

    @app.get("/healthz")
    def healthz() -> Dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/events", status_code=201)
    async def ingest(request: Request) -> Dict[str, str]:
        body = await read_authorized_body(request)
        try:
            event = EventIn.model_validate_json(body)
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=f"{e.error_count()} invalid field(s)")

        if not await run_in_threadpool(store.add_event, event):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        return {"status": "stored"}

    @app.post("/api/heartbeat")
    async def heartbeat(request: Request) -> Dict[str, str]:
        body = await read_authorized_body(request)
        try:
            beat = HeartbeatIn.model_validate_json(body)
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=f"{e.error_count()} invalid field(s)")
        if not await run_in_threadpool(store.heartbeat, beat):
            raise HTTPException(status_code=429, detail="Too many instances")
        return {"status": "ok"}

    @app.get("/login")
    def login_form(request: Request) -> Response:
        if is_logged_in(request):
            return RedirectResponse("/", status_code=303)
        return login_page()

    @app.post("/login")
    async def login(request: Request) -> Response:
        now = time.time()
        while login_failures and login_failures[0] < now - LOGIN_FAILURE_WINDOW_SECONDS:
            login_failures.popleft()
        if len(login_failures) >= MAX_LOGIN_FAILURES:
            return login_page("Too many failed attempts. Try again in a few minutes.", 429)

        body = await request.body()
        fields = parse_qs(body[:4096].decode("utf-8", "replace"))
        supplied = fields.get("password", [""])[0]
        if not hmac.compare_digest(supplied.encode(), settings.password.encode()):
            login_failures.append(now)
            logger.warning("Failed login attempt")
            return login_page("Wrong password.", 401)

        expires = str(int(now) + SESSION_SECONDS)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            f"{expires}.{sign(expires)}",
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
        )
        return response

    @app.post("/logout")
    def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get("/")
    def event_list(
        request: Request,
        instance: str = "",
        level: str = "",
        q: str = "",
        before: Optional[int] = Query(default=None),
    ) -> Response:
        require_login(request)
        instances = store.instances()
        rows = store.events(instance, level, q, before)
        has_more = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]

        instance_rows = "".join(
            f"""<tr><td><a href="/?{urlencode({'instance': i['id']})}">{esc(i['name'])}</a>
<div class="muted">{esc(i['id'][:8])}</div></td><td>{esc(i['version'])}</td>
<td>{instance_status(i['last_seen'])}</td>
<td class="nowrap">{format_time(i['last_seen'])}</td><td>{i['problems']}</td></tr>"""
            for i in instances
        ) or '<tr><td colspan="5" class="muted">No instance has reported yet.</td></tr>'

        instance_options = "".join(
            f'<option value="{esc(i["id"])}"{" selected" if i["id"] == instance else ""}>'
            f'{esc(i["name"])} ({esc(i["id"][:8])})</option>'
            for i in instances
        )
        level_options = "".join(
            f'<option value="{name}"{" selected" if name == level else ""}>{name}</option>'
            for name in ("error", "warning", "info")
        )

        event_rows = "".join(
            f"""<tr><td class="nowrap">{format_time(e['received_at'])}</td>
<td>{level_badge(e['level'])}</td><td>{esc(e['instance_name'])}</td>
<td><a href="/events/{e['id']}">{esc(e['kind'])}</a></td><td>{esc(e['container'])}</td>
<td class="message">{esc(e['message'][:200])}</td></tr>"""
            for e in rows
        ) or '<tr><td colspan="6" class="muted">No events found.</td></tr>'

        clear_link = '<a class="clear" href="/">Clear filters</a>' if instance or level or q else ""

        next_link = ""
        if has_more:
            query = urlencode(
                {"instance": instance, "level": level, "q": q, "before": rows[-1]["id"]}
            )
            next_link = f'<p><a href="/?{query}">Older events</a></p>'

        return page(
            "Whalekeeper logs",
            f"""{page_header("Whalekeeper logs")}
<h2>Instances</h2><div class="panel"><table>
<tr><th>Host</th><th>Version</th><th>Status</th><th>Last seen</th><th>Problems</th></tr>{instance_rows}
</table></div>
<h2>Events</h2>
<form class="filters" method="get" action="/">
<select name="instance" aria-label="Instance"><option value="">All instances</option>{instance_options}</select>
<select name="level" aria-label="Level"><option value="">All levels</option>{level_options}</select>
<input type="search" name="q" value="{esc(q)}" placeholder="Search" aria-label="Search">
<button>Search</button>{clear_link}</form><script>{FILTER_SCRIPT}</script>
<div class="panel"><table>
<tr><th>Received</th><th>Level</th><th>Host</th><th>Kind</th><th>Container</th><th>Message</th></tr>
{event_rows}</table></div>{next_link}""",
        )

    @app.get("/events/{event_id}")
    def event_detail(request: Request, event_id: int) -> Response:
        require_login(request)
        event = store.event(event_id)
        if event is None:
            return page("Not found", '<h1>Event not found</h1><p><a href="/">Back</a></p>', 404)
        context_data = json.loads(event["context"])
        sections = context_sections(context_data)
        context = json.dumps(context_data, indent=2, sort_keys=True)
        log_lines = "\n".join(json.loads(event["log_lines"]))
        return page(
            f"{event['kind']} #{event['id']}",
            f"""{page_header(f"{event['kind']} #{event['id']}")}
<p><a href="/">Back to events</a></p>
<div class="panel"><table>
<tr><th>Level</th><td>{level_badge(event['level'])}</td></tr>
<tr><th>Received</th><td>{format_time(event['received_at'])}</td></tr>
<tr><th>Host</th><td>{esc(event['instance_name'])} ({esc(event['instance_id'][:8])})</td></tr>
<tr><th>Version</th><td>{esc(event['version'])}</td></tr>
<tr><th>Container</th><td>{esc(event['container'])}</td></tr>
<tr><th>Message</th><td class="message">{esc(event['message'])}</td></tr>
</table></div>{sections}
<h2>Context</h2><div class="panel"><pre>{esc(context)}</pre></div>
<h2>Whalekeeper log</h2><div class="panel"><pre>{esc(log_lines) or "No log lines."}</pre></div>""",
        )

    return app
