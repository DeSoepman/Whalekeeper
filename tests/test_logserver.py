"""Tests for the log server (logserver/)."""
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from logserver import main as logserver
from logserver.main import Settings, create_app

TOKEN = "t" * 40
PASSWORD = "correct-horse-battery"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def event(**overrides):
    payload = {
        "instance_id": "a" * 32,
        "instance_name": "brother-nas",
        "version": "1.2.3",
        "kind": "update_failed",
        "level": "error",
        "container": "nginx",
        "message": "port is already allocated",
        "context": {"container": {"status": "created"}},
        "log_lines": ["line one", "line two"],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "logs.db")


@pytest.fixture
def client(db_path):
    settings = Settings(password=PASSWORD, token=TOKEN, db_path=db_path, cookie_secure=False)
    return TestClient(create_app(settings), follow_redirects=False)


@pytest.fixture
def logged_in(client):
    assert client.post("/login", data={"password": PASSWORD}).status_code == 303
    return client


class TestIngest:
    def test_stores_event(self, logged_in):
        assert logged_in.post("/api/events", json=event(), headers=AUTH).status_code == 201
        page = logged_in.get("/").text
        assert "brother-nas" in page
        assert "port is already allocated" in page

    @pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
    def test_rejects_bad_token(self, client, headers):
        assert client.post("/api/events", json=event(), headers=headers).status_code == 401

    @pytest.mark.parametrize("overrides", [
        {"instance_id": "../../etc"},
        {"level": "fatal"},
        {"kind": "Not Valid!"},
        {"message": "x" * 8001},
    ])
    def test_rejects_invalid_event(self, client, overrides):
        response = client.post("/api/events", json=event(**overrides), headers=AUTH)
        assert response.status_code == 422

    def test_rejects_oversized_body(self, client):
        response = client.post("/api/events", content=b"x" * (logserver.MAX_BODY_BYTES + 1), headers=AUTH)
        assert response.status_code == 413

    def test_limits_rate_per_instance(self, client, monkeypatch):
        monkeypatch.setattr(logserver, "MAX_EVENTS_PER_INSTANCE_PER_HOUR", 2)
        statuses = [client.post("/api/events", json=event(), headers=AUTH).status_code for _ in range(3)]
        assert statuses == [201, 201, 429]
        other = event(instance_id="b" * 32)
        assert client.post("/api/events", json=other, headers=AUTH).status_code == 201

    def test_limits_total_rate(self, client, monkeypatch):
        monkeypatch.setattr(logserver, "MAX_EVENTS_PER_HOUR", 1)
        assert client.post("/api/events", json=event(), headers=AUTH).status_code == 201
        other = event(instance_id="b" * 32)
        assert client.post("/api/events", json=other, headers=AUTH).status_code == 429


class TestPrune:
    def test_removes_expired_and_excess_events(self, client, db_path, monkeypatch):
        monkeypatch.setattr(logserver, "MAX_STORED_EVENTS", 2)
        client.post("/api/events", json=event(), headers=AUTH)
        connection = sqlite3.connect(db_path)
        with connection:
            connection.execute("UPDATE events SET received_at = ?", (int(time.time()) - 91 * 86400,))
        connection.close()

        store = logserver.Store(db_path, retention_days=90)
        for _ in range(3):
            store.add_event(logserver.EventIn(**event()))
            store.last_prune = 0

        connection = sqlite3.connect(db_path)
        count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        oldest = connection.execute("SELECT MIN(received_at) FROM events").fetchone()[0]
        connection.close()
        assert count == 2
        assert oldest > time.time() - 60


class TestAuth:
    @pytest.mark.parametrize("path", ["/", "/events/1"])
    def test_pages_require_login(self, client, path):
        response = client.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_wrong_password(self, client):
        assert client.post("/login", data={"password": "wrong"}).status_code == 401
        assert client.get("/").status_code == 303

    def test_forged_cookie_is_rejected(self, client):
        client.cookies.set(logserver.SESSION_COOKIE, f"{int(time.time()) + 999}.forged")
        assert client.get("/").status_code == 303

    def test_expired_session_is_rejected(self, db_path, monkeypatch):
        monkeypatch.setattr(logserver, "SESSION_SECONDS", -1)
        settings = Settings(password=PASSWORD, token=TOKEN, db_path=db_path, cookie_secure=False)
        client = TestClient(create_app(settings), follow_redirects=False)
        client.post("/login", data={"password": PASSWORD})
        assert client.get("/").status_code == 303

    def test_session_cookie_flags(self, db_path):
        settings = Settings(password=PASSWORD, token=TOKEN, db_path=db_path)
        client = TestClient(create_app(settings), follow_redirects=False)
        cookie = client.post("/login", data={"password": PASSWORD}).headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "secure" in cookie
        assert "samesite=strict" in cookie

    def test_logout(self, logged_in):
        logged_in.post("/logout")
        assert logged_in.get("/").status_code == 303

    def test_locks_after_repeated_failures(self, client):
        for _ in range(logserver.MAX_LOGIN_FAILURES):
            client.post("/login", data={"password": "wrong"})
        assert client.post("/login", data={"password": PASSWORD}).status_code == 429

    def test_api_docs_are_disabled(self, client):
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404

    def test_rejects_too_short_password(self, monkeypatch):
        monkeypatch.setenv("LOGSERVER_PASSWORD", "short")
        monkeypatch.setenv("LOGSERVER_TOKEN", TOKEN)
        with pytest.raises(SystemExit):
            Settings.from_env()


class TestFormatTime:
    @pytest.mark.parametrize("timestamp,expected", [
        (1767225600, "2026-01-01 01:00:00 CET"),
        (1782864000, "2026-07-01 02:00:00 CEST"),
    ])
    def test_uses_configured_timezone(self, monkeypatch, timestamp, expected):
        monkeypatch.setenv("TZ", "Europe/Brussels")
        time.tzset()
        try:
            assert logserver.format_time(timestamp) == expected
        finally:
            monkeypatch.undo()
            time.tzset()


class TestHeartbeat:
    BEAT = {"instance_id": "c" * 32, "instance_name": "brother-nas", "version": "1.2.3"}

    def test_updates_instance_without_creating_event(self, logged_in):
        assert logged_in.post("/api/heartbeat", json=self.BEAT, headers=AUTH).status_code == 200
        page = logged_in.get("/").text
        assert "brother-nas" in page
        assert "online" in page
        assert "No events found" in page

    def test_requires_token(self, client):
        assert client.post("/api/heartbeat", json=self.BEAT).status_code == 401

    def test_validates_input(self, client):
        response = client.post("/api/heartbeat", json={**self.BEAT, "instance_id": "x"}, headers=AUTH)
        assert response.status_code == 422

    def test_silent_instance_is_flagged(self, logged_in, db_path):
        logged_in.post("/api/heartbeat", json=self.BEAT, headers=AUTH)
        connection = sqlite3.connect(db_path)
        with connection:
            connection.execute("UPDATE instances SET last_seen = ?", (int(time.time()) - 14 * 3600,))
        connection.close()
        assert "silent" in logged_in.get("/").text

    def test_limits_number_of_instances(self, client, monkeypatch):
        monkeypatch.setattr(logserver, "MAX_INSTANCES", 1)
        assert client.post("/api/heartbeat", json=self.BEAT, headers=AUTH).status_code == 200
        assert client.post("/api/heartbeat", json=self.BEAT, headers=AUTH).status_code == 200
        other = {**self.BEAT, "instance_id": "d" * 32}
        assert client.post("/api/heartbeat", json=other, headers=AUTH).status_code == 429
        assert client.post("/api/events", json=event(instance_id="d" * 32), headers=AUTH).status_code == 429


class TestViewer:
    def test_detail_shows_traceback_and_container_logs(self, logged_in):
        context = {
            "traceback": "ValueError: <bad>",
            "container": {"name": "nginx", "status": "exited", "logs": ["fatal: <db> down"]},
            "dependents": [{"name": "qbit", "logs": ["dep line"]}],
        }
        logged_in.post("/api/events", json=event(context=context), headers=AUTH)
        page = logged_in.get("/events/1").text
        assert "<h2>Traceback</h2>" in page
        assert "ValueError: &lt;bad&gt;" in page
        assert "Container log: nginx" in page
        assert "fatal: &lt;db&gt; down" in page
        assert "Container log: qbit" in page

    def test_detail_survives_malformed_context(self, logged_in):
        context = {"traceback": 5, "container": "x", "dependents": ["y", 1], }
        logged_in.post("/api/events", json=event(context=context), headers=AUTH)
        assert logged_in.get("/events/1").status_code == 200

    def test_detail_shows_context_and_logs(self, logged_in):
        logged_in.post("/api/events", json=event(), headers=AUTH)
        page = logged_in.get("/events/1").text
        assert "created" in page
        assert "line two" in page

    def test_unknown_event(self, logged_in):
        assert logged_in.get("/events/999").status_code == 404

    def test_untrusted_values_are_escaped(self, logged_in):
        attack = "<script>alert(1)</script>"
        logged_in.post(
            "/api/events",
            json=event(
                instance_name=attack, container=attack, message=attack,
                version=attack[:60], context={"x": attack}, log_lines=[attack],
            ),
            headers=AUTH,
        )
        for path in ("/", "/events/1"):
            page = logged_in.get(path).text
            assert "<script>alert" not in page
            assert "&lt;script&gt;" in page

    def test_search_input_is_escaped(self, logged_in):
        page = logged_in.get("/", params={"q": '"><script>alert(1)</script>'}).text
        assert "<script>alert" not in page

    def test_csp_only_allows_the_filter_script(self, logged_in):
        response = logged_in.get("/")
        csp = response.headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert f"script-src 'sha256-{logserver.FILTER_SCRIPT_HASH}'" in csp
        assert "unsafe-inline'; form-action" in csp
        assert "script-src 'unsafe-inline'" not in csp
        assert f"<script>{logserver.FILTER_SCRIPT}</script>" in response.text

    def test_clear_link_only_when_filtered(self, logged_in):
        assert "Clear filters" not in logged_in.get("/").text
        assert "Clear filters" in logged_in.get("/", params={"level": "error"}).text

    def test_filters(self, logged_in):
        logged_in.post("/api/events", json=event(), headers=AUTH)
        logged_in.post(
            "/api/events",
            json=event(instance_id="b" * 32, level="info", kind="startup", message="started up"),
            headers=AUTH,
        )
        assert "started up" not in logged_in.get("/", params={"level": "error"}).text
        assert "started up" in logged_in.get("/", params={"instance": "b" * 32}).text
        assert "allocated" not in logged_in.get("/", params={"instance": "b" * 32}).text
        assert "started up" not in logged_in.get("/", params={"q": "allocated"}).text

    def test_search_treats_wildcards_literally(self, logged_in):
        logged_in.post("/api/events", json=event(), headers=AUTH)
        assert "No events found" in logged_in.get("/", params={"q": "%"}).text

    def test_pagination(self, logged_in, monkeypatch):
        monkeypatch.setattr(logserver, "PAGE_SIZE", 1)
        for message in ("first event", "second event"):
            logged_in.post("/api/events", json=event(message=message), headers=AUTH)
        page = logged_in.get("/").text
        assert "second event" in page
        assert "first event" not in page
        assert "first event" in logged_in.get("/", params={"before": 2}).text
